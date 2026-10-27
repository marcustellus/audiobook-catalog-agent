#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
hoerbuch_identitaet_v0_1.py - korrigiert NUR Titel, Autor, Reihe und Band

Ersatz fuer hoerbuch_nachrecherche_v0_1.py, wenn nur die Identitaet eines Buchs kaputt
ist (Autor aus dem Ordnernamen geraten, "Perry Rhodan 3196" als Autor, Uebersetzer im
Autorenfeld ...). Beschreibung, Klassifikation, Bewertungen und Cover bleiben unangetastet.

Pro Buch:
  1. Ordnername zerlegen (Autor / Reihe / Band / Titel, Zusaetze wie "(Ungekuerzt)" weg)
  2. Audible.de fragen (schnell, kostenlos) - passt der Treffer, ist das Buch fertig
  3. sonst EINE OpenAI-Websuche nur nach Titel/Autor/Reihe/Band
Kein Calibre, keine Beschreibung, keine Semantik, kein Cover, kein Neustart pro Buch.

Ablauf (wie beim Reihen-Skript):
  1. Vorschlag:  python .\hoerbuch_identitaet_v0_1.py --db .\hoerbuch_katalog_clean.sqlite3 --category "FunTasy"
                 -> identitaet_vorschlag.csv, an der DB wird nichts geaendert
  2. Pruefen:    CSV in LibreOffice/Excel oeffnen (Semikolon, UTF-8). Spalte "uebernehmen"
                 auf ja/nein, Werte in den *_neu-Spalten bei Bedarf korrigieren, speichern.
  3. Uebernehmen (GUI zu, DB sichern):
                 python .\hoerbuch_identitaet_v0_1.py --db .\hoerbuch_katalog_clean.sqlite3 --apply identitaet_vorschlag.csv

Bereits recherchierte Antworten liegen in identitaet_cache.jsonl; ein erneuter Vorschlag
fuer dieselben Buecher kostet nichts (--neu fragt trotzdem neu).
Uebernommen wird als neue Zeile in book_enrichment (Kopie der letzten, vier Felder ersetzt).
Festgelegte Reihen (series_overrides) behalten Vorrang.

Optionen: --book-id N (mehrfach), --limit N, --no-web (nur Audible), --workers N,
          --model NAME, --env PFAD, --alle (alle recherchierten Buecher der Kategorie, nicht nur verdaechtige)
Braucht hoerbuch_metadata_v0_5_5.py im selben Ordner (fuer Audible-Abfrage und OpenAI-Zugang).
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import re
import sqlite3
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PureWindowsPath

HERE = Path(__file__).resolve().parent
META_FILE = HERE / "hoerbuch_metadata_v0_5_5.py"
RUN_MODEL = "identitaet-v0.1"


def load_meta():
    if not META_FILE.is_file():
        sys.exit(f"[FEHLER] {META_FILE.name} fehlt im Ordner {HERE}")
    spec = importlib.util.spec_from_file_location("hb_meta", META_FILE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hb_meta"] = mod          # noetig fuer die Dataclasses im Modul
    spec.loader.exec_module(mod)  # main() ist geschuetzt, Import hat keine Nebenwirkungen
    return mod


M = load_meta()

# ---------------------------------------------------------------------------
# Auswahl
# ---------------------------------------------------------------------------

JUNK = {"unbekannt", "unknown", "anonym", "anonymous", "diverse", "various", "verschiedene"}
SUSPECT_AUTHOR = re.compile(
    r"\d|\b(band|folge|teil|vol|episode|hörspiel|hoerspiel|übersetz|uebersetz|translator|"
    r"sprecher|narrator|herausgeber|hrsg|illustrat)\b|perry rhodan|horus heresy|^atlan$",
    re.I)


def why_suspect(prov: dict, authors: list[str]) -> list[str]:
    why = []
    if prov.get("authors") == "scanner":
        why.append("Autor aus Ordnername")
    if prov.get("canonical_title") == "scanner":
        why.append("Titel aus Ordnername")
    if not authors:
        why.append("kein Autor")
    if any(a.strip().casefold() in JUNK for a in authors):
        why.append("Platzhalter-Autor")
    if any(SUSPECT_AUTHOR.search(a) for a in authors):
        why.append("Autor verdaechtig")
    return why


def load_candidates(con, args) -> list[dict]:
    rows = con.execute(
        """
        SELECT b.id, b.relative_path, b.folder_name, b.author_guess, b.title_guess, b.volume_guess,
               be.id AS enr_id, be.canonical_title, be.authors_json, be.series, be.series_number,
               be.provenance_json, so.series AS fix_series, so.series_number AS fix_number
        FROM book_enrichment be
        JOIN (SELECT book_id, MAX(id) m FROM book_enrichment GROUP BY book_id) x ON x.m = be.id
        JOIN books b ON b.id = be.book_id
        LEFT JOIN series_overrides so ON so.book_id = b.id
        ORDER BY b.relative_path COLLATE NOCASE
        """).fetchall()
    scope = (args.category or "").replace("/", "\\").strip("\\").casefold()
    out = []
    for r in rows:
        rel = (r["relative_path"] or "").replace("/", "\\").casefold()
        if scope and not (rel == scope or rel.startswith(scope + "\\")):
            continue
        if args.book_id and r["id"] not in args.book_id:
            continue
        try:
            prov = json.loads(r["provenance_json"] or "{}")
        except ValueError:
            prov = {}
        authors = [str(a) for a in M.json_list(r["authors_json"])]
        why = why_suspect(prov, authors)
        if not why and not (args.alle or args.book_id):
            continue
        d = dict(r)
        d["authors"] = authors
        d["why"] = why
        out.append(d)
    return out[: args.limit] if args.limit else out


# ---------------------------------------------------------------------------
# Ordnername zerlegen
# ---------------------------------------------------------------------------

NOISE = re.compile(
    r"(?ix)"
    r"\(\s*(?:un)?gek(?:ü|ue)rzt\s*\)|\bun?gek(?:ü|ue)rzt\b|\(mp3@?\d*\)|\(mp3[^)]*\)|"
    r"\(\s*hsp[^)]*\)?|\bh(?:ö|oe)rspiel\s+swr\d?\b|\(\s*(?:19|20)\d\d\s*\)|\(\s*h(?:ö|oe)rbuch\s*\)|"
    r"\(german edition\)")
NUM_TAIL = re.compile(r"^(.*?\S)\s*(?:[-#]|band|folge|teil|nr\.?)?\s*0*(\d{1,4})$", re.I)
NUM_HEAD = re.compile(r"^0*(\d{1,4})\s*[-_.]?\s+(.*)$")
ONLY_NUM = re.compile(r"^(?:band|folge|teil|nr\.?|#)?\s*0*(\d{1,4})$", re.I)
PAREN_SERIES = re.compile(r"\(([^()]*?\D)\s+0*(\d{1,4})\)\s*$")


def clean(s: str | None) -> str:
    s = (s or "").replace("_", " ")
    s = NOISE.sub(" ", s)
    s = re.sub(r"\(\s*\)", " ", s)
    s = re.sub(r"\s{2,}", " ", s)
    return s.strip(" -.,")


def parse_folder(book: dict) -> dict:
    """liefert Kandidaten: author, series, number, title (alles ggf. leer)."""
    name = clean(book["folder_name"])
    parts = [p.strip() for p in re.split(r"\s+-\s+", name) if p.strip()]
    res = {"author": "", "series": "", "number": "", "title": ""}
    if not parts:
        return res

    first = parts[0]
    m = ONLY_NUM.match(first)            # "Band 02 - Nahende Finsternis": Autor/Reihe stehen im Elternordner
    if m and len(parts) > 1:
        res["number"] = m.group(1)
        parent = clean(re.sub(r"\((?:band|folge|teil)?\s*\d+\s*-\s*\d+\)", "",
                              PureWindowsPath(book.get("relative_path") or "").parent.name, flags=re.I))
        pp = [x.strip() for x in re.split(r"\s+-\s+", parent) if x.strip()]
        res["author"] = pp[0] if pp else ""
        res["series"] = pp[1] if len(pp) > 1 else ""
        res["title"] = clean(parts[-1])
        return res
    head_num = False
    m = NUM_HEAD.match(first)            # "03 Perry Rhodan - Traversan - Die Rebellen ..."
    if m:
        res["number"], first, head_num = m.group(1), m.group(2), True
    m = NUM_TAIL.match(first)            # "Perry Rhodan 3191", "Terry Pratchet 18"
    if m and len(parts) > 1:
        first, res["number"] = m.group(1), m.group(2)
        res["series"] = first           # kann Reihe ODER Autor sein, Audible/Web klaeren das
    res["author"] = first

    rest = parts[1:]
    before, after = [], []               # Titelteile vor / nach der Band-Angabe
    seen_num = bool(res["number"])
    for i, p in enumerate(rest):
        m = ONLY_NUM.match(p)
        if m:
            res["number"] = res["number"] or m.group(1)
            seen_num = True
            continue
        m = NUM_TAIL.match(p)
        if m and i < len(rest) - 1:      # "Apokosmos 01" vor dem Titel
            res["series"], res["number"] = m.group(1), m.group(2)
            seen_num = True
            continue
        (after if seen_num else before).append(p)
    if head_num and len(after) > 1:      # "03 Perry Rhodan - Traversan - Titel": erst Reihe, dann Titel
        res["series"], title = after[0], after[1]
    elif after:                          # "Falsche Goetter - Die Haeresie ..." -> Haupttitel = erster Teil
        title = after[0]
        if before and not res["series"]:
            res["series"] = before[0]
    elif before:
        title = before[-1]
        if len(before) > 1 and not res["series"]:
            res["series"] = before[0]
    else:
        title = book["title_guess"] or name
    m = PAREN_SERIES.search(title)       # "Es ist was faul (Thursday Next 4)"
    if m:
        res["series"], res["number"] = res["series"] or m.group(1).strip(), res["number"] or m.group(2)
        title = title[: m.start()].strip()
    res["title"] = clean(title)
    return res


# ---------------------------------------------------------------------------
# Audible
# ---------------------------------------------------------------------------

ROLE = re.compile(r"\s*[-–(,]\s*(übersetz|uebersetz|translator|sprecher|narrator|herausgeber|hrsg|"
                  r"illustrat|vorwort|nachwort)[^)]*\)?\s*$", re.I)
_audible_lock = threading.Lock()
_audible_last = [0.0]


def audible_query(params: dict) -> list[dict]:
    base = {"num_results": "15", "products_sort_by": "Relevance",
            "response_groups": "contributors,product_desc,product_attrs,series"}
    url = M.AUDIBLE_ENDPOINT + "?" + urllib.parse.urlencode({**base, **params})
    with _audible_lock:                   # hoeflich bleiben: max. ~4 Anfragen/s
        wait = 0.25 - (time.time() - _audible_last[0])
        if wait > 0:
            time.sleep(wait)
        _audible_last[0] = time.time()
    try:
        prods = M.http_json(url).get("products") or []
    except Exception:
        return []
    return [p for p in prods if isinstance(p, dict)]


def clean_people(names: list[str]) -> list[str]:
    out = []
    for n in names:
        if ROLE.search(n):
            continue
        n = n.strip()
        if n and n not in out:
            out.append(n)
    return out


BRACKETS = re.compile(r"\s*\[[^\]]*\]|\s*\((?:german edition|ungek(?:ü|ue)rzt)[^)]*\)", re.I)
TITLE_TAIL = re.compile(r"\s*:\s*(?:ein|eine|der|die|das)?\s*[^:]*\b(roman|litrpg|h(?:ö|oe)rspiel|h(?:ö|oe)rbuch|saga|"
                        r"novel|thriller|krimi|fantasy)\b[^:]*$", re.I)
BUNDLE = re.compile(r"\s/\s|\b(bundle|sammelband|gesamtausgabe|box|paket|sammelbox)\b", re.I)
DOT_TAIL = re.compile(r"\.\s+(?:ein|eine)\s+[^.]*\b(roman|m(?:ä|ae)rchen|h(?:ö|oe)rspiel|erz(?:ä|ae)hlung|geschichte)\b.*$", re.I)
SERIES_RANGE = re.compile(r"\s+\d+\s*-\s*\d+$")


def _num_in(num: str, *texts) -> bool:
    if not num:
        return False
    pat = re.compile(rf"(?<!\d)0*{int(num)}(?!\d)")
    return any(t and pat.search(str(t)) for t in texts)


def tidy_title(t: str, series: str | None) -> str:
    t = BRACKETS.sub("", t or "").strip()
    t = TITLE_TAIL.sub("", t).strip()
    t = DOT_TAIL.sub("", t).strip()
    if series:                            # "Die dunklen Faelle des Harry Dresden - Sturmnacht" -> "Sturmnacht"
        m = re.match(rf"^{re.escape(series)}\s*\d*\s*[-:–]\s*(.+)$", t, re.I)
        if m:
            t = m.group(1).strip()
    return t


def best_audible(cand: dict) -> dict | None:
    title, author, num = cand["title"], cand["author"], cand["number"]
    if not title or len(title) < 3 or ONLY_NUM.match(title):   # "1" ist kein Titel -> Websuche
        return None
    queries = []
    if author:
        queries.append({"title": title, "author": author})
    queries.append({"title": title})
    best, seen = None, set()
    for q in queries:
        for p in audible_query(q):
            if p.get("asin") in seen:
                continue
            seen.add(p.get("asin"))
            if str(p.get("language") or "").casefold() not in {"german", "de", "deu", "deutsch"}:
                continue
            raw_title = BRACKETS.sub("", str(p.get("title") or "")).strip()
            if BUNDLE.search(raw_title) and not BUNDLE.search(title):
                continue                  # Sammelausgabe ist kein Treffer fuer einen Einzelband
            psub = str(p.get("subtitle") or "")
            pser, pseq = M.audible_series(p.get("series"))
            pser = BRACKETS.sub("", pser or "").strip() or None
            # Titelvergleich: ganzer Titel, Teil vor " - " / ":", mit Untertitel
            variants = {raw_title, re.split(r"\s+-\s+|:\s", raw_title)[0], f"{raw_title} {psub}",
                        tidy_title(raw_title, pser)}
            tsim = max(M.text_similarity(title, v) for v in variants if v)
            if tsim < 0.85:
                continue
            if num and pseq and _num_in(num, pseq) is False and re.fullmatch(r"\d+", pseq or ""):
                continue                  # anderer Band derselben Reihe
            pauth = clean_people(M.audible_names(p.get("authors")))
            pauth = [a for a in pauth if not (pser and M.text_similarity(a, pser) >= 0.8)]
            if not pauth:
                continue
            asim = max(M.text_similarity(author, a) for a in pauth) if author else 0.0
            author_ok = asim >= 0.85 or (tsim >= 0.98 and asim >= 0.6)   # "Aleksej Pechov" ~ "Alexey Pehov"
            num_ok = _num_in(num, pseq, psub, raw_title)
            if not (author_ok or num_ok):
                continue
            if not author_ok and tsim < 0.95:     # nur die Nummer bestaetigt: Titel muss praktisch exakt sein
                continue
            score = round(0.6 * tsim + 0.25 * author_ok + 0.15 * num_ok + (0.15 if not num else 0), 3)
            if best is None or score > best["score"]:
                series = SERIES_RANGE.sub("", pser or "").strip() or (cand["series"] or None)
                number = pseq if pseq else (num or None)
                best = {"score": min(score, 1.0), "title": tidy_title(raw_title, series),
                        "authors": pauth, "series": series, "number": number, "asin": p.get("asin")}
        if best and best["score"] >= 0.95:
            break
    return best


# ---------------------------------------------------------------------------
# Websuche (eine Abfrage)
# ---------------------------------------------------------------------------

WEB_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "matched": {"type": "boolean"},
        "confidence": {"type": "number"},
        "title": {"type": ["string", "null"]},
        "authors": {"type": "array", "items": {"type": "string"}},
        "series": {"type": ["string", "null"]},
        "series_number": {"type": ["string", "null"]},
        "note": {"type": ["string", "null"]},
    },
    "required": ["matched", "confidence", "title", "authors", "series", "series_number", "note"],
}


def web_prompt(book: dict, cand: dict) -> str:
    return f"""Bestimme die bibliografische Identitaet eines deutschsprachigen Hoerbuchs/Hoerspiels.
Gesucht sind NUR: Werktitel, Autor(en), Reihe und Band-/Folgennummer.

Ordnerpfad: {book['relative_path']}
Ordnername: {book['folder_name']}
Vorzerlegung (kann falsch sein): Autor/Reihe="{cand['author']}", Reihe="{cand['series']}", Nummer="{cand['number']}", Titel="{cand['title']}"

Regeln:
- Der linke Teil des Ordnernamens kann Autor ODER Reihe sein (z. B. "Perry Rhodan 3191" ist Reihe + Heftnummer,
  der Autor ist der jeweilige Heftautor; "Terry Pratchet 18" ist Autor + Scheibenwelt-Band).
- Autoren nur als echte Personen-/Autorennamen, ohne Uebersetzer, Sprecher, Herausgeberzusatz.
- Titel ohne Reihenname, Bandnummer und Zusaetze wie "(Ungekuerzt)".
- Reihe mit dem gebraeuchlichen deutschen Namen, Nummer nur die Zahl.
- Nichts erzwingen: bei Unsicherheit matched=false und niedrige confidence.
"""


def ask_web(client, model: str, book: dict, cand: dict) -> dict | None:
    resp = client.responses.create(
        model=model,
        reasoning={"effort": "low"},
        tools=[{"type": "web_search"}],
        tool_choice="required",
        input=web_prompt(book, cand),
        text={"format": {"type": "json_schema", "name": "identitaet", "strict": True, "schema": WEB_SCHEMA}},
    )
    data = json.loads(resp.output_text or "{}")
    data["authors"] = clean_people([str(a) for a in data.get("authors") or []])
    return data


# ---------------------------------------------------------------------------
# Vorschlag
# ---------------------------------------------------------------------------

CSV_COLS = ["book_id", "uebernehmen", "quelle", "konfidenz", "grund",
            "titel_alt", "autor_alt", "reihe_alt", "band_alt",
            "titel_neu", "autor_neu", "reihe_neu", "band_neu", "reihe_festgelegt", "ordner"]


def resolve(book: dict, client, args, cache: dict) -> dict:
    key = str(book["id"])
    if key in cache and not args.neu:
        return cache[key]
    cand = parse_folder(book)
    res = {"book_id": book["id"], "folder": book["folder_name"], "cand": cand}
    a = best_audible(cand)
    if a:
        res.update(source="audible", conf=round(a["score"], 3), title=a["title"], authors=a["authors"],
                   series=a["series"], number=a["number"], note=a["asin"])
    elif client is not None and not args.no_web:
        try:
            w = ask_web(client, args.model, book, cand)
            res.update(source="web", conf=round(float(w.get("confidence") or 0), 3),
                       title=w.get("title"), authors=w.get("authors") or [],
                       series=w.get("series"),
                       number=(w.get("series_number") if str(w.get("series_number") or "0").strip("0 ") else None)
                       or cand["number"] or None,
                       note=w.get("note"), matched=bool(w.get("matched")))
        except Exception as exc:
            res.update(source="fehler", conf=0, note=f"{type(exc).__name__}: {exc}")
    else:
        res.update(source="ordnername", conf=0.5, title=cand["title"],
                   authors=[cand["author"]] if cand["author"] else [],
                   series=cand["series"], number=cand["number"])
    return res


def propose(con, args) -> int:
    books = load_candidates(con, args)
    print(f"{len(books)} Buecher mit verdaechtiger Identitaet.")
    if not books:
        return 0
    cache_path = Path(args.cache)
    cache = {}
    if cache_path.exists():
        for line in cache_path.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
                cache[str(d["book_id"])] = d
            except (ValueError, KeyError):
                pass
    client = None
    if not args.no_web:
        client, env = M.get_openai_client(args.env)
        print(f"Websuche: {'an (' + args.model + ')' if client else 'aus (kein OpenAI-Key gefunden)'}")

    results, t0 = {}, time.time()
    stats = {"audible": 0, "web": 0, "ordnername": 0, "fehler": 0, "cache": 0}
    with ThreadPoolExecutor(max_workers=args.workers) as ex, cache_path.open("a", encoding="utf-8") as cf:
        futs = {ex.submit(resolve, b, client, args, cache): b for b in books}
        for n, fut in enumerate(futs, 1):
            b = futs[fut]
            r = fut.result()
            from_cache = str(b["id"]) in cache and not args.neu
            if from_cache:
                stats["cache"] += 1
            else:
                cf.write(json.dumps(r, ensure_ascii=False) + "\n")
                cf.flush()
            stats[r.get("source", "fehler")] = stats.get(r.get("source", "fehler"), 0) + 1
            results[b["id"]] = r
            print(f"[{n:>4}/{len(books)}] {r.get('source', '?'):10} {float(r.get('conf') or 0):.2f}  "
                  f"{' & '.join(r.get('authors') or [])[:30]:30} | {str(r.get('title') or '-')[:45]:45} | "
                  f"{r.get('series') or ''} {r.get('number') or ''}", flush=True)

    with open(args.out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(CSV_COLS)
        for b in books:
            r = results[b["id"]]
            src, conf = r.get("source"), float(r.get("conf") or 0)
            ok = (src == "audible") or (src == "web" and r.get("matched") and conf >= 0.80)
            fixed = f"{b['fix_series']} {b['fix_number'] or ''}".strip() if b["fix_series"] else ""
            w.writerow([b["id"], "ja" if ok and r.get("title") and r.get("authors") else "nein", src, f"{conf:.2f}",
                        "; ".join(b["why"]),
                        b["canonical_title"] or "", " & ".join(b["authors"]), b["series"] or "", b["series_number"] or "",
                        r.get("title") or "", " & ".join(r.get("authors") or []), r.get("series") or "",
                        r.get("number") or "", fixed, b["relative_path"]])
    print(f"\nFertig in {time.time() - t0:.0f} s. Audible {stats['audible']}, Web {stats['web']}, "
          f"nur Ordnername {stats['ordnername']}, Fehler {stats['fehler']} (davon aus Zwischenspeicher {stats['cache']}).")
    print(f"Vorschlag: {args.out}  ->  pruefen, dann mit --apply {args.out} uebernehmen.")
    return 0


# ---------------------------------------------------------------------------
# Uebernehmen
# ---------------------------------------------------------------------------

def apply(con, args) -> int:
    with open(args.apply, encoding="utf-8-sig", newline="") as f:
        sample = f.read(4096)
        f.seek(0)
        delim = ";" if sample.count(";") >= sample.count(",") else ","
        rows = [r for r in csv.DictReader(f, delimiter=delim)
                if (r.get("uebernehmen") or "").strip().casefold() in {"ja", "j", "x", "yes", "y", "1"}]
    if not rows:
        print("Keine Zeilen mit uebernehmen=ja.")
        return 0
    cols = [c[1] for c in con.execute("PRAGMA table_info(book_enrichment)")]
    copy_cols = [c for c in cols if c not in ("id", "enrichment_run_id", "created_at")]
    scan_run = con.execute("SELECT MAX(id) FROM scan_runs").fetchone()[0]
    cur = con.execute("INSERT INTO enrichment_runs(scan_run_id, model, category, requested_limit) VALUES (?,?,?,?)",
                      (scan_run, RUN_MODEL, args.category, len(rows)))
    run_id = cur.lastrowid
    done = skipped = 0
    for r in rows:
        bid = int(r["book_id"])
        last = con.execute("SELECT * FROM book_enrichment WHERE book_id=? ORDER BY id DESC LIMIT 1", (bid,)).fetchone()
        if last is None:
            print(f"  {bid}: keine Recherche vorhanden, uebersprungen")
            skipped += 1
            continue
        vals = {c: last[c] for c in copy_cols}
        title = (r.get("titel_neu") or "").strip()
        authors = [a.strip() for a in re.split(r"\s*&\s*", r.get("autor_neu") or "") if a.strip()]
        if not title or not authors:
            print(f"  {bid}: Titel oder Autor leer, uebersprungen")
            skipped += 1
            continue
        src = "identitaet:" + (r.get("quelle") or "manuell").strip()
        prov = M.json_dict(last["provenance_json"])
        vals["canonical_title"], vals["authors_json"] = title, json.dumps(authors, ensure_ascii=False)
        prov["canonical_title"] = prov["authors"] = src
        fix = con.execute("SELECT series, series_number FROM series_overrides WHERE book_id=?", (bid,)).fetchone()
        if fix:                                   # festgelegte Reihe gewinnt
            vals["series"], vals["series_number"] = fix[0], fix[1]
        else:
            vals["series"] = (r.get("reihe_neu") or "").strip() or None
            vals["series_number"] = (r.get("band_neu") or "").strip() or None
            prov["series"] = prov["series_number"] = src
        vals["provenance_json"] = json.dumps(prov, ensure_ascii=False)
        vals["matched"] = 1
        try:
            vals["confidence"] = max(float(last["confidence"] or 0), float((r.get("konfidenz") or "0").replace(",", ".")))
        except ValueError:
            pass
        con.execute(f"INSERT INTO book_enrichment(enrichment_run_id, {', '.join(copy_cols)}) "
                    f"VALUES (?, {', '.join('?' * len(copy_cols))})", [run_id] + [vals[c] for c in copy_cols])
        done += 1
    con.execute("UPDATE enrichment_runs SET finished_at=CURRENT_TIMESTAMP, processed_books=?, successful_books=?, "
                "failed_books=? WHERE id=?", (len(rows), done, skipped, run_id))
    con.commit()
    print(f"Uebernommen: {done}, uebersprungen: {skipped} (Lauf {run_id}).")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True)
    ap.add_argument("--category", help="Pfad-Praefix, z. B. 'FunTasy' oder 'SF'")
    ap.add_argument("--book-id", type=int, action="append", default=[])
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--alle", action="store_true", help="alle recherchierten Buecher, nicht nur verdaechtige")
    ap.add_argument("--no-web", action="store_true", help="nur Audible, keine OpenAI-Websuche")
    ap.add_argument("--neu", action="store_true", help="Zwischenspeicher ignorieren und neu fragen")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--model", default=M.DEFAULT_MODEL)
    ap.add_argument("--env", type=Path, default=None)
    ap.add_argument("--out", default="identitaet_vorschlag.csv")
    ap.add_argument("--cache", default="identitaet_cache.jsonl")
    ap.add_argument("--apply", metavar="CSV")
    args = ap.parse_args()
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    try:
        return apply(con, args) if args.apply else propose(con, args)
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
