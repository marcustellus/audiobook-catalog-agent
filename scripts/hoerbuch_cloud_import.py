#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
hoerbuch_cloud_import.py - uebernimmt die Ergebnisse des Cloud-Agenten (teil_*.json) in die Katalog-DB

Ablauf (GUI vorher schliessen, DB vorher sichern):
  Vorschau:     python .\hoerbuch_cloud_import.py --db .\hoerbuch_katalog_clean.sqlite3 --ordner .\ergebnis
  Uebernehmen:  dasselbe mit --apply

Was passiert je Buch:
  - nur wenn books.id UND Fingerprint passen (sonst wurde der Ordner inzwischen neu gescannt -> uebersprungen)
  - nur wenn das Buch noch keine Recherche hat (sonst uebersprungen; --ueberschreiben erzwingt eine neue Zeile)
  - neue Zeile in book_enrichment (Titel, Autoren, Reihe/Band, Sprecher, Laufzeit, Verlag, Beschreibung, Genre)
    Beschreibungen von Audible stehen nicht in der Datei, sie werden hier ueber die ASIN nachgeladen.
    series_overrides (deine festen Reihen) haben Vorrang.
  - Cover: Bild wird von der URL geladen, in _cover_storage\original|thumb|detail abgelegt und ausgewaehlt,
    aber nur wenn das Buch noch kein ausgewaehltes Cover hat. Manuelle/gesperrte Cover werden nie angetastet.
Jede teil-Datei wird nur einmal uebernommen (Merker: enrichment_runs.model = 'cloud-v1', category = Dateiname).
Braucht hoerbuch_metadata_v0_5_5.py im selben Ordner.
"""
from __future__ import annotations

import argparse
import importlib.util
import io
import json
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUN_MODEL = "cloud-v1"


def load_meta():
    p = HERE / "hoerbuch_metadata_v0_5_5.py"
    if not p.is_file():
        sys.exit(f"[FEHLER] {p.name} fehlt im Ordner {HERE}")
    spec = importlib.util.spec_from_file_location("hb_meta", p)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hb_meta"] = mod
    spec.loader.exec_module(mod)
    return mod


M = load_meta()
from PIL import Image  # noqa: E402


def audible_desc(asin: str) -> str | None:
    url = f"{M.AUDIBLE_ENDPOINT}/{asin}?response_groups=product_extended_attrs,product_desc"
    try:
        p = M.http_json(url).get("product") or {}
    except Exception:
        return None
    return M.html_to_text(p.get("publisher_summary"))


def save_cover(con, db_dir: Path, cache: Path, bid: int, c: dict) -> str:
    if con.execute("SELECT 1 FROM book_covers WHERE book_id=? AND (is_selected=1 OR locked=1 OR is_manual=1)",
                   (bid,)).fetchone():
        return "hat schon Cover"
    try:
        raw = M.download_bytes(c["url"])
        with Image.open(io.BytesIO(raw)) as im:
            im.load()
            fmt, (w, h), pic = im.format, im.size, im.convert("RGB")
    except Exception as exc:
        return f"Download fehlgeschlagen ({type(exc).__name__})"
    for d in ("original", "thumb", "detail"):
        (cache / d).mkdir(parents=True, exist_ok=True)
    stem = f"{bid}_cloud"
    orig = cache / "original" / f"{stem}{M.image_extension(fmt)}"
    thumb, detail = cache / "thumb" / f"{stem}.jpg", cache / "detail" / f"{stem}.jpg"
    orig.write_bytes(raw)
    M.canvas_contain(pic, M.THUMB_SIZE).save(thumb, "JPEG", quality=90, optimize=True)
    M.canvas_contain(pic, M.DETAIL_SIZE).save(detail, "JPEG", quality=93, optimize=True)
    con.execute(
        """INSERT INTO book_covers(book_id, research_id, source_name, source_type, source_page_url, image_url,
               local_original_path, local_thumb_path, local_detail_path, width, height, aspect_ratio,
               match_quality, quality_score, is_selected, is_manual, locked)
           VALUES (?, NULL, ?, 'cloud', ?, ?, ?, ?, ?, ?, ?, ?, 'cloud_agent', 1.0, 1, 0, 0)""",
        (bid, c.get("quelle") or "web", c.get("page_url") or "", c["url"],
         M.relpath_for_db(orig, db_dir), M.relpath_for_db(thumb, db_dir), M.relpath_for_db(detail, db_dir),
         w, h, w / h))
    return "neu"


def import_teil(con, f: Path, args, db_dir: Path, cache: Path, apply: bool) -> dict:
    data = json.loads(f.read_text(encoding="utf-8"))
    stat = {"datei": f.name, "jobs": len(data["jobs"]), "neu": 0, "uebersprungen": [], "cover": 0}
    run_id = None
    if apply:
        scan = con.execute("SELECT MAX(id) FROM scan_runs").fetchone()[0] or 1
        run_id = con.execute("INSERT INTO enrichment_runs(scan_run_id, model, category) VALUES (?,?,?)",
                             (scan, RUN_MODEL, f.name)).lastrowid
    for j in data["jobs"]:
        bid, P, st = int(j["id"]), j["p"], j["status"]
        row = con.execute("SELECT fingerprint FROM books WHERE id=?", (bid,)).fetchone()
        if not row or row[0] != j["fp"]:
            stat["uebersprungen"].append(f"{bid}: Buch fehlt/neu gescannt")
            continue
        if not args.ueberschreiben and con.execute("SELECT 1 FROM book_enrichment WHERE book_id=?", (bid,)).fetchone():
            stat["uebersprungen"].append(f"{bid}: hat schon Recherche")
            continue
        if st.get("identitaet") != "ok":
            stat["uebersprungen"].append(f"{bid}: Identitaet {st.get('identitaet')}")
            continue
        desc, aud = P.get("description"), None
        if not desc and P.get("description_source") == "audible.de" and P.get("asin"):
            desc = aud = audible_desc(P["asin"])
        ov = con.execute("SELECT series, series_number FROM series_overrides WHERE book_id=?", (bid,)).fetchone()
        series, number = (ov[0], ov[1]) if ov else (P.get("series"), P.get("series_number"))
        year = None
        if P.get("release_date"):
            try:
                year = int(str(P["release_date"])[:4])
            except ValueError:
                pass
        stat["neu"] += 1
        if not apply:
            continue
        prov = {k: f"cloud:{P.get('description_source') or 'agent'}" if k == "description" else "cloud"
                for k in ("canonical_title", "authors", "series", "series_number", "description")}
        con.execute(
            """INSERT INTO book_enrichment(enrichment_run_id, book_id, matched, confidence, canonical_title, authors_json,
                   series, series_number, asin, narrators_json, runtime_length_min, audio_publisher, audio_release_date,
                   publication_year, description, description_source, audible_description, primary_genre, subgenres_json,
                   warnings_json, sources_json, provenance_json, semantic_model)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, bid, 1, float(P.get("confidence") or 0.8), P.get("title"), json.dumps(P.get("authors") or [], ensure_ascii=False),
             series, number, P.get("asin"), json.dumps(P.get("narrators") or [], ensure_ascii=False), P.get("runtime_min"),
             P.get("publisher"), P.get("release_date"), year, desc if st.get("beschreibung") == "ok" else None,
             P.get("description_source") if st.get("beschreibung") == "ok" else None, aud, P.get("primary_genre"),
             json.dumps(P.get("subgenres") or [], ensure_ascii=False),
             json.dumps([P["note"]] if P.get("note") else [], ensure_ascii=False),
             json.dumps((P.get("sources") or []) + ([{"url": f"https://www.audible.de/pd/{P['asin']}"}] if P.get("asin") else []),
                        ensure_ascii=False), json.dumps(prov), RUN_MODEL))
        if st.get("cover") == "ok" and (P.get("cover") or {}).get("url"):
            r = save_cover(con, db_dir, cache, bid, P["cover"])
            stat["cover"] += r == "neu"
            if r not in ("neu", "hat schon Cover"):
                stat["uebersprungen"].append(f"{bid}: Cover {r}")
    if apply:
        con.execute("UPDATE enrichment_runs SET finished_at=CURRENT_TIMESTAMP, processed_books=?, successful_books=? WHERE id=?",
                    (stat["jobs"], stat["neu"], run_id))
        con.commit()
    return stat


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True)
    ap.add_argument("--ordner", required=True, help="Ordner mit teil_*.json")
    ap.add_argument("--cache-dir", default=None, help="Standard: _cover_storage neben der DB")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--ueberschreiben", action="store_true")
    args = ap.parse_args()
    db = Path(args.db).resolve()
    con = sqlite3.connect(db)
    db_dir = db.parent
    cache = Path(args.cache_dir) if args.cache_dir else db_dir / "_cover_storage"
    done = {r[0] for r in con.execute("SELECT category FROM enrichment_runs WHERE model=?", (RUN_MODEL,))}
    files = sorted(Path(args.ordner).glob("teil_*.json"))
    todo = [f for f in files if f.name not in done]
    print(f"{len(files)} Teil-Dateien, davon schon uebernommen: {len(files) - len(todo)}")
    total = 0
    for f in todo:
        s = import_teil(con, f, args, db_dir, cache, args.apply)
        total += s["neu"]
        print(f"  {s['datei']}: {s['jobs']} Jobs, {'uebernommen' if args.apply else 'wuerden uebernommen'} {s['neu']}"
              + (f", Cover neu {s['cover']}" if args.apply else ""))
        for u in s["uebersprungen"]:
            print(f"     - {u}")
    print(("Fertig: " if args.apply else "Vorschau, nichts geaendert: ") + f"{total} Buecher."
          + ("" if args.apply else "  Zum Uebernehmen mit --apply wiederholen (GUI zu, DB gesichert)."))
    return 0


if __name__ == "__main__":
    sys.exit(main())
