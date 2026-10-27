#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
hoerbuch_metadata_v0_5_5.py
=========================
(Stand 26.10.2026: Audible-Suche mit Rueckfall-Abfragen, siehe fetch_audible_enrichment)

Neue Metadata-Pipeline für den Hörbuchkatalog.

Architektur:
    lokale Scanner-Daten
        -> Calibre (Werkdaten, ISBN, Beschreibung)
        -> Audible.de (konkrete Audioausgabe, Cover, Sprecher, Laufzeit, ASIN)
        -> deterministische Normalisierung
        -> OpenAI nur für semantische Klassifikation
        -> Web-Recherche nur bei wichtigen Lücken

WICHTIG:
- verändert KEINE Hörbuch-/Mediendateien
- schreibt nur Metadaten/Cover in SQLite + _cover_storage
- Calibre-Beschreibung wird nach HTML-Bereinigung wortgetreu übernommen
- Beschreibung-QA: bevorzugt Deutsch, mindestens 60 Wörter / 320 Zeichen
- manuell gesperrte Covers werden niemals automatisch überschrieben
- OpenAI-Key wird ausschließlich aus .env gelesen und nie ausgegeben

Voraussetzungen:
    calibre 9.x (optional, aber empfohlen)
    python -m pip install -U openai pillow

Standardtest:
    python .\hoerbuch_metadata_v0_5_5.py `
        --db .\hoerbuch_katalog_test.sqlite3 `
        --category FunTasy `
        --limit 10

Erste 20 komplett neu aus Scanner + Providern aufbauen (Altbestand ignorieren):
    python .\hoerbuch_metadata_v0_5_5.py `
        --db .\hoerbuch_katalog_test.sqlite3 `
        --category FunTasy `
        --limit 20 `
        --redo `
        --fresh

Nur Provider testen, ohne OpenAI:
    python .\hoerbuch_metadata_v0_5_5.py `
        --db .\hoerbuch_katalog_test.sqlite3 `
        --category FunTasy `
        --limit 10 `
        --no-ai

Einen einzelnen Datensatz testen:
    python .\hoerbuch_metadata_v0_5_5.py `
        --db .\hoerbuch_katalog_test.sqlite3 `
        --book-id 123 `
        --redo
"""

from __future__ import annotations

import argparse
import html
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable

try:
    from PIL import Image, ImageOps
except ImportError:
    print(
        "[FEHLER] Pillow fehlt.\n"
        "Installieren mit:\n"
        "    python -m pip install -U pillow",
        file=sys.stderr,
    )
    raise SystemExit(2)

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None  # type: ignore[assignment]


DEFAULT_DB = Path("hoerbuch_katalog.sqlite3")
DEFAULT_MODEL = "gpt-6-luna"
AUDIBLE_ENDPOINT = "https://api.audible.de/1.0/catalog/products"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0 Safari/537.36 "
    "HoerbuchMetadata/0.5.4"
)

REQUEST_TIMEOUT = 25
MAX_HTTP_BYTES = 24 * 1024 * 1024
THUMB_SIZE = (256, 256)
DETAIL_SIZE = (640, 640)


SCHEMA_SQL = r"""
CREATE TABLE IF NOT EXISTS enrichment_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_run_id INTEGER NOT NULL,
    model TEXT,
    category TEXT,
    requested_limit INTEGER,
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT,
    processed_books INTEGER NOT NULL DEFAULT 0,
    successful_books INTEGER NOT NULL DEFAULT 0,
    failed_books INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS calibre_enrichment (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    enrichment_run_id INTEGER NOT NULL,
    book_id INTEGER NOT NULL,
    found INTEGER NOT NULL,
    title TEXT,
    authors_json TEXT NOT NULL DEFAULT '[]',
    publisher TEXT,
    publication_date TEXT,
    language TEXT,
    isbn13 TEXT,
    series TEXT,
    series_number TEXT,
    tags_json TEXT NOT NULL DEFAULT '[]',
    description_html TEXT,
    description_text TEXT,
    identifiers_json TEXT NOT NULL DEFAULT '{}',
    raw_opf TEXT,
    error_text TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_calibre_enrichment_book
ON calibre_enrichment(book_id);

CREATE TABLE IF NOT EXISTS audible_enrichment (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    enrichment_run_id INTEGER NOT NULL,
    book_id INTEGER NOT NULL,
    found INTEGER NOT NULL,
    match_score REAL,
    asin TEXT,
    title TEXT,
    subtitle TEXT,
    authors_json TEXT NOT NULL DEFAULT '[]',
    narrators_json TEXT NOT NULL DEFAULT '[]',
    series TEXT,
    series_number TEXT,
    language TEXT,
    runtime_length_min INTEGER,
    format_type TEXT,
    publisher_name TEXT,
    release_date TEXT,
    issue_date TEXT,
    publisher_summary_html TEXT,
    publisher_summary_text TEXT,
    merchandising_summary_html TEXT,
    merchandising_summary_text TEXT,
    product_images_json TEXT NOT NULL DEFAULT '{}',
    rating_json TEXT NOT NULL DEFAULT '{}',
    keywords_json TEXT NOT NULL DEFAULT '[]',
    raw_json TEXT,
    error_text TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_audible_enrichment_book
ON audible_enrichment(book_id);

CREATE TABLE IF NOT EXISTS book_enrichment (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    enrichment_run_id INTEGER NOT NULL,
    book_id INTEGER NOT NULL,

    matched INTEGER NOT NULL,
    confidence REAL NOT NULL,

    canonical_title TEXT,
    authors_json TEXT NOT NULL DEFAULT '[]',
    series TEXT,
    series_number TEXT,

    original_language TEXT,
    german_edition INTEGER,
    publication_year INTEGER,
    publisher TEXT,
    isbn13 TEXT,

    asin TEXT,
    narrators_json TEXT NOT NULL DEFAULT '[]',
    runtime_length_min INTEGER,
    audio_format TEXT,
    audio_publisher TEXT,
    audio_release_date TEXT,

    description TEXT,
    description_source TEXT,
    calibre_description TEXT,
    audible_description TEXT,

    primary_genre TEXT,
    subgenres_json TEXT NOT NULL DEFAULT '[]',
    audience TEXT NOT NULL DEFAULT 'unknown',
    protagonist_age TEXT,

    coming_of_age REAL,
    training_arc REAL,
    magic_school REAL,
    chosen_one REAL,
    progression_fantasy REAL,
    litrpg REAL,
    romance REAL,
    grimdark REAL,
    humor REAL,
    violence REAL,

    genre_traits_json TEXT NOT NULL DEFAULT '[]',
    content_profile TEXT,
    additional_information TEXT,
    warnings_json TEXT NOT NULL DEFAULT '[]',
    ratings_json TEXT NOT NULL DEFAULT '[]',
    sources_json TEXT NOT NULL DEFAULT '[]',
    provenance_json TEXT NOT NULL DEFAULT '{}',

    semantic_model TEXT,
    semantic_response_id TEXT,
    semantic_raw_json TEXT,
    web_fallback_used INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_book_enrichment_book
ON book_enrichment(book_id);

CREATE TABLE IF NOT EXISTS enrichment_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    enrichment_run_id INTEGER NOT NULL,
    book_id INTEGER,
    stage TEXT NOT NULL,
    error_text TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS book_covers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id INTEGER NOT NULL,
    research_id INTEGER,
    source_name TEXT,
    source_type TEXT,
    source_page_url TEXT,
    image_url TEXT NOT NULL DEFAULT '',
    local_original_path TEXT NOT NULL,
    local_thumb_path TEXT NOT NULL,
    local_detail_path TEXT NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    aspect_ratio REAL NOT NULL,
    match_quality TEXT NOT NULL,
    quality_score REAL NOT NULL,
    is_selected INTEGER NOT NULL DEFAULT 1,
    is_manual INTEGER NOT NULL DEFAULT 0,
    locked INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_book_covers_book
ON book_covers(book_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_book_covers_selected
ON book_covers(book_id)
WHERE is_selected = 1;
"""


SEMANTIC_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "primary_genre", "subgenres", "audience", "protagonist_age",
        "traits", "genre_traits", "content_profile",
        "additional_information", "warnings",
    ],
    "properties": {
        "primary_genre": {"type": ["string", "null"]},
        "subgenres": {"type": "array", "items": {"type": "string"}},
        "audience": {
            "type": "string",
            "enum": ["adult", "young_adult", "middle_grade", "children", "mixed", "unknown"],
        },
        "protagonist_age": {"type": ["string", "null"]},
        "traits": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "coming_of_age", "training_arc", "magic_school", "chosen_one",
                "progression_fantasy", "litrpg", "romance", "grimdark",
                "humor", "violence",
            ],
            "properties": {
                name: {"type": ["number", "null"], "minimum": 0, "maximum": 1}
                for name in [
                    "coming_of_age", "training_arc", "magic_school", "chosen_one",
                    "progression_fantasy", "litrpg", "romance", "grimdark",
                    "humor", "violence",
                ]
            },
        },
        "genre_traits": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "value_0_10", "note"],
                "properties": {
                    "name": {"type": "string"},
                    "value_0_10": {"type": "number", "minimum": 0, "maximum": 10},
                    "note": {"type": ["string", "null"]},
                },
            },
        },
        "content_profile": {"type": ["string", "null"]},
        "additional_information": {"type": ["string", "null"]},
        "warnings": {"type": "array", "items": {"type": "string"}},
    },
}


WEB_FALLBACK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "matched", "confidence", "canonical_title", "authors", "series",
        "series_number", "original_language", "german_edition",
        "publication_year", "publisher", "isbn13", "description", "sources",
    ],
    "properties": {
        "matched": {"type": "boolean"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "canonical_title": {"type": ["string", "null"]},
        "authors": {"type": "array", "items": {"type": "string"}},
        "series": {"type": ["string", "null"]},
        "series_number": {"type": ["string", "null"]},
        "original_language": {"type": ["string", "null"]},
        "german_edition": {"type": ["boolean", "null"]},
        "publication_year": {"type": ["integer", "null"]},
        "publisher": {"type": ["string", "null"]},
        "isbn13": {"type": ["string", "null"]},
        "description": {"type": ["string", "null"]},
        "sources": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "url", "source_type"],
                "properties": {
                    "title": {"type": "string"},
                    "url": {"type": "string"},
                    "source_type": {
                        "type": "string",
                        "enum": ["publisher", "author", "retailer", "library", "review", "reference", "other"],
                    },
                },
            },
        },
    },
}


@dataclass
class LocalBook:
    id: int
    absolute_path: str
    relative_path: str
    folder_name: str
    author_guess: str | None
    title_guess: str | None
    volume_guess: int | None

    @property
    def category(self) -> str:
        rel = self.relative_path.replace("/", "\\")
        return rel.split("\\", 1)[0] if rel else ""


@dataclass
class ExistingResearch:
    matched: bool = False
    confidence: float = 0.0
    canonical_title: str | None = None
    authors: list[str] | None = None
    series: str | None = None
    series_number: str | None = None
    original_language: str | None = None
    german_edition: bool | None = None
    publication_year: int | None = None
    publisher: str | None = None
    isbn13: str | None = None
    description: str | None = None
    primary_genre: str | None = None
    subgenres: list[str] | None = None
    audience: str = "unknown"
    protagonist_age: str | None = None
    traits: dict[str, float | None] | None = None
    content_profile: str | None = None
    warnings: list[str] | None = None
    ratings: list[dict[str, Any]] | None = None
    sources: list[dict[str, Any]] | None = None


@dataclass
class CalibreData:
    found: bool = False
    title: str | None = None
    authors: list[str] | None = None
    publisher: str | None = None
    publication_date: str | None = None
    language: str | None = None
    isbn13: str | None = None
    series: str | None = None
    series_number: str | None = None
    tags: list[str] | None = None
    description_html: str | None = None
    description_text: str | None = None
    identifiers: dict[str, str] | None = None
    raw_opf: str | None = None
    error: str | None = None


@dataclass
class AudibleData:
    found: bool = False
    match_score: float = 0.0
    asin: str | None = None
    title: str | None = None
    subtitle: str | None = None
    authors: list[str] | None = None
    narrators: list[str] | None = None
    series: str | None = None
    series_number: str | None = None
    language: str | None = None
    runtime_length_min: int | None = None
    format_type: str | None = None
    publisher_name: str | None = None
    release_date: str | None = None
    issue_date: str | None = None
    publisher_summary_html: str | None = None
    publisher_summary_text: str | None = None
    merchandising_summary_html: str | None = None
    merchandising_summary_text: str | None = None
    product_images: dict[str, str] | None = None
    rating: dict[str, Any] | None = None
    keywords: list[str] | None = None
    raw: dict[str, Any] | None = None
    error: str | None = None


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append(data.strip())

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() in {"p", "br", "div", "li"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"p", "div", "li"}:
            self.parts.append("\n")


def html_to_text(raw: str | None) -> str | None:
    if not raw:
        return None
    parser = TextExtractor()
    try:
        parser.feed(html.unescape(raw))
        text = " ".join(parser.parts)
    except Exception:
        text = re.sub(r"<[^>]+>", " ", html.unescape(raw))
    text = re.sub(r"\s*\n\s*", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text or None


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    text = unicodedata.normalize("NFKD", value).casefold().replace("ß", "ss")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"\b(?:ungekuerzt|ungekürzt|hoerbuch|hörbuch|roman)\b", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def text_similarity(a: str | None, b: str | None) -> float:
    na, nb = normalize_text(a), normalize_text(b)
    if not na or not nb:
        return 0.0
    seq = SequenceMatcher(None, na, nb).ratio()
    sa, sb = set(na.split()), set(nb.split())
    union = sa | sb
    jac = len(sa & sb) / len(union) if union else 0.0
    containment = max(
        len(sa & sb) / len(sa) if sa else 0.0,
        len(sa & sb) / len(sb) if sb else 0.0,
    )
    return max(seq, jac, containment * 0.95)


def normalize_isbn(value: str | None) -> str | None:
    if not value:
        return None
    x = re.sub(r"[^0-9Xx]", "", value)
    return x if len(x) in {10, 13} else None


def parse_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    s = str(value).strip().casefold()
    if s in {"true", "yes", "ja", "1"}:
        return True
    if s in {"false", "no", "nein", "0"}:
        return False
    return None


def parse_year(value: str | None) -> int | None:
    if not value:
        return None
    m = re.search(r"\b(18|19|20|21)\d{2}\b", value)
    return int(m.group(0)) if m else None


def json_list(raw: str | None) -> list[Any]:
    if not raw:
        return []
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else []
    except Exception:
        return []


def json_dict(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def unique_strings(values: Iterable[str | None]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value:
            continue
        s = str(value).strip()
        key = normalize_text(s)
        if not s or key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    raw = path.read_text(encoding="utf-8-sig")
    for line0 in raw.splitlines():
        line = line0.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        values[key] = value
    return values


def find_env_file(explicit: Path | None) -> Path | None:
    if explicit is not None:
        p = explicit.expanduser()
        return p if p.exists() and p.is_file() else None
    candidates = [Path(__file__).resolve().parent / ".env", Path.cwd() / ".env"]
    seen: set[Path] = set()
    for p in candidates:
        rp = p.resolve()
        if rp in seen:
            continue
        seen.add(rp)
        if p.exists() and p.is_file():
            return p
    return None


def get_openai_client(explicit_env: Path | None) -> tuple[Any | None, Path | None]:
    if OpenAI is None:
        return None, None
    env_path = find_env_file(explicit_env)
    if env_path is None:
        return None, None
    key = parse_env_file(env_path).get("OPENAI_API_KEY", "").strip()
    if not key:
        return None, env_path
    return OpenAI(api_key=key, timeout=120.0, max_retries=2), env_path


def response_to_dict(response: Any) -> dict[str, Any]:
    if hasattr(response, "model_dump"):
        return response.model_dump(mode="json")
    if hasattr(response, "to_dict"):
        return response.to_dict()
    return {"repr": repr(response)}


def ensure_cover_schema_compat(con: sqlite3.Connection) -> None:
    """Migriert ältere book_covers-v0.1/v0.2-Tabellen minimal weiter."""
    con.executescript(SCHEMA_SQL)
    existing = {row[1] for row in con.execute("PRAGMA table_info(book_covers)")}
    additions = {
        "is_manual": "INTEGER NOT NULL DEFAULT 0",
        "locked": "INTEGER NOT NULL DEFAULT 0",
    }
    for name, decl in additions.items():
        if name not in existing:
            con.execute(f"ALTER TABLE book_covers ADD COLUMN {name} {decl}")
    con.commit()


def latest_scan_run_id(con: sqlite3.Connection) -> int:
    row = con.execute("SELECT MAX(id) FROM scan_runs").fetchone()
    if not row or row[0] is None:
        raise RuntimeError("Keine scan_runs gefunden.")
    return int(row[0])


def load_books(
    con: sqlite3.Connection,
    scan_run_id: int,
    category: str | None,
    limit: int | None,
    offset: int,
    book_id: int | None,
    redo: bool,
) -> list[LocalBook]:
    sql = """
        SELECT id, absolute_path, relative_path, folder_name,
               author_guess, title_guess, volume_guess
        FROM books
        WHERE scan_run_id = ?
    """
    params: list[Any] = [scan_run_id]
    if book_id is not None:
        sql += " AND id = ?"
        params.append(book_id)
    if category:
        sql += " AND (relative_path = ? OR relative_path LIKE ?)"
        params.extend([category, category + r"\%"])
    if not redo:
        sql += " AND NOT EXISTS (SELECT 1 FROM book_enrichment bm WHERE bm.book_id = books.id)"
    sql += " ORDER BY relative_path COLLATE NOCASE"
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params.extend([limit, offset])
    return [
        LocalBook(
            id=int(r[0]), absolute_path=str(r[1]), relative_path=str(r[2]),
            folder_name=str(r[3]), author_guess=r[4], title_guess=r[5], volume_guess=r[6]
        )
        for r in con.execute(sql, params).fetchall()
    ]


def load_existing_research(con: sqlite3.Connection, book_id: int) -> ExistingResearch:
    try:
        row = con.execute(
            """
            SELECT * FROM book_research
            WHERE book_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (book_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    if not row:
        return ExistingResearch()
    cols = [d[0] for d in con.execute("SELECT * FROM book_research LIMIT 0").description]
    d = dict(zip(cols, row))
    return ExistingResearch(
        matched=bool(d.get("matched")),
        confidence=float(d.get("confidence") or 0.0),
        canonical_title=d.get("canonical_title"),
        authors=[str(x) for x in json_list(d.get("authors_json"))],
        series=d.get("series"), series_number=d.get("series_number"),
        original_language=d.get("original_language"),
        german_edition=parse_bool(d.get("german_edition")),
        publication_year=d.get("publication_year"), publisher=d.get("publisher"),
        isbn13=d.get("isbn13"), description=d.get("description"),
        primary_genre=d.get("primary_genre"),
        subgenres=[str(x) for x in json_list(d.get("subgenres_json"))],
        audience=d.get("audience") or "unknown", protagonist_age=d.get("protagonist_age"),
        traits={
            k: d.get(k) for k in [
                "coming_of_age", "training_arc", "magic_school", "chosen_one",
                "progression_fantasy", "litrpg", "romance", "grimdark", "humor", "violence",
            ]
        },
        content_profile=d.get("content_profile"),
        warnings=[str(x) for x in json_list(d.get("warnings_json"))],
        ratings=[x for x in json_list(d.get("ratings_json")) if isinstance(x, dict)],
        sources=[x for x in json_list(d.get("sources_json")) if isinstance(x, dict)],
    )


def _row_to_existing_research(con: sqlite3.Connection, row: sqlite3.Row | tuple | None) -> ExistingResearch:
    if not row:
        return ExistingResearch()
    cols = [d[0] for d in con.execute("SELECT * FROM book_research LIMIT 0").description]
    d = dict(zip(cols, row))
    return ExistingResearch(
        matched=bool(d.get("matched")),
        confidence=float(d.get("confidence") or 0.0),
        canonical_title=d.get("canonical_title"),
        authors=[str(x) for x in json_list(d.get("authors_json"))],
        series=d.get("series"),
        series_number=d.get("series_number"),
        original_language=d.get("original_language"),
        german_edition=parse_bool(d.get("german_edition")),
        publication_year=d.get("publication_year"),
        publisher=d.get("publisher"),
        isbn13=d.get("isbn13"),
        description=d.get("description"),
        primary_genre=d.get("primary_genre"),
        subgenres=[str(x) for x in json_list(d.get("subgenres_json"))],
        audience=d.get("audience") or "unknown",
        protagonist_age=d.get("protagonist_age"),
        traits={
            k: d.get(k) for k in [
                "coming_of_age", "training_arc", "magic_school", "chosen_one",
                "progression_fantasy", "litrpg", "romance", "grimdark", "humor", "violence",
            ]
        },
        content_profile=d.get("content_profile"),
        warnings=[str(x) for x in json_list(d.get("warnings_json"))],
        ratings=[x for x in json_list(d.get("ratings_json")) if isinstance(x, dict)],
        sources=[x for x in json_list(d.get("sources_json")) if isinstance(x, dict)],
    )


def load_trusted_identity_seed(con: sqlite3.Connection, book_id: int) -> ExistingResearch:
    """
    Bevorzugt ursprüngliche Research-Treffer und ignoriert die von metadata-v0.5
    selbst erzeugten Kompatibilitätszeilen. So kann ein Fehlversuch die stabile
    Werkidentität eines späteren --fresh-Laufs nicht vergiften.
    """
    try:
        row = con.execute(
            """
            SELECT br.*
            FROM book_research br
            JOIN research_runs rr ON rr.id = br.research_run_id
            WHERE br.book_id = ?
              AND br.matched = 1
              AND br.confidence >= 0.75
              AND br.canonical_title IS NOT NULL
              AND TRIM(br.canonical_title) <> ''
              AND br.authors_json IS NOT NULL
              AND br.authors_json NOT IN ('[]', '')
              AND COALESCE(rr.model, '') NOT LIKE 'metadata-v0.5%'
            ORDER BY br.confidence DESC, br.id DESC
            LIMIT 1
            """,
            (book_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        row = None

    if row:
        return _row_to_existing_research(con, row)

    try:
        row = con.execute(
            """
            SELECT *
            FROM book_research
            WHERE book_id = ?
              AND matched = 1
              AND confidence >= 0.80
              AND canonical_title IS NOT NULL
              AND TRIM(canonical_title) <> ''
              AND authors_json IS NOT NULL
              AND authors_json NOT IN ('[]', '')
            ORDER BY confidence DESC, id DESC
            LIMIT 1
            """,
            (book_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        row = None

    return _row_to_existing_research(con, row)


def identity_only(seed: ExistingResearch) -> ExistingResearch:
    """Für --fresh nur stabile Identitäts-/Match-Hinweise übernehmen."""
    return ExistingResearch(
        matched=seed.matched,
        confidence=seed.confidence,
        canonical_title=seed.canonical_title,
        authors=list(seed.authors or []),
        series=seed.series,
        series_number=seed.series_number,
        original_language=seed.original_language,
        german_edition=seed.german_edition,
    )


def assess_calibre_match(
    calibre: CalibreData,
    title_hint: str | None,
    author_hints: list[str],
) -> tuple[bool, float, float, float]:
    """
    Calibre liefert Kandidaten. Erst Titel-/Autor-Konsistenz macht daraus einen
    akzeptierten Treffer.
    """
    if not calibre.found:
        return False, 0.0, 0.0, 0.0

    title_score = text_similarity(title_hint, calibre.title)

    if author_hints and calibre.authors:
        author_score = max(
            text_similarity(a, b)
            for a in author_hints
            for b in calibre.authors
            if a and b
        )
    elif not author_hints:
        author_score = 0.65
    else:
        author_score = 0.0

    combined = 0.72 * title_score + 0.28 * author_score

    accepted = (
        title_score >= 0.90
        or (
            title_score >= 0.66
            and author_score >= 0.40
            and combined >= 0.68
        )
    )
    return accepted, combined, title_score, author_score


def reject_calibre_identity_mismatch(
    calibre: CalibreData,
    *,
    combined: float,
    title_score: float,
    author_score: float,
) -> CalibreData:
    calibre.found = False
    calibre.error = (
        "Treffer verworfen: Identität passt nicht "
        f"(gesamt={combined:.3f}, Titel={title_score:.3f}, Autor={author_score:.3f})"
    )
    return calibre


def detect_calibre(explicit: Path | None) -> Path | None:
    if explicit:
        p = explicit.expanduser()
        return p if p.exists() else None
    found = shutil.which("fetch-ebook-metadata")
    if found:
        return Path(found)
    candidates = []
    for env_name in ("ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(env_name)
        if base:
            candidates.append(Path(base) / "Calibre2" / "fetch-ebook-metadata.exe")
    candidates.append(Path(r"C:\Program Files\Calibre2\fetch-ebook-metadata.exe"))
    for p in candidates:
        if p.exists():
            return p
    return None


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].casefold()


def parse_calibre_opf(raw: str) -> CalibreData:
    data = CalibreData(found=False, raw_opf=raw)
    start_candidates = [i for i in (raw.find("<?xml"), raw.find("<package")) if i >= 0]
    if start_candidates:
        raw = raw[min(start_candidates):]
    root = ET.fromstring(raw)

    values: dict[str, list[str]] = {}
    identifiers: dict[str, str] = {}
    meta_values: dict[str, str] = {}

    for elem in root.iter():
        name = local_name(elem.tag)
        text = (elem.text or "").strip()
        if name in {"title", "creator", "publisher", "date", "language", "description", "subject", "identifier"} and text:
            values.setdefault(name, []).append(text)
        if name == "identifier" and text:
            scheme = ""
            for k, v in elem.attrib.items():
                lk = local_name(k)
                if lk in {"scheme", "id"}:
                    scheme = v
            if not scheme:
                if normalize_isbn(text):
                    scheme = "isbn"
                else:
                    scheme = "identifier"
            identifiers[scheme.casefold()] = text
        if name == "meta":
            mname = elem.attrib.get("name") or elem.attrib.get("property") or ""
            content = elem.attrib.get("content") or text
            if mname and content:
                meta_values[mname.casefold()] = content

    authors = values.get("creator", [])
    subjects = values.get("subject", [])
    isbn = None
    for key, val in identifiers.items():
        if "isbn" in key or normalize_isbn(val):
            candidate = normalize_isbn(val)
            if candidate:
                isbn = candidate
                break

    series = meta_values.get("calibre:series") or meta_values.get("belongs-to-collection")
    series_number = meta_values.get("calibre:series_index") or meta_values.get("group-position")
    description_html = values.get("description", [None])[0]

    return CalibreData(
        found=bool(values.get("title") or authors or isbn),
        title=values.get("title", [None])[0],
        authors=unique_strings(authors),
        publisher=values.get("publisher", [None])[0],
        publication_date=values.get("date", [None])[0],
        language=values.get("language", [None])[0],
        isbn13=isbn,
        series=series,
        series_number=series_number,
        tags=unique_strings(subjects),
        description_html=description_html,
        description_text=html_to_text(description_html),
        identifiers=identifiers,
        raw_opf=raw,
    )


def fetch_calibre_enrichment(
    exe: Path,
    title: str,
    authors: list[str],
    isbn: str | None,
    timeout: int,
) -> CalibreData:
    """
    Führt Calibre mit einem harten Watchdog aus.

    Warum nicht subprocess.run(..., PIPE)?
    Unter Windows kann Calibre weitere Prozesse starten. Wenn ein Kindprozess
    einen geerbten Pipe-Handle offen hält, kann communicate() nach einem Timeout
    trotzdem festhängen. Temporäre Dateien vermeiden genau dieses Problem.
    """
    cmd = [str(exe)]
    if title:
        cmd += ["--title", title]
    if authors:
        cmd += ["--authors", " & ".join(authors)]
    if isbn:
        cmd += ["--isbn", isbn]
    cmd += ["--opf", "--timeout", str(timeout)]

    hard_timeout = max(10, int(timeout) + 10)
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    try:
        with tempfile.TemporaryFile() as out_f, tempfile.TemporaryFile() as err_f:
            proc = subprocess.Popen(
                cmd,
                stdout=out_f,
                stderr=err_f,
                creationflags=creationflags,
            )

            try:
                proc.wait(timeout=hard_timeout)
            except subprocess.TimeoutExpired:
                # Calibre kann Unterprozesse besitzen. Unter Windows deshalb
                # den kompletten Prozessbaum abschießen, nicht nur den Parent.
                if os.name == "nt":
                    try:
                        subprocess.run(
                            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=8,
                            check=False,
                        )
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                else:
                    try:
                        proc.kill()
                    except Exception:
                        pass

                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass

                return CalibreData(
                    error=f"Calibre-Timeout nach {hard_timeout}s"
                )

            out_f.seek(0)
            err_f.seek(0)
            raw = out_f.read().decode("utf-8", errors="replace").strip()
            err = err_f.read().decode("utf-8", errors="replace").strip()

    except Exception as exc:
        return CalibreData(error=f"{type(exc).__name__}: {exc}")

    if not raw:
        return CalibreData(error=err[-1200:] if err else "Keine OPF-Ausgabe")

    try:
        return parse_calibre_opf(raw)
    except Exception as exc:
        return CalibreData(
            raw_opf=raw,
            error=f"OPF Parse: {type(exc).__name__}: {exc}",
        )


def http_json(url: str, timeout: int = REQUEST_TIMEOUT) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Accept-Language": "de-DE,de;q=0.9,en;q=0.6",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(MAX_HTTP_BYTES + 1)
        if len(raw) > MAX_HTTP_BYTES:
            raise RuntimeError("HTTP-Antwort zu groß")
        return json.loads(raw.decode("utf-8"))


_ROLE_SUFFIX = re.compile(r"\s*[-–(,]\s*(übersetz|uebersetz|translator|herausgeber|hrsg|illustrat|vorwort|nachwort)[^)]*\)?\s*$", re.I)


def audible_names(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        name = item.get("name") if isinstance(item, dict) else item if isinstance(item, str) else None
        if name and not _ROLE_SUFFIX.search(str(name)):   # 26.10.: Uebersetzer u. ae. sind keine Autoren
            out.append(str(name))
    return unique_strings(out)


def audible_series(value: Any) -> tuple[str | None, str | None]:
    if not isinstance(value, list) or not value:
        return None, None
    item = value[0]
    if not isinstance(item, dict):
        return None, None
    return item.get("title"), str(item.get("sequence")) if item.get("sequence") is not None else None


def series_number_from_text(text: str | None) -> str | None:
    if not text:
        return None
    patterns = [
        r"\((?:[^()]*)\s(\d+(?:[.,]\d+)?)\)\s*$",
        r"\b(?:band|buch|book|teil|folge|vol\.?|volume)\s*#?\s*(\d+(?:[.,]\d+)?)\b",
        r"\b#\s*(\d+(?:[.,]\d+)?)\b",
    ]
    for p in patterns:
        m = re.search(p, text, flags=re.I)
        if m:
            return m.group(1).replace(",", ".")
    return None


def score_audible_product(
    product: dict[str, Any],
    title_hint: str,
    author_hints: list[str],
    series_hint: str | None,
    series_number_hint: str | None,
) -> float:
    ptitle = str(product.get("title") or "")
    psubtitle = str(product.get("subtitle") or "")
    title_score = max(
        text_similarity(title_hint, ptitle),
        text_similarity(title_hint, f"{ptitle} {psubtitle}"),
    )

    pauthors = audible_names(product.get("authors"))
    if author_hints and pauthors:
        author_score = max(text_similarity(a, b) for a in author_hints for b in pauthors)
    elif not author_hints:
        author_score = 0.65
    else:
        author_score = 0.0

    pseries, pseq = audible_series(product.get("series"))
    series_score = text_similarity(series_hint, pseries) if series_hint and pseries else 0.5

    seq_hint = series_number_hint or series_number_from_text(title_hint)
    seq_product = pseq or series_number_from_text(psubtitle)
    if seq_hint and seq_product:
        seq_score = 1.0 if normalize_text(seq_hint) == normalize_text(seq_product) else 0.0
    elif not seq_hint:
        seq_score = 0.5
    else:
        seq_score = 0.35

    language = str(product.get("language") or "").casefold()
    language_score = 1.0 if language in {"german", "de", "deu", "deutsch"} else 0.2

    score = (
        0.55 * title_score
        + 0.27 * author_score
        + 0.08 * series_score
        + 0.05 * seq_score
        + 0.05 * language_score
    )

    if title_score >= 0.98 and author_score >= 0.95:
        score = max(score, 0.96)
    if seq_hint and seq_product and seq_score == 0.0:
        score -= 0.12
    return max(0.0, min(1.0, score))


def fetch_audible_enrichment(
    title: str,
    authors: list[str],
    series_hint: str | None,
    series_number_hint: str | None,
    min_score: float,
) -> AudibleData:
    # 26.10.2026: Rueckfall-Abfragen. Frueher gab es nur EINE Abfrage (Titel + Autor); wich der
    # Autorname von Audibles Schreibweise ab oder stand im Titel noch Reihe/Band/": Roman", kam
    # nichts zurueck und jedes Buch lief in die teure Websuche.
    variants: list[str] = []
    for t in (title,
              re.split(r"\s+:\s+|\s+/\s+", title or "")[0],
              re.split(r"\s+-\s+", title or "")[-1]):
        t = re.sub(r"(?i)\((?:un)?gek(?:ü|ue)rzt[^)]*\)|\(mp3[^)]*\)|\(hsp[^)]*\)?|\bh(?:ö|oe)rspiel\b.*$", "", t or "")
        t = re.sub(r"\s{2,}", " ", t).strip(" -.,:")
        if t and len(t) >= 3 and t not in variants and not re.fullmatch(r"(?i)(band|folge|teil)?\s*\d+", t):
            variants.append(t)
    queries: list[dict[str, str]] = []
    for t in variants:
        if authors:
            queries.append({"title": t, "author": authors[0]})
        queries.append({"title": t})

    products: list[Any] = []
    seen_asins: set[str] = set()
    last_error = None
    for q in queries:
        params: dict[str, str] = {
            "num_results": "10",
            "products_sort_by": "Relevance",
            "image_sizes": "500,1024,2000",
            "response_groups": "contributors,product_desc,product_extended_attrs,product_attrs,media,rating,series",
            **q,
        }
        url = AUDIBLE_ENDPOINT + "?" + urllib.parse.urlencode(params)
        try:
            payload = http_json(url)
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            continue
        for prod in payload.get("products") or []:
            if isinstance(prod, dict) and prod.get("asin") not in seen_asins:
                seen_asins.add(prod.get("asin"))
                products.append(prod)
        if products and "author" in q:      # Treffer mit Autor -> genuegt
            break
    title = variants[0] if variants else title
    if not products:
        return AudibleData(error=last_error or "Keine Audible-Treffer")

    scored: list[tuple[float, dict[str, Any]]] = []
    for product in products:
        if not isinstance(product, dict):
            continue
        score = max(score_audible_product(product, t, authors, series_hint, series_number_hint)
                    for t in (variants or [title]))
        pauth = audible_names(product.get("authors"))
        if authors and pauth:                 # fremder Autor -> nur bei exakt gleichem Titel zulassen
            asim = max(text_similarity(a, b) for a in authors for b in pauth)
            ptitle = normalize_text(re.sub(r"(?i)\s*\(german edition\)", "", str(product.get("title") or "")))
            if asim < 0.6 and ptitle not in {normalize_text(t) for t in (variants or [title])}:
                continue
        scored.append((score, product))
    if not scored:
        return AudibleData(error="Keine auswertbaren Audible-Treffer")

    scored.sort(key=lambda x: x[0], reverse=True)
    score, p = scored[0]
    if score < min_score:
        return AudibleData(match_score=score, raw=p, error=f"Bester Audible-Match nur {score:.3f}")

    series, seq = audible_series(p.get("series"))
    publisher_html = p.get("publisher_summary")
    merchandising_html = p.get("merchandising_summary")
    images = p.get("product_images") if isinstance(p.get("product_images"), dict) else {}
    rating = p.get("rating") if isinstance(p.get("rating"), dict) else {}
    keywords = p.get("thesaurus_subject_keywords") if isinstance(p.get("thesaurus_subject_keywords"), list) else []

    runtime = p.get("runtime_length_min")
    try:
        runtime_int = int(runtime) if runtime is not None else None
    except Exception:
        runtime_int = None

    return AudibleData(
        found=True, match_score=score, asin=p.get("asin"), title=p.get("title"), subtitle=p.get("subtitle"),
        authors=audible_names(p.get("authors")), narrators=audible_names(p.get("narrators")),
        series=series, series_number=seq, language=p.get("language"), runtime_length_min=runtime_int,
        format_type=p.get("format_type"), publisher_name=p.get("publisher_name"),
        release_date=p.get("release_date"), issue_date=p.get("issue_date"),
        publisher_summary_html=publisher_html, publisher_summary_text=html_to_text(publisher_html),
        merchandising_summary_html=merchandising_html, merchandising_summary_text=html_to_text(merchandising_html),
        product_images={str(k): str(v) for k, v in images.items() if v}, rating=rating,
        keywords=[str(x) for x in keywords], raw=p,
    )


def choose_cover_url(images: dict[str, str] | None, preferred: int) -> str | None:
    if not images:
        return None
    numeric: list[tuple[int, str]] = []
    for k, v in images.items():
        try:
            numeric.append((int(k), v))
        except Exception:
            pass
    if not numeric:
        return next(iter(images.values()), None)
    numeric.sort()
    # Exakte/nahe Größe bevorzugen, nicht unnötig 2000 px ziehen.
    return min(numeric, key=lambda kv: abs(kv[0] - preferred))[1]


def download_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "image/*,*/*;q=0.8"})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        data = resp.read(MAX_HTTP_BYTES + 1)
    if len(data) > MAX_HTTP_BYTES:
        raise RuntimeError("Bild zu groß")
    return data


def image_extension(fmt: str | None) -> str:
    return {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "GIF": ".gif"}.get((fmt or "").upper(), ".jpg")


def canvas_contain(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    source = img.convert("RGB")
    fitted = ImageOps.contain(source, size, method=Image.Resampling.LANCZOS)
    # neutraler Rand, kein Crop, kein Stretch
    bg = Image.new("RGB", size, (34, 34, 34))
    x = (size[0] - fitted.width) // 2
    y = (size[1] - fitted.height) // 2
    bg.paste(fitted, (x, y))
    return bg


def relpath_for_db(path: Path, db_dir: Path) -> str:
    try:
        return str(path.resolve().relative_to(db_dir.resolve()))
    except Exception:
        return str(path.resolve())


def has_locked_manual_cover(con: sqlite3.Connection, book_id: int) -> bool:
    try:
        row = con.execute(
            "SELECT 1 FROM book_covers WHERE book_id=? AND is_selected=1 AND locked=1 LIMIT 1",
            (book_id,),
        ).fetchone()
        return bool(row)
    except sqlite3.OperationalError:
        return False


def store_audible_cover(
    con: sqlite3.Connection,
    book: LocalBook,
    audible: AudibleData,
    db_path: Path,
    cache_root: Path,
    preferred_size: int,
) -> bool:
    if not audible.found or not audible.product_images:
        return False
    if has_locked_manual_cover(con, book.id):
        return False
    url = choose_cover_url(audible.product_images, preferred_size)
    if not url:
        return False
    raw = download_bytes(url)
    with Image.open(io.BytesIO(raw)) as img:
        img.load()
        width, height = img.size
        if width < 250 or height < 250:
            raise RuntimeError(f"Audible-Cover zu klein: {width}x{height}")
        fmt = img.format
        original = img.copy()

    original_dir = cache_root / "original"
    thumb_dir = cache_root / "thumb"
    detail_dir = cache_root / "detail"
    for d in (original_dir, thumb_dir, detail_dir):
        d.mkdir(parents=True, exist_ok=True)

    ext = image_extension(fmt)
    original_path = original_dir / f"{book.id}_audible{ext}"
    thumb_path = thumb_dir / f"{book.id}.jpg"
    detail_path = detail_dir / f"{book.id}.jpg"
    original_path.write_bytes(raw)
    canvas_contain(original, THUMB_SIZE).save(thumb_path, "JPEG", quality=90, optimize=True)
    canvas_contain(original, DETAIL_SIZE).save(detail_path, "JPEG", quality=93, optimize=True)

    con.execute("UPDATE book_covers SET is_selected=0 WHERE book_id=? AND is_selected=1", (book.id,))
    con.execute(
        """
        INSERT INTO book_covers(
            book_id, research_id, source_name, source_type, source_page_url,
            image_url, local_original_path, local_thumb_path, local_detail_path,
            width, height, aspect_ratio, match_quality, quality_score,
            is_selected, is_manual, locked
        ) VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, 0)
        """,
        (
            book.id, "Audible.de", "audible",
            f"https://www.audible.de/pd/{audible.asin}" if audible.asin else None,
            url,
            relpath_for_db(original_path, db_path.parent),
            relpath_for_db(thumb_path, db_path.parent),
            relpath_for_db(detail_path, db_path.parent),
            width, height, width / height,
            "audible_exact" if audible.match_score >= 0.90 else "audible_match",
            audible.match_score,
        ),
    )
    return True


def store_calibre_row(con: sqlite3.Connection, run_id: int, book_id: int, c: CalibreData) -> None:
    con.execute(
        """
        INSERT INTO calibre_enrichment(
            enrichment_run_id, book_id, found, title, authors_json, publisher,
            publication_date, language, isbn13, series, series_number, tags_json,
            description_html, description_text, identifiers_json, raw_opf, error_text
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id, book_id, 1 if c.found else 0, c.title,
            json.dumps(c.authors or [], ensure_ascii=False), c.publisher,
            c.publication_date, c.language, c.isbn13, c.series, c.series_number,
            json.dumps(c.tags or [], ensure_ascii=False), c.description_html, c.description_text,
            json.dumps(c.identifiers or {}, ensure_ascii=False), c.raw_opf, c.error,
        ),
    )


def store_audible_row(con: sqlite3.Connection, run_id: int, book_id: int, a: AudibleData) -> None:
    con.execute(
        """
        INSERT INTO audible_enrichment(
            enrichment_run_id, book_id, found, match_score, asin, title, subtitle,
            authors_json, narrators_json, series, series_number, language,
            runtime_length_min, format_type, publisher_name, release_date, issue_date,
            publisher_summary_html, publisher_summary_text,
            merchandising_summary_html, merchandising_summary_text,
            product_images_json, rating_json, keywords_json, raw_json, error_text
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id, book_id, 1 if a.found else 0, a.match_score, a.asin, a.title, a.subtitle,
            json.dumps(a.authors or [], ensure_ascii=False),
            json.dumps(a.narrators or [], ensure_ascii=False),
            a.series, a.series_number, a.language, a.runtime_length_min, a.format_type,
            a.publisher_name, a.release_date, a.issue_date,
            a.publisher_summary_html, a.publisher_summary_text,
            a.merchandising_summary_html, a.merchandising_summary_text,
            json.dumps(a.product_images or {}, ensure_ascii=False),
            json.dumps(a.rating or {}, ensure_ascii=False),
            json.dumps(a.keywords or [], ensure_ascii=False),
            json.dumps(a.raw or {}, ensure_ascii=False), a.error,
        ),
    )



GERMAN_DESCRIPTION_STOPWORDS = {
    "der", "die", "das", "den", "dem", "des", "ein", "eine", "einer", "einem",
    "einen", "und", "oder", "aber", "ist", "sind", "war", "wird", "werden", "mit",
    "für", "von", "zu", "im", "in", "auf", "aus", "als", "auch", "nicht", "sich",
    "sein", "seine", "ihr", "ihre", "er", "sie", "es", "an", "bei", "nach", "vor",
    "über", "um", "nur", "noch", "wenn", "doch", "eines", "einer", "einem", "einen",
}

ENGLISH_DESCRIPTION_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "is", "are", "was", "were", "will", "with",
    "for", "from", "to", "in", "on", "of", "as", "also", "not", "his", "her", "their",
    "he", "she", "it", "they", "this", "that", "these", "those", "at", "by", "into",
    "after", "before", "about", "when", "while", "only", "one", "who", "has", "have",
}


def description_word_count(text: str | None) -> int:
    if not text:
        return 0
    return len(re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿÄÖÜäöüß0-9]+(?:['’-][A-Za-zÀ-ÖØ-öø-ÿÄÖÜäöüß0-9]+)?", text))


def guess_description_language(text: str | None) -> str:
    """Kleine deterministische DE/EN-Heuristik. Keine Volltext-Spracherkennung nötig."""
    if not text:
        return "unknown"
    words = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿÄÖÜäöüß]+", text.casefold())
    if len(words) < 8:
        return "unknown"
    de = sum(1 for w in words if w in GERMAN_DESCRIPTION_STOPWORDS)
    en = sum(1 for w in words if w in ENGLISH_DESCRIPTION_STOPWORDS)
    # Umlaute/ß sind ein zusätzlicher, aber nicht allein entscheidender Hinweis.
    if re.search(r"[äöüß]", text.casefold()):
        de += 2
    total = max(1, len(words))
    # Bei fast ausgeglichenen Scores lieber "unknown" als falsch klassifizieren.
    if de >= 3 and de >= en * 1.45 and de / total >= 0.035:
        return "de"
    if en >= 3 and en >= de * 1.45 and en / total >= 0.035:
        return "en"
    return "unknown"


def assess_description(
    text: str | None,
    *,
    expect_german: bool,
    min_words: int,
    min_chars: int,
) -> dict[str, Any]:
    clean = (text or "").strip()
    words = description_word_count(clean)
    chars = len(clean)
    language = guess_description_language(clean)
    reasons: list[str] = []

    if not clean:
        reasons.append("missing")
    else:
        if words < min_words:
            reasons.append(f"short:{words}w")
        if chars < min_chars:
            reasons.append(f"short_chars:{chars}")
        if expect_german and language == "en":
            reasons.append("english_for_german_edition")

    acceptable = bool(clean) and not reasons
    return {
        "acceptable": acceptable,
        "word_count": words,
        "char_count": chars,
        "language": language,
        "reasons": reasons,
    }


def choose_description(
    candidates: list[tuple[str | None, str]],
    *,
    expect_german: bool,
    min_words: int,
    min_chars: int,
) -> tuple[str | None, str | None, dict[str, Any]]:
    """
    Wählt zuerst eine qualitativ ausreichende Beschreibung. Unter gleich guten
    Kandidaten gilt die Reihenfolge der übergebenen Quellen (Calibre, Audible,
    Altbestand). Wenn alle schwach sind, bleibt der beste Kandidat sichtbar und
    löst anschließend gezielte Nachrecherche aus.
    """
    evaluated: list[tuple[str, str, dict[str, Any], int]] = []
    for rank, (text, source) in enumerate(candidates):
        clean = (text or "").strip()
        if not clean:
            continue
        q = assess_description(
            clean,
            expect_german=expect_german,
            min_words=min_words,
            min_chars=min_chars,
        )
        evaluated.append((clean, source, q, rank))

    if not evaluated:
        return None, None, assess_description(
            None, expect_german=expect_german,
            min_words=min_words, min_chars=min_chars,
        )

    acceptable = [item for item in evaluated if item[2]["acceptable"]]
    if acceptable:
        # Quellenpräferenz bleibt Calibre > Audible > existing_research.
        text, source, quality, _ = min(acceptable, key=lambda x: x[3])
        return text, source, quality

    # Fallback-Auswahl: Deutsch schlägt Englisch; danach Länge; dann Quellenrang.
    def weak_score(item: tuple[str, str, dict[str, Any], int]) -> tuple[int, int, int, int]:
        _, _, q, rank = item
        lang = q["language"]
        language_score = 2 if (not expect_german or lang == "de") else (1 if lang == "unknown" else 0)
        return (language_score, q["word_count"], q["char_count"], -rank)

    text, source, quality, _ = max(evaluated, key=weak_score)
    return text, source, quality


def description_needs_upgrade(
    merged: dict[str, Any],
    *,
    min_words: int,
    min_chars: int,
) -> tuple[bool, dict[str, Any]]:
    quality = assess_description(
        merged.get("description"),
        expect_german=bool(merged.get("german_edition")),
        min_words=min_words,
        min_chars=min_chars,
    )
    return (not quality["acceptable"], quality)



# v0.5.5: Werte, die nur aus dem Ordnernamen geraten wurden, sind Platzhalter.
# Sie dürfen angezeigt werden, gelten aber nicht als belegte Fakten: Websuche wird
# ausgelöst und ein Web-Treffer darf sie überschreiben.
PROVISIONAL_SOURCES = {"scanner"}
JUNK_AUTHORS = {
    "unbekannt", "unknown", "anonym", "anonymous", "diverse", "various",
    "verschiedene", "various artists", "n a", "na", "autor", "author",
}
VOLUME_ONLY_TITLE_RE = re.compile(
    r"(?ix)^\s*(?:(?:band|bd\.?|teil|folge|vol(?:ume)?\.?|episode)\s*)?0*\d{1,3}"
    r"(?:\s*\(\d{4}\))?\s*$"
)
LEADING_VOLUME_RE = re.compile(r"(?ix)^\s*(?:(?:band|bd\.?|teil|folge)\s*)?0*\d{1,3}\s*[-.:]\s*\S")


def clean_authors(values: list[Any] | None) -> list[str]:
    out: list[str] = []
    for v in values or []:
        name = str(v or "").strip()
        if name and normalize_text(name) not in JUNK_AUTHORS and name not in out:
            out.append(name)
    return out


def is_provisional(provenance: dict[str, str], field: str) -> bool:
    return provenance.get(field) in PROVISIONAL_SOURCES


def scanner_title_is_weak(book: "LocalBook") -> bool:
    """'Band 1 (2025)', '1 - Stein (2012)', 'Teil 1 - Der Sturm': der linke Teil
    des Ordnernamens war dann eher Serie als Autor."""
    title = (book.title_guess or "").strip()
    return bool(VOLUME_ONLY_TITLE_RE.match(title) or LEADING_VOLUME_RE.match(title))


def load_series_override(con: sqlite3.Connection, book_id: int) -> tuple[str, str | None] | None:
    try:
        row = con.execute(
            "SELECT series, series_number FROM series_overrides WHERE book_id = ?", (book_id,)
        ).fetchone()
    except sqlite3.OperationalError:          # Tabelle existiert erst nach hoerbuch_reihen_v0_1.py --apply
        return None
    return (str(row[0]), str(row[1]) if row[1] is not None else None) if row and row[0] else None


def needs_identity_bootstrap(
    book: LocalBook,
    identity_seed: ExistingResearch,
) -> bool:
    """
    Cold-start rescue:
    On a genuinely fresh DB there is no previous research seed. Some folders are
    only author/series labels (e.g. "Alina Fox") rather than book titles.
    In those cases identify the work first, before asking Calibre/Audible.
    """
    if (
        identity_seed.matched
        and identity_seed.confidence >= 0.75
        and identity_seed.canonical_title
        and identity_seed.authors
    ):
        return False

    title = (book.title_guess or book.folder_name or "").strip()
    author = (book.author_guess or "").strip()

    if not author:
        return True
    if not title:
        return True
    # v0.5.5: "Serie - Band 1" / "Serie - 1 - Titel" -> linker Teil ist kein Autor.
    if scanner_title_is_weak(book):
        return True

    # A scanner title identical to the complete folder label is often too weak
    # to trust as a bibliographic title on its own.
    if normalize_text(title) == normalize_text(book.folder_name):
        return True

    return False


def identity_bootstrap_prompt(book: LocalBook) -> str:
    return f"""
Identifiziere das konkrete deutschsprachige Hörbuch/Werk hinter einem lokalen
Bibliothekseintrag. Dies ist ein COLD-START: Der Ordnername kann Autor, Serie,
Figurenname oder Werktitel sein. Nicht einfach annehmen, dass der Ordnername der
Buchtitel ist.

LOKALER EINTRAG
Pfad: {book.relative_path}
Ordner: {book.folder_name}
Linker Teil des Ordnernamens (Autor ODER Serie/Reihe!): {book.author_guess or '?'}
Rechter Teil (Titel oder nur Band-/Teilnummer): {book.title_guess or '?'}

AUFGABE
- Suche im Web gezielt nach dem exakten Hörbuch/Werk.
- Nutze Begriffe wie Hörbuch, Autor, Reihe und ggf. Verlags-/Shop-Treffer.
- Bevorzuge deutsche Verlagsseiten, Audible, Buchhandel, Bibliotheken,
  Autorenseiten und belastbare Kataloge.
- Match NICHT erzwingen. Wenn die Identität nicht hinreichend sicher ist,
  matched=false.
- Wenn sicher: kanonischen Titel, echte Autoren, Serie/Band, Sprache,
  deutsche Ausgabe, Jahr, Verlag, ISBN soweit verfügbar zurückgeben.
- Falls eine gute deutsche Inhaltsbeschreibung auffindbar ist, 80-180 Wörter,
  sachlich, spoilerarm, keine Rezension. Sonst description=null.
- Nur tatsächlich benutzte Quellen nennen.

WICHTIG
Ein Beispiel für eine problematische Struktur wäre ein Ordner wie "Alina Fox":
Das könnte eine Figur/Serie sein und nicht der eigentliche Werktitel. Recherchiere
deshalb die Identität statt den String blind als Titel zu behandeln.
""".strip()


def existing_from_web_identity(web: dict[str, Any]) -> ExistingResearch:
    return ExistingResearch(
        matched=bool(web.get("matched")),
        confidence=float(web.get("confidence") or 0.0),
        canonical_title=web.get("canonical_title"),
        authors=[str(x) for x in (web.get("authors") or []) if x],
        series=web.get("series"),
        series_number=web.get("series_number"),
        original_language=web.get("original_language"),
        german_edition=web.get("german_edition"),
        publication_year=web.get("publication_year"),
        publisher=web.get("publisher"),
        isbn13=web.get("isbn13"),
        description=web.get("description"),
        sources=[x for x in (web.get("sources") or []) if isinstance(x, dict)],
    )


def semantic_prompt(
    book: LocalBook,
    merged: dict[str, Any],
    calibre: CalibreData,
    audible: AudibleData,
) -> str:
    return f"""
Du klassifizierst ein Hörbuch für einen privaten Katalog. Nutze NUR die unten
bereitgestellten Metadaten und Beschreibungstexte. Keine Websuche in diesem Schritt.

ZIEL
- Inhalt semantisch einordnen, ohne bibliographische Daten neu zu erfinden.
- Antworte in DEUTSCH.
- Das Entscheidungsprofil soll einem Hörer helfen, der ein Hörbuch vor dem Hören
  nicht überfliegen kann. Etwa 60-100 Wörter. Es ist KEINE zweite Inhaltsangabe:
  Handlung und Figuren nicht wiederholen. Stattdessen: Erzählform (Roman,
  Kurzgeschichten, Anthologie, Hörspiel), Ton und Tempo, Härtegrad, nötiges
  Vorwissen bzw. Serieneinstieg, Länge und Sprecher, für wen es passt und für wen
  eher nicht. Wenn die Texte dafür nichts hergeben, lieber kurz bleiben.
- Zusatzinformationen: höchstens etwa 120 Wörter, nur wirklich nützliche Besonderheiten.
- Die Beschreibung selbst NICHT umschreiben; sie wird separat aus dem Feed übernommen.
- Bei Fantasy die festen Fantasy-Merkmale sinnvoll bewerten.
- Bei anderen Genres dürfen die festen Fantasy-Werte null sein. Erzeuge dafür 4-7
  passende genre_traits, z.B. Krimi: Ermittlungsfokus, Action, psychologische Tiefe,
  Cozy-Faktor, Täterperspektive; Sachbuch: Vorkenntnisse, technische Tiefe,
  Praxisanteil, Aktualität, Nachschlagecharakter.
- Werte 0..1 bzw. 0..10 nur, wenn aus den Texten sinnvoll ableitbar; sonst null bzw.
  bei genre_traits nur Dimensionen aufnehmen, die tatsächlich bewertbar sind.

LOKALER EINTRAG
Pfad: {book.relative_path}
Lokaler Titel: {book.title_guess or book.folder_name}
Lokaler Autor: {book.author_guess or '?'}

ZUSAMMENGEFÜHRTE FAKTEN
{json.dumps(merged, ensure_ascii=False, indent=2)}

CALIBRE-TAGS
{json.dumps((calibre.tags or []) if calibre.found else [], ensure_ascii=False)}

AUDIBLE-KEYWORDS
{json.dumps(audible.keywords or [], ensure_ascii=False)}

CALIBRE-BESCHREIBUNG
{(calibre.description_text if calibre.found else None) or '(keine)'}

AUDIBLE-BESCHREIBUNG
{audible.publisher_summary_text or audible.merchandising_summary_text or '(keine)'}
""".strip()


def run_semantic(client: Any, model: str, prompt: str) -> tuple[dict[str, Any], Any]:
    response = client.responses.create(
        model=model,
        reasoning={"effort": "low"},
        input=prompt,
        text={
            "format": {
                "type": "json_schema",
                "name": "audiobook_semantic_profile",
                "strict": True,
                "schema": SEMANTIC_SCHEMA,
            }
        },
    )
    if not response.output_text:
        raise RuntimeError("Leere semantische Modellantwort")
    return json.loads(response.output_text), response


def needs_web_fallback(
    merged: dict[str, Any],
    *,
    min_description_words: int,
    min_description_chars: int,
    provenance: dict[str, str] | None = None,
) -> bool:
    provenance = provenance or {}
    if not merged.get("canonical_title") or is_provisional(provenance, "canonical_title"):
        return True
    if not merged.get("authors") or is_provisional(provenance, "authors"):
        return True
    needs_desc, _ = description_needs_upgrade(
        merged,
        min_words=min_description_words,
        min_chars=min_description_chars,
    )
    if needs_desc:
        return True
    if float(merged.get("confidence") or 0.0) < 0.72:
        return True
    return False


def web_fallback_prompt(
    book: LocalBook,
    merged: dict[str, Any],
    *,
    description_quality: dict[str, Any],
    min_description_words: int,
) -> str:
    reasons = ", ".join(description_quality.get("reasons") or []) or "keine"
    return f"""
Recherchiere NUR die fehlenden oder unsicheren Angaben zu diesem lokalen deutschen
Hörbuch. Vorhandene strukturierte Feed-Daten haben Vorrang.

LOKAL
Pfad: {book.relative_path}
Linker Teil des Ordnernamens (Autor ODER Serie/Reihe!): {book.author_guess or '?'}
Rechter Teil (Titel oder nur Band-/Teilnummer): {book.title_guess or book.folder_name}

BISHERIGE DATEN
{json.dumps(merged, ensure_ascii=False, indent=2)}

BESCHREIBUNGS-QUALITÄT
Aktuelle Probleme: {reasons}
Ziel: eine flüssige DEUTSCHE Inhaltsbeschreibung von ungefähr 80-180 Wörtern,
sofern die aktuelle Beschreibung fehlt, englisch ist oder weniger als
{min_description_words} Wörter enthält. Nutze bevorzugt Verlag, Autorenseite,
Audible/Buchhandel oder Bibliothekskatalog. Keine Rezension und möglichst keine
Spoiler. Formuliere bei Webquellen eine eigene sachliche Zusammenfassung; keine
langen wörtlichen Übernahmen aus fremden Webseiten.

Regeln:
- Werte mit Herkunft "scanner" in den bisherigen Daten sind nur aus dem Ordnernamen
  geraten (z. B. Serie als Autor, "Band 1" als Titel). Ersetze sie durch den echten
  Werktitel und die echten Autoren. Bei Anthologien alle Autoren oder den
  Herausgeber nennen, nie den Reihennamen.
- Match nicht erzwingen.
- Deutsche Ausgabe bevorzugen.
- Bereits gute Feed-Fakten nicht überschreiben.
- Serie/Band möglichst knapp normalisieren.
- Nur Quellen nennen, die tatsächlich benutzt wurden.
""".strip()


def run_web_fallback(client: Any, model: str, prompt: str) -> tuple[dict[str, Any], Any]:
    response = client.responses.create(
        model=model,
        reasoning={"effort": "low"},
        tools=[{"type": "web_search"}],
        tool_choice="required",
        input=prompt,
        text={
            "format": {
                "type": "json_schema",
                "name": "audiobook_gap_research",
                "strict": True,
                "schema": WEB_FALLBACK_SCHEMA,
            }
        },
    )
    if not response.output_text:
        raise RuntimeError("Leere Web-Fallback-Antwort")
    return json.loads(response.output_text), response


def merge_initial(
    book: LocalBook,
    existing: ExistingResearch,
    calibre: CalibreData,
    audible: AudibleData,
    *,
    min_description_words: int,
    min_description_chars: int,
) -> tuple[dict[str, Any], dict[str, str]]:
    provenance: dict[str, str] = {}

    def choose(field: str, candidates: list[tuple[Any, str]]) -> Any:
        for value, source in candidates:
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            if isinstance(value, list) and not value:
                continue
            provenance[field] = source
            return value
        return None

    title = choose("canonical_title", [
        (calibre.title if calibre.found else None, "calibre"),
        (audible.title, "audible.de"),
        (existing.canonical_title, "existing_research"),
        (book.title_guess or book.folder_name, "scanner"),
    ])
    authors = choose("authors", [
        (clean_authors(calibre.authors) if calibre.found else [], "calibre"),
        (clean_authors(audible.authors), "audible.de"),
        (clean_authors(existing.authors), "existing_research"),
        ([] if scanner_title_is_weak(book) else clean_authors([book.author_guess]), "scanner"),
    ]) or []
    series = choose("series", [
        (audible.series, "audible.de"),
        (calibre.series if calibre.found else None, "calibre"),
        (existing.series, "existing_research"),
    ])
    series_number = choose("series_number", [
        (audible.series_number, "audible.de"),
        (calibre.series_number if calibre.found else None, "calibre"),
        (existing.series_number, "existing_research"),
        (str(book.volume_guess) if book.volume_guess is not None else None, "scanner"),
    ])
    isbn13 = choose("isbn13", [
        (calibre.isbn13 if calibre.found else None, "calibre"),
        (existing.isbn13, "existing_research"),
    ])

    lang = (calibre.language or "").casefold() if calibre.found else ""
    audible_lang = (audible.language or "").casefold()
    german_edition = (
        True if lang in {"de", "deu", "ger", "german", "deutsch"} or audible_lang in {"german", "de", "deu", "deutsch"}
        else existing.german_edition
    )

    calibre_desc = (calibre.description_text or "").strip() if calibre.found else ""
    audible_desc = (audible.publisher_summary_text or audible.merchandising_summary_text or "").strip()
    existing_desc = (existing.description or "").strip()
    description, description_source, description_quality = choose_description(
        [
            (calibre_desc, "calibre"),
            (audible_desc, "audible.de"),
            (existing_desc, "existing_research"),
        ],
        expect_german=bool(german_edition),
        min_words=min_description_words,
        min_chars=min_description_chars,
    )
    if description_source:
        provenance["description"] = description_source
    provenance["description_quality"] = json.dumps(description_quality, ensure_ascii=False)

    pub_year = (parse_year(calibre.publication_date) if calibre.found else None) or parse_year(audible.release_date) or existing.publication_year
    publisher = (calibre.publisher if calibre.found else None) or audible.publisher_name or existing.publisher

    confidence_parts = []
    if audible.found:
        confidence_parts.append(audible.match_score)
    if calibre.found:
        confidence_parts.append(0.92)
    if existing.matched:
        confidence_parts.append(existing.confidence)
    confidence = max(confidence_parts) if confidence_parts else 0.55

    merged = {
        "matched": bool(audible.found or calibre.found or existing.matched),
        "confidence": float(confidence),
        "canonical_title": title,
        "authors": authors,
        "series": series,
        "series_number": series_number,
        "original_language": existing.original_language,
        "german_edition": german_edition,
        "publication_year": pub_year,
        "publisher": publisher,
        "isbn13": isbn13,
        "asin": audible.asin,
        "narrators": audible.narrators or [],
        "runtime_length_min": audible.runtime_length_min,
        "audio_format": audible.format_type,
        "audio_publisher": audible.publisher_name,
        "audio_release_date": audible.release_date,
        "description": description,
        "description_source": description_source,
        "calibre_description": calibre.description_text if calibre.found else None,
        "audible_description": audible.publisher_summary_text or audible.merchandising_summary_text,
    }
    return merged, provenance


def merge_web_fallback(
    merged: dict[str, Any],
    provenance: dict[str, str],
    web: dict[str, Any],
    *,
    min_description_words: int,
    min_description_chars: int,
) -> None:
    if not web.get("matched"):
        return
    for field in [
        "canonical_title", "authors", "series", "series_number", "original_language",
        "german_edition", "publication_year", "publisher", "isbn13",
    ]:
        current = merged.get(field)
        missing = current is None or current == "" or current == []
        new = web.get(field)
        if field == "authors":
            new = clean_authors(new)
        if (missing or is_provisional(provenance, field)) and new not in (None, "", []):
            merged[field] = new
            provenance[field] = "openai_web_fallback"

    web_desc = (web.get("description") or "").strip()
    if web_desc:
        current_q = assess_description(
            merged.get("description"),
            expect_german=bool(merged.get("german_edition")),
            min_words=min_description_words,
            min_chars=min_description_chars,
        )
        web_q = assess_description(
            web_desc,
            expect_german=bool(merged.get("german_edition")),
            min_words=min_description_words,
            min_chars=min_description_chars,
        )
        # Ersetzen, wenn der aktuelle Text Qualitätsprobleme hat und der neue Text
        # diese behebt oder klar besser ist.
        current_bad = not current_q["acceptable"]
        web_better = (
            web_q["acceptable"]
            or (
                current_bad
                and web_q["language"] != "en"
                and web_q["word_count"] > current_q["word_count"]
            )
        )
        if current_bad and web_better:
            merged["description"] = web_desc
            merged["description_source"] = "openai_web_fallback"
            provenance["description"] = "openai_web_fallback"
            provenance["description_quality"] = json.dumps(web_q, ensure_ascii=False)

    merged["confidence"] = max(float(merged.get("confidence") or 0), float(web.get("confidence") or 0))
    merged["matched"] = bool(merged.get("matched") or web.get("matched"))


def combine_sources(existing: ExistingResearch, audible: AudibleData, web: dict[str, Any] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in existing.sources or []:
        url = str(source.get("url") or "")
        if url and url not in seen:
            seen.add(url)
            out.append(source)
    if audible.asin:
        url = f"https://www.audible.de/pd/{audible.asin}"
        if url not in seen:
            seen.add(url)
            out.append({"title": f"Audible.de: {audible.title or audible.asin}", "url": url, "source_type": "retailer"})
    if web:
        for source in web.get("sources") or []:
            if not isinstance(source, dict):
                continue
            url = str(source.get("url") or "")
            if url and url not in seen:
                seen.add(url)
                out.append(source)
    return out


def audible_rating_rows(a: AudibleData) -> list[dict[str, Any]]:
    if not a.rating:
        return []
    d = a.rating
    value = None
    for key in ("overall_average_rating", "average_rating", "display_average_rating"):
        if key in d:
            try:
                value = float(d[key])
                break
            except Exception:
                pass
    count = None
    try:
        n = d.get("num_reviews")
        count = int(n) if n is not None else None
    except Exception:
        pass
    if value is None and count is None:
        return []
    return [{
        "source": "Audible.de",
        "value": value,
        "scale": 5 if value is not None else None,
        "count": count,
        "url": f"https://www.audible.de/pd/{a.asin}" if a.asin else None,
    }]


def ensure_compat_research_schema(con: sqlite3.Connection) -> bool:
    try:
        con.execute("SELECT 1 FROM book_research LIMIT 1")
        con.execute("SELECT 1 FROM research_runs LIMIT 1")
        return True
    except sqlite3.OperationalError:
        return False


def create_compat_research_run(con: sqlite3.Connection, scan_run_id: int, model: str, category: str | None, limit: int | None) -> int | None:
    if not ensure_compat_research_schema(con):
        return None
    cur = con.execute(
        """INSERT INTO research_runs(scan_run_id, model, category, requested_limit)
           VALUES (?, ?, ?, ?)""",
        (scan_run_id, f"metadata-v0.5/{model}", category, limit),
    )
    return int(cur.lastrowid) if cur.lastrowid is not None else None


def insert_compat_book_research(
    con: sqlite3.Connection,
    research_run_id: int | None,
    book_id: int,
    model: str,
    merged: dict[str, Any],
    semantic: dict[str, Any],
    ratings: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    raw_bundle: dict[str, Any],
    response_id: str | None,
) -> None:
    if research_run_id is None:
        return
    traits = semantic.get("traits") or {}
    con.execute(
        """
        INSERT INTO book_research(
            research_run_id, book_id, matched, confidence,
            canonical_title, authors_json, series, series_number,
            original_language, german_edition, publication_year, publisher, isbn13,
            description, primary_genre, subgenres_json, audience, protagonist_age,
            coming_of_age, training_arc, magic_school, chosen_one,
            progression_fantasy, litrpg, romance, grimdark, humor, violence,
            content_profile, warnings_json, ratings_json, sources_json,
            response_id, model, raw_response_json
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        """,
        (
            research_run_id, book_id, 1 if merged.get("matched") else 0, float(merged.get("confidence") or 0),
            merged.get("canonical_title"), json.dumps(merged.get("authors") or [], ensure_ascii=False),
            merged.get("series"), merged.get("series_number"), merged.get("original_language"),
            None if merged.get("german_edition") is None else (1 if merged.get("german_edition") else 0),
            merged.get("publication_year"), merged.get("publisher"), merged.get("isbn13"),
            merged.get("description"), semantic.get("primary_genre"),
            json.dumps(semantic.get("subgenres") or [], ensure_ascii=False), semantic.get("audience") or "unknown",
            semantic.get("protagonist_age"),
            traits.get("coming_of_age"), traits.get("training_arc"), traits.get("magic_school"),
            traits.get("chosen_one"), traits.get("progression_fantasy"), traits.get("litrpg"),
            traits.get("romance"), traits.get("grimdark"), traits.get("humor"), traits.get("violence"),
            semantic.get("content_profile"), json.dumps(semantic.get("warnings") or [], ensure_ascii=False),
            json.dumps(ratings, ensure_ascii=False), json.dumps(sources, ensure_ascii=False),
            response_id, f"metadata-v0.5/{model}", json.dumps(raw_bundle, ensure_ascii=False),
        ),
    )


def store_book_enrichment(
    con: sqlite3.Connection,
    run_id: int,
    book_id: int,
    merged: dict[str, Any],
    semantic: dict[str, Any],
    ratings: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    provenance: dict[str, str],
    model: str | None,
    semantic_response: Any | None,
    web_used: bool,
) -> None:
    traits = semantic.get("traits") or {}
    con.execute(
        """
        INSERT INTO book_enrichment(
            enrichment_run_id, book_id, matched, confidence,
            canonical_title, authors_json, series, series_number,
            original_language, german_edition, publication_year, publisher, isbn13,
            asin, narrators_json, runtime_length_min, audio_format, audio_publisher, audio_release_date,
            description, description_source, calibre_description, audible_description,
            primary_genre, subgenres_json, audience, protagonist_age,
            coming_of_age, training_arc, magic_school, chosen_one,
            progression_fantasy, litrpg, romance, grimdark, humor, violence,
            genre_traits_json, content_profile, additional_information,
            warnings_json, ratings_json, sources_json, provenance_json,
            semantic_model, semantic_response_id, semantic_raw_json, web_fallback_used
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        """,
        (
            run_id, book_id, 1 if merged.get("matched") else 0, float(merged.get("confidence") or 0),
            merged.get("canonical_title"), json.dumps(merged.get("authors") or [], ensure_ascii=False),
            merged.get("series"), merged.get("series_number"), merged.get("original_language"),
            None if merged.get("german_edition") is None else (1 if merged.get("german_edition") else 0),
            merged.get("publication_year"), merged.get("publisher"), merged.get("isbn13"),
            merged.get("asin"), json.dumps(merged.get("narrators") or [], ensure_ascii=False),
            merged.get("runtime_length_min"), merged.get("audio_format"), merged.get("audio_publisher"),
            merged.get("audio_release_date"), merged.get("description"), merged.get("description_source"),
            merged.get("calibre_description"), merged.get("audible_description"),
            semantic.get("primary_genre"), json.dumps(semantic.get("subgenres") or [], ensure_ascii=False),
            semantic.get("audience") or "unknown", semantic.get("protagonist_age"),
            traits.get("coming_of_age"), traits.get("training_arc"), traits.get("magic_school"),
            traits.get("chosen_one"), traits.get("progression_fantasy"), traits.get("litrpg"),
            traits.get("romance"), traits.get("grimdark"), traits.get("humor"), traits.get("violence"),
            json.dumps(semantic.get("genre_traits") or [], ensure_ascii=False),
            semantic.get("content_profile"), semantic.get("additional_information"),
            json.dumps(semantic.get("warnings") or [], ensure_ascii=False),
            json.dumps(ratings, ensure_ascii=False), json.dumps(sources, ensure_ascii=False),
            json.dumps(provenance, ensure_ascii=False), model,
            getattr(semantic_response, "id", None) if semantic_response else None,
            json.dumps(response_to_dict(semantic_response), ensure_ascii=False) if semantic_response else None,
            1 if web_used else 0,
        ),
    )


def default_semantic_from_existing(existing: ExistingResearch) -> dict[str, Any]:
    return {
        "primary_genre": existing.primary_genre,
        "subgenres": existing.subgenres or [],
        "audience": existing.audience or "unknown",
        "protagonist_age": existing.protagonist_age,
        "traits": existing.traits or {
            k: None for k in [
                "coming_of_age", "training_arc", "magic_school", "chosen_one",
                "progression_fantasy", "litrpg", "romance", "grimdark", "humor", "violence",
            ]
        },
        "genre_traits": [],
        "content_profile": existing.content_profile,
        "additional_information": None,
        "warnings": existing.warnings or [],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Hörbuch-Metadaten-Orchestrator v0.5.5")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--category", type=str, default=None)
    parser.add_argument("--book-id", type=int, default=None)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--redo", action="store_true")
    parser.add_argument(
        "--fresh", action="store_true",
        help="Ignoriert bisherigen book_research-Altbestand als Eingabe; startet nur mit Scanner + Providern."
    )
    parser.add_argument("--model", type=str, default=DEFAULT_MODEL)
    parser.add_argument("--env", type=Path, default=None)
    parser.add_argument("--calibre-exe", type=Path, default=None)
    parser.add_argument("--no-calibre", action="store_true")
    parser.add_argument("--no-audible", action="store_true")
    parser.add_argument("--no-ai", action="store_true")
    parser.add_argument("--no-web-fallback", action="store_true")
    parser.add_argument("--audible-min-score", type=float, default=0.72)
    parser.add_argument("--cover-size", type=int, default=1024)
    parser.add_argument("--cache-dir", type=Path, default=None)
    parser.add_argument("--calibre-timeout", type=int, default=30)
    parser.add_argument("--description-min-words", type=int, default=60,
                        help="Unter dieser Wortzahl gilt eine Beschreibung als zu kurz. Standard: 60")
    parser.add_argument("--description-min-chars", type=int, default=320,
                        help="Zusätzliche Mindestzeichenzahl für Beschreibungen. Standard: 320")
    parser.add_argument("--pause", type=float, default=0.15)
    args = parser.parse_args()

    db_path = args.db.resolve()
    if not db_path.exists():
        print(f"[FEHLER] Datenbank nicht gefunden: {db_path}", file=sys.stderr)
        return 2

    calibre_exe = None if args.no_calibre else detect_calibre(args.calibre_exe)
    client, env_path = (None, None) if args.no_ai else get_openai_client(args.env)
    if not args.no_ai and client is None:
        print("[HINWEIS] Kein OpenAI-Client verfügbar. Semantik/Web-Fallback werden übersprungen.")
    if env_path:
        print(f"OpenAI-Key:     aus {env_path}")

    cache_root = (args.cache_dir.resolve() if args.cache_dir else db_path.parent / "_cover_storage")
    con = sqlite3.connect(db_path)
    try:
        ensure_cover_schema_compat(con)
        scan_run_id = latest_scan_run_id(con)
        limit = None if args.all else max(1, args.limit)
        books = load_books(
            con, scan_run_id, args.category, limit, max(0, args.offset),
            args.book_id, args.redo,
        )
        if not books:
            print("Keine passenden Bücher zu bearbeiten.")
            return 0

        cur = con.execute(
            "INSERT INTO enrichment_runs(scan_run_id, model, category, requested_limit) VALUES (?, ?, ?, ?)",
            (scan_run_id, None if args.no_ai else args.model, args.category, limit),
        )
        enrichment_run_id = int(cur.lastrowid)
        compat_run_id = create_compat_research_run(
            con, scan_run_id, args.model if client else "providers-only", args.category, limit
        )
        con.commit()

        print(f"Datenbank:      {db_path}")
        print(f"Metadata-Run:   {enrichment_run_id}")
        print(
            f"Altbestand:     "
            f"{'Inhalte ignoriert; vertrauenswürdige Werkidentität bleibt als Such-Seed (--fresh)' if args.fresh else 'als Fallback erlaubt'}"
        )
        print(f"Bücher:         {len(books)}")
        print(f"Calibre:        {calibre_exe if calibre_exe else 'aus'}")
        print(f"Audible.de:     {'aus' if args.no_audible else 'an'}")
        print(f"Semantik:       {args.model if client else 'aus'}")
        print(f"Web-Fallback:   {'aus' if args.no_web_fallback or not client else 'nur bei Lücken'}")
        print(f"Cover-Cache:    {cache_root}")
        print(
            f"Text-QA:        >= {max(1, args.description_min_words)} Wörter, "
            f">= {max(1, args.description_min_chars)} Zeichen, Deutsch bei DE-Ausgabe"
        )
        print()

        ok = 0
        failed = 0

        for idx, book in enumerate(books, 1):
            # --fresh soll alte Metadaten NICHT wiederverwenden. Aber die bereits
            # geklärte Identität (Titel/Autor) ist ein Suchschlüssel, kein Inhalt.
            # Ohne diesen Seed würde z.B. ein Scanner-Eintrag "Alina Fox" wieder
            # als Buchtitel behandelt, obwohl das Werk "Tödlicher Kristall" heißt.
            identity_seed = load_trusted_identity_seed(con, book.id)
            existing = identity_only(identity_seed) if args.fresh else load_existing_research(con, book.id)

            identity_web_data: dict[str, Any] | None = None
            identity_web_response = None

            # True cold start: weak scanner identity gets a one-off web identity
            # pass BEFORE Calibre/Audible. This lets a brand-new DB bootstrap itself
            # instead of depending on old research rows.
            if (
                client
                and not args.no_web_fallback
                and needs_identity_bootstrap(book, identity_seed)
            ):
                try:
                    print(
                        f"[{idx:>3}/{len(books)}] "
                        f"{book.author_guess or '?'} | {book.title_guess or book.folder_name}",
                        flush=True,
                    )
                    print("    Identität: suche ...", flush=True)
                    identity_web_data, identity_web_response = run_web_fallback(
                        client,
                        args.model,
                        identity_bootstrap_prompt(book),
                    )
                    if (
                        identity_web_data.get("matched")
                        and float(identity_web_data.get("confidence") or 0.0) >= 0.75
                        and identity_web_data.get("canonical_title")
                        and identity_web_data.get("authors")
                    ):
                        identity_seed = existing_from_web_identity(identity_web_data)
                        existing = identity_only(identity_seed) if args.fresh else identity_seed
                        print(
                            f"    Identität: OK {identity_seed.confidence:.2f} | "
                            f"{', '.join(identity_seed.authors or [])} | "
                            f"{identity_seed.canonical_title}",
                            flush=True,
                        )
                    else:
                        print(
                            f"    Identität: unsicher "
                            f"{float(identity_web_data.get('confidence') or 0.0):.2f}",
                            flush=True,
                        )
                except Exception as exc:
                    con.execute(
                        "INSERT INTO enrichment_errors(enrichment_run_id, book_id, stage, error_text) "
                        "VALUES (?, ?, ?, ?)",
                        (
                            enrichment_run_id,
                            book.id,
                            "identity_bootstrap",
                            f"{type(exc).__name__}: {exc}",
                        ),
                    )
                    con.commit()
                    print(f"    Identität: FEHLER | {type(exc).__name__}: {exc}", flush=True)

            title_hint = (
                identity_seed.canonical_title
                or book.title_guess
                or book.folder_name
            )
            author_hints = (
                identity_seed.authors
                or ([book.author_guess] if book.author_guess else [])
            )

            if identity_web_data is None:
                print(
                    f"[{idx:>3}/{len(books)}] "
                    f"{', '.join(author_hints) or '?'} | {title_hint}",
                    flush=True,
                )

            try:
                calibre = CalibreData(error="Calibre deaktiviert")
                if calibre_exe:
                    print("    Calibre:  suche ...", flush=True)
                    calibre = fetch_calibre_enrichment(
                        calibre_exe, title_hint, author_hints,
                        None if args.fresh else normalize_isbn(existing.isbn13),
                        args.calibre_timeout,
                    )

                    if calibre.found:
                        accepted, cscore, tscore, ascore = assess_calibre_match(
                            calibre, title_hint, author_hints
                        )
                        if not accepted:
                            bad_title = calibre.title or "?"
                            calibre = reject_calibre_identity_mismatch(
                                calibre,
                                combined=cscore,
                                title_score=tscore,
                                author_score=ascore,
                            )
                            print(
                                f"    Calibre:  VERWORFEN | {bad_title} | Match {cscore:.3f}",
                                flush=True,
                            )

                store_calibre_row(con, enrichment_run_id, book.id, calibre)
                if calibre.found:
                    print(f"    Calibre:  OK | {calibre.title or '?'} | ISBN {calibre.isbn13 or '-'}")
                elif not (calibre.error or "").startswith("Treffer verworfen:"):
                    print(f"    Calibre:  -- | {calibre.error or 'kein Treffer'}")

                audible = AudibleData(error="Audible deaktiviert")
                if not args.no_audible:
                    print("    Audible:  suche ...", flush=True)
                    q_title = calibre.title if calibre.found and calibre.title else title_hint
                    q_authors = calibre.authors if calibre.found and calibre.authors else author_hints
                    audible = fetch_audible_enrichment(
                        q_title, q_authors,
                        calibre.series if calibre.found and calibre.series else identity_seed.series,
                        calibre.series_number if calibre.found and calibre.series_number else identity_seed.series_number,
                        args.audible_min_score,
                    )
                store_audible_row(con, enrichment_run_id, book.id, audible)
                if audible.found:
                    print(
                        f"    Audible:  OK {audible.match_score:.3f} | {audible.title or '?'}"
                        + (f" | {audible.runtime_length_min} min" if audible.runtime_length_min else "")
                    )
                else:
                    print(f"    Audible:  -- | {audible.error or 'kein Treffer'}")

                merged, provenance = merge_initial(
                    book, existing, calibre, audible,
                    min_description_words=max(1, args.description_min_words),
                    min_description_chars=max(1, args.description_min_chars),
                )

                # Reuse information already obtained by the cold-start identity pass.
                # This avoids paying for the same web research twice.
                web_data: dict[str, Any] | None = identity_web_data
                web_response = identity_web_response
                web_used = bool(identity_web_data)

                if identity_web_data:
                    merge_web_fallback(
                        merged,
                        provenance,
                        identity_web_data,
                        min_description_words=max(1, args.description_min_words),
                        min_description_chars=max(1, args.description_min_chars),
                    )

                needs_desc_upgrade, desc_quality = description_needs_upgrade(
                    merged,
                    min_words=max(1, args.description_min_words),
                    min_chars=max(1, args.description_min_chars),
                )
                if needs_desc_upgrade:
                    reason_text = ", ".join(desc_quality.get("reasons") or [])
                    print(
                        f"    Text-QA:  Nachsuche nötig | "
                        f"{desc_quality.get('word_count', 0)} Wörter | "
                        f"Sprache={desc_quality.get('language', 'unknown')}"
                        + (f" | {reason_text}" if reason_text else "")
                    )
                else:
                    print(
                        f"    Text-QA:  OK | {desc_quality.get('word_count', 0)} Wörter | "
                        f"Sprache={desc_quality.get('language', 'unknown')}"
                    )

                if client and not args.no_web_fallback and needs_web_fallback(
                    merged,
                    min_description_words=max(1, args.description_min_words),
                    min_description_chars=max(1, args.description_min_chars),
                    provenance=provenance,
                ):
                    try:
                        print("    Web:      suche gezielt ...", flush=True)
                        web_data, web_response = run_web_fallback(
                            client, args.model, web_fallback_prompt(
                                book,
                                {**merged, "_herkunft": {k: v for k, v in provenance.items() if k != "description_quality"}},
                                description_quality=desc_quality,
                                min_description_words=max(1, args.description_min_words),
                            )
                        )
                        merge_web_fallback(
                            merged, provenance, web_data,
                            min_description_words=max(1, args.description_min_words),
                            min_description_chars=max(1, args.description_min_chars),
                        )
                        web_used = True
                        print(f"    Web:      FALLBACK {float(web_data.get('confidence') or 0):.2f}")
                    except Exception as exc:
                        con.execute(
                            "INSERT INTO enrichment_errors(enrichment_run_id, book_id, stage, error_text) VALUES (?, ?, ?, ?)",
                            (enrichment_run_id, book.id, "web_fallback", f"{type(exc).__name__}: {exc}"),
                        )
                        print(f"    Web:      FEHLER {type(exc).__name__}: {exc}")

                # v0.5.5: festgelegte Reihe (hoerbuch_reihen_v0_1.py) hat Vorrang vor jeder Recherche
                override = load_series_override(con, book.id)
                if override:
                    merged["series"], merged["series_number"] = override
                    provenance["series"] = provenance["series_number"] = "series_override"
                    print(f"    Reihe:    festgelegt | {override[0]}" + (f" #{override[1]}" if override[1] else ""))

                semantic = default_semantic_from_existing(existing)
                semantic_response = None
                if client:
                    try:
                        print("    Semantik: analysiere ...", flush=True)
                        semantic, semantic_response = run_semantic(
                            client, args.model, semantic_prompt(book, merged, calibre, audible)
                        )
                        print(
                            f"    Semantik: {semantic.get('primary_genre') or '?'} | "
                            f"{semantic.get('audience') or 'unknown'}"
                        )
                    except Exception as exc:
                        con.execute(
                            "INSERT INTO enrichment_errors(enrichment_run_id, book_id, stage, error_text) VALUES (?, ?, ?, ?)",
                            (enrichment_run_id, book.id, "semantic", f"{type(exc).__name__}: {exc}"),
                        )
                        print(f"    Semantik: FEHLER {type(exc).__name__}: {exc}")

                ratings = list(existing.ratings or [])
                ratings.extend(audible_rating_rows(audible))
                sources = combine_sources(existing, audible, web_data)

                cover_saved = False
                if audible.found and audible.product_images:
                    try:
                        cover_saved = store_audible_cover(
                            con, book, audible, db_path, cache_root, args.cover_size
                        )
                    except Exception as exc:
                        con.execute(
                            "INSERT INTO enrichment_errors(enrichment_run_id, book_id, stage, error_text) VALUES (?, ?, ?, ?)",
                            (enrichment_run_id, book.id, "cover", f"{type(exc).__name__}: {exc}"),
                        )
                        print(f"    Cover:    FEHLER {type(exc).__name__}: {exc}")
                if cover_saved:
                    print("    Cover:    Audible gespeichert")
                elif has_locked_manual_cover(con, book.id):
                    print("    Cover:    manuell gesperrt, nicht angefasst")
                else:
                    print("    Cover:    kein automatisches Cover")

                store_book_enrichment(
                    con, enrichment_run_id, book.id, merged, semantic, ratings, sources,
                    provenance, args.model if client else None, semantic_response, web_used,
                )
                raw_bundle = {
                    "calibre": {"found": calibre.found, "error": calibre.error},
                    "audible": audible.raw or {},
                    "semantic": semantic,
                    "web_fallback": web_data,
                    "provenance": provenance,
                }
                insert_compat_book_research(
                    con, compat_run_id, book.id, args.model if client else "providers-only",
                    merged, semantic, ratings, sources, raw_bundle,
                    getattr(semantic_response, "id", None) if semantic_response else None,
                )
                con.commit()
                ok += 1

                desc_src = merged.get("description_source") or "-"
                print(
                    f"    FINAL:    {float(merged.get('confidence') or 0):.2f} | "
                    f"Beschreibung={desc_src} | {merged.get('canonical_title') or '?'}"
                )

            except KeyboardInterrupt:
                print("\nAbbruch durch Benutzer.")
                con.commit()
                return 130
            except Exception as exc:
                failed += 1
                msg = f"{type(exc).__name__}: {exc}"
                con.execute(
                    "INSERT INTO enrichment_errors(enrichment_run_id, book_id, stage, error_text) VALUES (?, ?, ?, ?)",
                    (enrichment_run_id, book.id, "book", msg),
                )
                con.commit()
                print(f"    FEHLER: {msg}")

            time.sleep(max(0.0, args.pause))

        con.execute(
            """UPDATE enrichment_runs SET finished_at=CURRENT_TIMESTAMP,
               processed_books=?, successful_books=?, failed_books=? WHERE id=?""",
            (len(books), ok, failed, enrichment_run_id),
        )
        if compat_run_id is not None:
            con.execute(
                """UPDATE research_runs SET finished_at=CURRENT_TIMESTAMP,
                   processed_books=?, successful_books=?, failed_books=? WHERE id=?""",
                (len(books), ok, failed, compat_run_id),
            )
        con.commit()

        print()
        print("=" * 88)
        print("Metadata-Lauf abgeschlossen")
        print(f"Verarbeitet: {len(books)}")
        print(f"Erfolgreich: {ok}")
        print(f"Fehler:      {failed}")
        print(f"DB:          {db_path}")
        print("Mediendateien wurden NICHT verändert.")
        print("=" * 88)
        return 0 if failed == 0 else 1

    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
