#!/usr/bin/env python3
"""make_schema.py DB  - legt in einer vom Scanner erzeugten DB die Tabellen fuer Recherche und Cover an
(book_enrichment, book_covers, enrichment_runs, series_overrides ...). Bestehende Daten bleiben unberuehrt."""
import importlib.util, sqlite3, sys
from pathlib import Path
if len(sys.argv) != 2:
    sys.exit(__doc__)
here = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("hb_meta", here / "hoerbuch_metadata_v0_5_5.py")
M = importlib.util.module_from_spec(spec); sys.modules["hb_meta"] = M; spec.loader.exec_module(M)
con = sqlite3.connect(sys.argv[1])
M.ensure_cover_schema_compat(con)
con.execute("""CREATE TABLE IF NOT EXISTS series_overrides (
    book_id INTEGER PRIMARY KEY, series TEXT NOT NULL, series_number TEXT, rule TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
con.commit()
print("Tabellen:", ", ".join(r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY 1")))
