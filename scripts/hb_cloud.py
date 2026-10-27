#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
hb_cloud.py - Werkzeug fuer den Cloud-Agenten: Hoerbuch-Katalog ergaenzen (Identitaet, Beschreibung, Genre, Cover)

Der Agent entscheidet, dieses Skript erledigt alles Deterministische. Pro Buch eine Jobdatei
jobs/<book_id>.json mit Status je Kategorie (identitaet, beschreibung, cover):
pending | pruefen | ok | failed | skipped.  Die DB wird nur gelesen.

Befehle (alle mit --work ARBEITSORDNER, Standard ./work):
  init     --db DB [--kategorie K ...] [--limit N]   Jobs fuer noch nicht recherchierte Buecher anlegen
           (bestehende Jobdateien bleiben unangetastet)
  index    DATEI                                     Bereits abgelieferte IDs (index.json von Drive) als erledigt markieren
  audible  [--limit N] [--ids ...]                    Audible-Durchgang fuer alle offenen Jobs (kostet keine Modell-Token)
  offen    [--n 10] [--kategorie K]                  kompakte Liste der Jobs, die der Agent bearbeiten muss
  zeige    ID                                         eine Jobdatei kompakt
  setze    ID --json '{...}' | --datei F.json        Ergebnis des Agenten einarbeiten (Felder + Status)
  cover    ID URL [--seite URL] [--quelle NAME]       Bild laden, pruefen, als Cover vormerken
  asin     ID ASIN                                    Buch fest einem Audible-Produkt zuordnen (Daten + Cover)
  blatt    [--ids ...] [--n 40] --out F.jpg           Kontaktbogen der vorgemerkten Cover zur Sichtpruefung
  stand                                               Zaehler je Kategorie und Status
  paket    --out teil_NNNN.json [--max 35]            fertige, noch nicht abgelieferte Jobs buendeln; schreibt index.json
           und je eine Upload-Fassung *.up.json (diese hochladen; Groesse muss der .json entsprechen)
  suche    -t TITEL [-a AUTOR] | STICHWORTE ...       Audible-Katalogsuche, kompakt (titel/autor trifft genauer als Stichworte)
  serie    ASIN [--parent]                            alle Baende der Audible-Serie eines Produkts (Nummer | ASIN | Titel | Datum)
"""
from __future__ import annotations

import argparse
import collections
import importlib.util
import io
import json
import re
import sqlite3
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath

HERE = Path(__file__).resolve().parent


def _load(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, HERE / file)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


IDT = _load("hb_identitaet", "hoerbuch_identitaet_v0_1.py")
M = IDT.M
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

STATI = ("pending", "pruefen", "ok", "failed", "skipped")
KATS = ("identitaet", "beschreibung", "cover")
FULL_GROUPS = "contributors,product_desc,product_attrs,product_extended_attrs,series,media,category_ladders"
MIN_DESC = 250          # Zeichen; kuerzere Audible-Texte -> Agent schreibt selbst


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Jobdateien
# ---------------------------------------------------------------------------

class Store:
    def __init__(self, work: Path):
        self.work = work
        self.jobs = work / "jobs"
        self.thumbs = work / "thumbs"
        self.jobs.mkdir(parents=True, exist_ok=True)
        self.thumbs.mkdir(parents=True, exist_ok=True)

    def path(self, bid: int) -> Path:
        return self.jobs / f"{int(bid)}.json"

    def load(self, bid: int) -> dict:
        p = self.path(bid)
        if not p.is_file():
            sys.exit(f"[FEHLER] Job {bid} existiert nicht")
        return json.loads(p.read_text(encoding="utf-8"))

    def save(self, job: dict) -> None:
        job["geaendert"] = now()
        tmp = self.path(job["id"]).with_suffix(".tmp")
        tmp.write_text(json.dumps(job, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path(job["id"]))

    def all(self) -> list[dict]:
        out = []
        for p in sorted(self.jobs.glob("*.json"), key=lambda x: int(x.stem)):
            out.append(json.loads(p.read_text(encoding="utf-8")))
        return out


def new_job(r: sqlite3.Row) -> dict:
    rel = r["relative_path"] or ""
    return {
        "id": r["id"], "fp": r["fingerprint"], "kategorie": PureWindowsPath(rel).parts[0] if rel else "",
        "ordner": rel, "folder_name": r["folder_name"],
        "guess": {"author": r["author_guess"], "title": r["title_guess"], "volume": r["volume_guess"]},
        "audio_count": r["audio_count"], "mb": round((r["total_bytes"] or 0) / 1e6),
        "status": {k: "pending" for k in KATS},
        "audible_versucht": False, "abgeliefert": None,
        "p": {"title": None, "authors": [], "narrators": [], "series": None, "series_number": None,
              "asin": None, "publisher": None, "release_date": None, "runtime_min": None,
              "description": None, "description_source": None, "primary_genre": None, "subgenres": [],
              "cover": None, "sources": [], "confidence": None, "note": None},
        "log": [], "angelegt": now(),
    }


def log(job: dict, msg: str) -> None:
    job["log"] = (job.get("log") or [])[-9:] + [f"{now()} {msg}"]


def terminal(job: dict) -> bool:
    return all(job["status"][k] in ("ok", "failed", "skipped") for k in KATS)


# ---------------------------------------------------------------------------
# Audible
# ---------------------------------------------------------------------------

def is_placeholder(p: dict) -> bool:
    """Audible fuehrt leere Platzhalter-Produkte (Erscheinungsdatum 2200, Verlag 'ZZZ - Series Advisor Placeholder')."""
    return str(p.get("release_date") or "").startswith("2200") or "placeholder" in str(p.get("publisher_name") or "").lower()


def audible_product(asin: str) -> dict | None:
    url = f"{M.AUDIBLE_ENDPOINT}/{urllib.parse.quote(asin)}?response_groups={FULL_GROUPS}&image_sizes=1024,2000,500"
    for attempt in range(3):
        try:
            p = M.http_json(url).get("product") or None
            return None if (p and is_placeholder(p)) else p
        except Exception:
            time.sleep(1.5 * (attempt + 1))
    return None


def genres_from(p: dict) -> tuple[str | None, list[str]]:
    """Hauptgenre = oberste Audible-Kategorie der ersten Leiter, Untergenres = die tieferen Stufen aller Leitern."""
    top, sub = None, []
    for lad in p.get("category_ladders") or []:
        steps = [str(s.get("name") or "").strip() for s in lad.get("ladder") or []]
        steps = [s for s in steps if s]
        if steps and top is None:
            top = steps[0]
        for n in steps[1:]:
            if n not in sub:
                sub.append(n)
    return top, sub[:6]


def fill_from_product(job: dict, p: dict, score: float | None, via: str) -> None:
    P = job["p"]
    pser, pseq = M.audible_series(p.get("series"))
    pser = IDT.SERIES_RANGE.sub("", IDT.BRACKETS.sub("", pser or "")).strip() or None
    P["title"] = IDT.tidy_title(str(p.get("title") or ""), pser) or P["title"]
    auth = IDT.clean_people(M.audible_names(p.get("authors")))
    P["authors"] = [a for a in auth if not (pser and M.text_similarity(a, pser) >= 0.8)] or auth
    P["narrators"] = M.audible_names(p.get("narrators"))
    P["series"], P["series_number"] = pser, pseq or P["series_number"]
    P["asin"] = p.get("asin")
    P["publisher"] = p.get("publisher_name")
    P["release_date"] = p.get("release_date") or p.get("issue_date")
    P["runtime_min"] = p.get("runtime_length_min")
    P["confidence"] = score
    g, sub = genres_from(p)
    P["primary_genre"], P["subgenres"] = P["primary_genre"] or g, P["subgenres"] or sub
    src = {"url": f"https://www.audible.de/pd/{p.get('asin')}", "supports": ["identitaet", "beschreibung"]}
    if src not in P["sources"]:
        P["sources"].append(src)
    desc = M.html_to_text(p.get("publisher_summary"))   # merchandising_summary ist gekuerzt ("...")
    if desc and len(desc) >= MIN_DESC and job["status"]["beschreibung"] != "ok":
        P["description"], P["description_source"] = desc.strip(), "audible.de"
        job["status"]["beschreibung"] = "ok"
    elif job["status"]["beschreibung"] != "ok":
        job["status"]["beschreibung"] = "pending"
    imgs = p.get("product_images") or {}
    url = imgs.get("1024") or imgs.get("2000") or imgs.get("500")
    if url and job["status"]["cover"] != "ok":
        res = check_cover(job, url, f"https://www.audible.de/pd/{p.get('asin')}", "audible")
        job["status"]["cover"] = "ok" if res["ok"] else "pending"
    if not P["series"]:                  # "Autor - Reihe - 03 - Titel" im Elternordner
        parent = PureWindowsPath(job["ordner"]).parent.name
        pp = IDT.parse_folder({"folder_name": parent, "relative_path": str(PureWindowsPath(job["ordner"]).parent),
                               "title_guess": None})
        if pp["series"] and pp["number"] and M.text_similarity(pp["title"], P["title"]) >= 0.8 \
                and M.text_similarity(pp["series"], (P["authors"] or [""])[0]) < 0.8:
            P["series"], P["series_number"] = pp["series"], pp["number"]
            log(job, f"reihe aus Elternordner: {pp['series']} {pp['number']}")
    log(job, f"audible {via} {p.get('asin')} score={score}")


def run_audible(job: dict) -> str:
    g = IDT.parse_folder({"folder_name": job["folder_name"], "relative_path": job["ordner"],
                          "title_guess": job["guess"]["title"]})
    tries = [g]
    if g["series"] and g["title"] and g["series"] != g["author"]:
        tries.append({**g, "author": ""})
    if job["guess"]["title"] and IDT.clean(job["guess"]["title"]) != g["title"]:
        tries.append({**g, "title": IDT.clean(job["guess"]["title"])})
    hit = None
    for cand in tries:
        for t in dict.fromkeys(x for x in [cand["title"], *_variants(cand["title"])] if x):
            hit = IDT.best_audible({**cand, "title": t})
            if hit:
                break
        if hit:
            break
    job["audible_versucht"] = True
    job["parse"] = g
    if not hit:
        log(job, "audible: kein Treffer")
        return "kein"
    p = audible_product(hit["asin"])
    if not p:
        log(job, f"audible: Produkt {hit['asin']} nicht ladbar")
        return "fehler"
    fill_from_product(job, p, hit["score"], "suche")
    job["status"]["identitaet"] = "ok" if hit["score"] >= 0.9 else "pruefen"
    return job["status"]["identitaet"]


def _variants(t: str) -> list[str]:
    out = []
    v = re.split(r"\s+:\s+|\s+-\s+", t or "")
    if len(v) > 1:
        out += [v[0], v[-1]]
    w = re.sub(r"(?i)\b(ungek(ü|ue)rzt|gek(ü|ue)rzt|h(ö|oe)rbuch|h(ö|oe)rspiel|mp3|cd)\b", "", t or "")
    out.append(re.sub(r"\s{2,}", " ", w).strip(" -,."))
    return [x for x in out if x and len(x) >= 3]


# ---------------------------------------------------------------------------
# Cover
# ---------------------------------------------------------------------------

def check_cover(job: dict, url: str, page: str | None, quelle: str) -> dict:
    if "/images/G/" in url or "Placement" in url:
        return {"ok": False, "why": "Platzhalterbild"}
    try:
        raw = M.download_bytes(url)
        with Image.open(io.BytesIO(raw)) as im:
            im.load()
            w, h = im.size
            pic = im.convert("RGB")
    except Exception as exc:
        return {"ok": False, "why": f"Download/Bild: {type(exc).__name__}"}
    if min(w, h) < 300:
        return {"ok": False, "why": f"zu klein {w}x{h}"}
    if w / h > 1.3 or h / w > 1.8:
        return {"ok": False, "why": f"falsches Format {w}x{h}"}
    pic.thumbnail((240, 240))
    pic.save(STORE.thumbs / f"{job['id']}.jpg", "JPEG", quality=85)
    job["p"]["cover"] = {"url": url, "page_url": page, "w": w, "h": h, "quelle": quelle}
    return {"ok": True, "w": w, "h": h}


# ---------------------------------------------------------------------------
# Befehle
# ---------------------------------------------------------------------------

def cmd_init(a) -> int:
    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute("""SELECT b.* FROM books b
                          WHERE NOT EXISTS (SELECT 1 FROM book_enrichment e WHERE e.book_id = b.id)
                          ORDER BY b.relative_path COLLATE NOCASE""").fetchall()
    kats = {k.casefold() for k in (a.kategorie or [])}
    neu = vorhanden = 0
    for r in rows:
        kat = (PureWindowsPath(r["relative_path"] or "").parts or [""])[0]
        if kats and kat.casefold() not in kats:
            continue
        if STORE.path(r["id"]).is_file():
            vorhanden += 1
            continue
        if a.limit and neu >= a.limit:
            break
        STORE.save(new_job(r))
        neu += 1
    print(json.dumps({"neu": neu, "schon_vorhanden": vorhanden}))
    return 0


def cmd_index(a) -> int:
    idx = json.loads(Path(a.datei).read_text(encoding="utf-8"))
    done = {int(k): v for k, v in (idx.get("abgeliefert") or {}).items()}
    n = 0
    for bid, teil in done.items():
        if STORE.path(bid).is_file():
            job = STORE.load(bid)
            if not job.get("abgeliefert"):
                job["abgeliefert"] = teil
                STORE.save(job)
                n += 1
    print(json.dumps({"als_erledigt_markiert": n, "im_index": len(done)}))
    return 0


def cmd_audible(a) -> int:
    ids = set(a.ids or [])
    cnt = collections.Counter()
    todo = [j for j in STORE.all() if not j.get("abgeliefert") and (j["id"] in ids if ids else not j["audible_versucht"])]
    if a.limit:
        todo = todo[: a.limit]
    t0 = time.time()
    for i, job in enumerate(todo, 1):
        try:
            cnt[run_audible(job)] += 1
        except Exception as exc:
            log(job, f"audible: Ausnahme {type(exc).__name__}: {exc}")
            cnt["fehler"] += 1
        STORE.save(job)
        if i % 25 == 0:
            print(f"  {i}/{len(todo)}  {dict(cnt)}  {time.time() - t0:.0f}s", flush=True)
    print(json.dumps({"bearbeitet": len(todo), **cnt}))
    return 0


def brief(job: dict) -> dict:
    P = job["p"]
    return {"id": job["id"], "kat": job["kategorie"], "ordner": job["folder_name"],
            "parent": PureWindowsPath(job["ordner"]).parent.name,
            "status": {k: v for k, v in job["status"].items() if v != "ok"},
            "jetzt": {k: P[k] for k in ("title", "authors", "series", "series_number", "asin") if P[k]},
            "desc_len": len(P["description"] or ""), "cover": bool(P["cover"]), "audio": job["audio_count"],
            "mb": job["mb"]}


def cmd_offen(a) -> int:
    out = [j for j in STORE.all()
           if not j.get("abgeliefert") and not terminal(j)
           and (not a.kategorie or j["kategorie"].casefold() == a.kategorie.casefold())]
    for j in out[: a.n]:
        print(json.dumps(brief(j), ensure_ascii=False))
    print(json.dumps({"offen_gesamt": len(out)}))
    return 0


def cmd_zeige(a) -> int:
    j = STORE.load(a.id)
    j = {k: v for k, v in j.items() if k not in ("log",)}
    if j["p"].get("description"):
        j["p"]["description"] = j["p"]["description"][:300] + ("…" if len(j["p"]["description"]) > 300 else "")
    print(json.dumps(j, ensure_ascii=False))
    return 0


SET_FIELDS = set(new_job({"id": 0, "fingerprint": "", "relative_path": "", "folder_name": "", "author_guess": None,
                          "title_guess": None, "volume_guess": None, "audio_count": 0, "total_bytes": 0})["p"])


def cmd_setze(a) -> int:
    job = STORE.load(a.id)
    data = json.loads(Path(a.datei).read_text(encoding="utf-8") if a.datei else a.json)
    status = data.pop("status", {}) or {}
    unknown = [k for k in data if k not in SET_FIELDS]
    if unknown:
        sys.exit(f"[FEHLER] unbekannte Felder: {unknown}  erlaubt: {sorted(SET_FIELDS)}")
    if "authors" in data and isinstance(data["authors"], str):
        data["authors"] = [data["authors"]]
    if "cover" in data:
        sys.exit("[FEHLER] Cover bitte mit dem Befehl 'cover' setzen (prueft das Bild)")
    if data.get("description") is not None:
        d = data["description"].strip()
        if len(d) < 200:
            sys.exit(f"[FEHLER] Beschreibung zu kurz ({len(d)} Zeichen, mind. 200)")
        data["description"] = d
        data.setdefault("description_source", "agent")
    for src in data.pop("sources", []) or []:
        if src not in job["p"]["sources"]:
            job["p"]["sources"].append(src)
    job["p"].update(data)
    for k, v in status.items():
        if k not in KATS or v not in STATI:
            sys.exit(f"[FEHLER] Status {k}={v} ungueltig")
        job["status"][k] = v
    if job["status"]["beschreibung"] == "ok" and not job["p"]["description"]:
        sys.exit("[FEHLER] beschreibung=ok ohne Text")
    if job["status"]["identitaet"] == "ok" and not (job["p"]["title"] and job["p"]["authors"]):
        sys.exit("[FEHLER] identitaet=ok braucht title und authors")
    log(job, "setze " + ",".join(list(data) + [f"{k}={v}" for k, v in status.items()]))
    STORE.save(job)
    print(json.dumps(brief(job), ensure_ascii=False))
    return 0


def cmd_cover(a) -> int:
    job = STORE.load(a.id)
    res = check_cover(job, a.url, a.seite, a.quelle)
    job["status"]["cover"] = "ok" if res["ok"] else job["status"]["cover"]
    log(job, f"cover {a.url[:80]} -> {res}")
    STORE.save(job)
    print(json.dumps({"id": job["id"], **res}))
    return 0 if res["ok"] else 1


def cmd_asin(a) -> int:
    job = STORE.load(a.id)
    p = audible_product(a.asin)
    if not p:
        sys.exit(f"[FEHLER] ASIN {a.asin} nicht gefunden")
    ref = [job["guess"]["title"], job["folder_name"], (job.get("parse") or {}).get("title")]
    tsim = max(M.text_similarity(r, v) for r in ref if r
               for v in (p.get("title"), f"{p.get('title')} {p.get('subtitle') or ''}") if v)
    if tsim < 0.6 and not a.erzwingen:
        print(json.dumps({"abgelehnt": True, "audible_titel": p.get("title"), "untertitel": p.get("subtitle"),
                          "autoren": M.audible_names(p.get("authors")), "titel_aehnlichkeit": round(tsim, 2),
                          "hinweis": "passt nicht zum Ordnernamen; nichts geaendert (--erzwingen, wenn sicher)"},
                         ensure_ascii=False))
        return 1
    fill_from_product(job, p, 1.0, "fest")
    job["status"]["identitaet"] = "ok"
    STORE.save(job)
    print(json.dumps(brief(job), ensure_ascii=False))
    return 0


def cmd_blatt(a) -> int:
    jobs = [j for j in STORE.all() if j["p"].get("cover") and (STORE.thumbs / f"{j['id']}.jpg").is_file()]
    if a.ids:
        want = set(a.ids)
        jobs = [j for j in jobs if j["id"] in want]
    else:
        jobs = [j for j in jobs if not j.get("abgeliefert") and not j.get("blatt_gesehen")][: a.n]
    if not jobs:
        print(json.dumps({"bilder": 0}))
        return 0
    cols, cw, ch = 8, 240, 300
    rows = (len(jobs) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * cw, rows * ch), "white")
    d = ImageDraw.Draw(sheet)
    font = None
    for f in ("DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "arial.ttf"):
        try:
            font = ImageFont.truetype(f, 12)
            break
        except OSError:
            continue
    font = font or ImageFont.load_default()
    for i, j in enumerate(jobs):
        x, y = (i % cols) * cw, (i // cols) * ch
        with Image.open(STORE.thumbs / f"{j['id']}.jpg") as t:
            sheet.paste(t, (x + (cw - t.width) // 2, y + 2))
        P = j["p"]
        d.text((x + 4, y + 246), f"#{j['id']} {(P['title'] or j['folder_name'])[:34]}", fill="black", font=font)
        d.text((x + 4, y + 262), f"{', '.join(P['authors'][:1])[:36]}", fill="gray", font=font)
        d.text((x + 4, y + 278), f"{P['cover']['w']}x{P['cover']['h']} {P['cover']['quelle']}", fill="gray", font=font)
        if not a.ids:
            j["blatt_gesehen"] = True
            STORE.save(j)
    sheet.save(a.out, "JPEG", quality=80)
    print(json.dumps({"bilder": len(jobs), "datei": a.out, "ids": [j["id"] for j in jobs]}))
    return 0


def cmd_stand(a) -> int:
    jobs = STORE.all()
    per = collections.defaultdict(collections.Counter)
    for j in jobs:
        c = per[j["kategorie"]]
        c["jobs"] += 1
        c["abgeliefert"] += bool(j.get("abgeliefert"))
        c["fertig"] += terminal(j)
        for k in KATS:
            c[f"{k}_{j['status'][k]}"] += 1
    print(json.dumps({k: dict(v) for k, v in per.items()}, ensure_ascii=False, indent=1))
    return 0


def cmd_paket(a) -> int:
    out = Path(a.out)
    teil = out.name
    ready = [j for j in STORE.all() if terminal(j) and not j.get("abgeliefert")]
    if a.max:
        ready = ready[: a.max]
    if not ready:
        print(json.dumps({"jobs": 0}))
        return 0
    def slim(j: dict) -> dict:
        d = {k: j[k] for k in ("id", "fp", "kategorie", "ordner", "status")}
        d["p"] = dict(j["p"])
        if d["p"].get("description_source") == "audible.de" and d["p"].get("asin"):
            d["p"]["description"] = None   # Import holt den Audible-Text selbst ueber die ASIN (spart Upload)
        d["p"]["sources"] = [x for x in d["p"]["sources"] if "audible.de/pd/" not in x.get("url", "")]
        if d["p"].get("cover"):
            d["p"]["cover"] = {k: v for k, v in d["p"]["cover"].items() if k in ("url", "quelle", "page_url")
                               and not (k == "page_url" and "audible.de/pd/" in (v or ""))}
        d["p"] = {k: v for k, v in d["p"].items() if v not in (None, [], "")}
        return d
    payload = {"format": "hoerbuch-cloud-teil-1", "erstellt": now(), "jobs": [slim(j) for j in ready]}
    out.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    idx_file = STORE.work / "index.json"
    idx = json.loads(idx_file.read_text(encoding="utf-8")) if idx_file.is_file() else {"abgeliefert": {}}
    for j in ready:
        j["abgeliefert"] = teil
        STORE.save(j)
        idx["abgeliefert"][str(j["id"])] = teil
    idx["stand"] = now()
    idx_file.write_text(json.dumps(idx, separators=(",", ":")), encoding="utf-8")
    up = {}
    for f in (out, idx_file):
        raw = f.read_bytes()
        u = upload_safe(raw.decode("utf-8"))
        assert json.loads(u) == json.loads(raw), "Upload-Fassung weicht inhaltlich ab"
        uf = f.with_name(f.stem + ".up.json")
        uf.write_text(u, encoding="utf-8")
        up[f.name] = {"upload": str(uf), "soll_bytes": len(raw)}
    st = collections.Counter(f"{k}_{j['status'][k]}" for j in ready for k in KATS)
    print(json.dumps({"jobs": len(ready), "datei": str(out), "bytes": out.stat().st_size,
                      "index": str(idx_file), "index_ids": len(idx["abgeliefert"]), "upload": up, **st}))
    return 0


def upload_safe(s: str) -> str:
    """Zeichen, die beim Durchreichen an create_file veraendert werden koennen (NBSP, kombinierende Zeichen,
    nicht-NFC, Steuer-/Formatzeichen), als JSON-\\u-Escape schreiben. Inhalt bleibt identisch, die Datei wird
    nach dem Hochladen exakt so gross wie das Original."""
    import unicodedata
    def bad(c: str) -> bool:
        return (unicodedata.combining(c) or ord(c) < 32 or unicodedata.normalize("NFC", c) != c
                or (c != " " and unicodedata.category(c) in ("Zs", "Zl", "Zp", "Cf", "Cc", "Co", "Cn")))
    return "".join(("\\u%04x" % ord(c)) if bad(c) else c for c in s)


def _aud_query(params: dict) -> list[dict]:
    params = {**params, "num_results": "8", "products_sort_by": "Relevance",
              "response_groups": "product_attrs,contributors,series,product_desc"}
    url = f"{M.AUDIBLE_ENDPOINT}?" + urllib.parse.urlencode(params)
    try:
        return M.http_json(url).get("products") or []
    except Exception as exc:
        print("  FEHLER", exc); return []


def cmd_suche(a) -> int:
    if a.titel:
        prods = _aud_query({"title": a.titel, **({"author": a.autor} if a.autor else {})})
    else:
        prods = _aud_query({"keywords": " ".join(a.worte)})
    for p in prods:
        if is_placeholder(p):
            continue
        au = ", ".join(x.get("name", "") for x in p.get("authors") or [])[:50]
        s = p.get("series") or []
        s = f" [{s[0].get('title')} {s[0].get('sequence')}]" if s else ""
        print(f"  {p['asin']} | {p.get('title')}{s} | {au} | {p.get('release_date')} | {p.get('language')} | {p.get('format_type')}")
    return 0


def cmd_serie(a) -> int:
    asin = a.asin
    if not a.parent:
        p = M.http_json(f"{M.AUDIBLE_ENDPOINT}/{asin}?response_groups=relationships").get("product") or {}
        par = [r for r in p.get("relationships") or [] if r.get("relationship_to_product") == "parent"
               and r.get("relationship_type") == "series"]
        if not par:
            print("keine Serie gefunden"); return 1
        asin = par[0]["asin"]
    par = M.http_json(f"{M.AUDIBLE_ENDPOINT}/{asin}?response_groups=relationships,product_attrs").get("product") or {}
    kids = [r for r in par.get("relationships") or [] if r.get("relationship_to_product") == "child"]
    info: dict[str, dict] = {}
    asins = [k["asin"] for k in kids]
    for i in range(0, len(asins), 40):
        q = urllib.parse.urlencode({"asins": ",".join(asins[i:i + 40]), "response_groups": "product_attrs,product_desc"})
        for p in M.http_json(f"{M.AUDIBLE_ENDPOINT}?{q}").get("products") or []:
            info[p["asin"]] = p
    print(f"# Serie {asin}: {len(kids)} Eintraege")
    for k in sorted(kids, key=lambda r: float(r.get("sort") or 0)):
        p = info.get(k["asin"], {})
        mark = "  (Platzhalter)" if p and is_placeholder(p) else ""
        print(f"{k.get('sequence')} | {k['asin']} | {(p.get('title') or '')[:70]} | {p.get('release_date')}{mark}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", default="work")
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("init"); p.add_argument("--db", required=True); p.add_argument("--kategorie", nargs="*")
    p.add_argument("--limit", type=int)
    p = sp.add_parser("index"); p.add_argument("datei")
    p = sp.add_parser("audible"); p.add_argument("--limit", type=int); p.add_argument("--ids", type=int, nargs="*")
    p = sp.add_parser("offen"); p.add_argument("--n", type=int, default=10); p.add_argument("--kategorie")
    p = sp.add_parser("zeige"); p.add_argument("id", type=int)
    p = sp.add_parser("setze"); p.add_argument("id", type=int); p.add_argument("--json"); p.add_argument("--datei")
    p = sp.add_parser("cover"); p.add_argument("id", type=int); p.add_argument("url")
    p.add_argument("--seite"); p.add_argument("--quelle", default="web")
    p = sp.add_parser("asin"); p.add_argument("id", type=int); p.add_argument("asin"); p.add_argument("--erzwingen", action="store_true")
    p = sp.add_parser("blatt"); p.add_argument("--ids", type=int, nargs="*"); p.add_argument("--n", type=int, default=40)
    p.add_argument("--out", required=True)
    sp.add_parser("stand")
    p = sp.add_parser("paket"); p.add_argument("--out", required=True); p.add_argument("--max", type=int, default=35)
    p = sp.add_parser("suche"); p.add_argument("-t", "--titel"); p.add_argument("-a", "--autor"); p.add_argument("worte", nargs="*")
    p = sp.add_parser("serie"); p.add_argument("asin"); p.add_argument("--parent", action="store_true")
    a = ap.parse_args()
    global STORE
    STORE = Store(Path(a.work))
    return globals()[f"cmd_{a.cmd}"](a)


if __name__ == "__main__":
    sys.exit(main())
