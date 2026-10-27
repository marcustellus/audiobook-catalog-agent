#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hoerbuch_scanner.py (frueher hoerbuch_katalog_v0_2.py)
Version 0.2 - Read-only Scanner für eine bestehende Hörbuchsammlung.

Änderungen gegenüber v0.1:
- erkennt auch benannte CD/Disc-Unterordner wie
  "David Safier - Muh - CD1", "... CD2" als Teile EINES Hörbuchs
- interne Vollständigkeitsprüfung:
    * physisch gefundene Audiodateien
    * katalogisierten Büchern zugeordnete Audiodateien
    * absichtlich ignorierte Audiodateien (z.B. Booklet)
    * unzugeordnete Audiodateien
    * versehentlich doppelt zugeordnete Audiodateien
- schreibt zusätzlich hoerbuch_scan_audit.csv
- weiterhin vollständig read-only gegenüber dem Medienbestand

Keine Internetzugriffe. Keine Umbenennungen. Keine Änderungen an Mediendateien.
Nur Python-Standardbibliothek erforderlich.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import re
import sqlite3
import sys
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


DEFAULT_ROOT = Path(".")   # Wurzel der Sammlung; besser immer --root angeben
DEFAULT_DB = Path("hoerbuch_katalog.sqlite3")
DEFAULT_UNCERTAIN_CSV = Path("hoerbuch_unsicher_v0_2.csv")
DEFAULT_AUDIT_CSV = Path("hoerbuch_scan_audit.csv")

AUDIO_EXTENSIONS = {
    ".mp3", ".m4b", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wav", ".wma"
}

IGNORE_DIR_NAMES = {
    "ebook", "ebooks", "e-book", "e-books",
    "cover", "covers", "bilder", "images", "image",
    "maps", "karten", "booklet", "booklets",
    "soundtrack", "extras", "extra",
    "_reflinks für unterstützung",
    "unterstützung für re-uploads",
}

# Unterordner, deren GANZER Name nur eine physische Unterteilung bezeichnet.
SIMPLE_PART_DIR_RE = re.compile(
    r"""(?ix)^
    (?:
        cd[\s._-]*0*\d+ |
        disc[\s._-]*0*\d+ |
        disk[\s._-]*0*\d+ |
        teil[\s._-]*0*\d+ |
        part[\s._-]*0*\d+ |
        kapitel[\s._-]*0*\d+ |
        chapter[\s._-]*0*\d+ |
        audio |
        hörbuch |
        hoerbuch |
        \d{1,3}
    )
    (?:\s.*)?$
    """
)

# Benannte physische Unterteilung am ENDE:
# "David Safier - Muh - CD1", "Assassini CD 7", "Die Kaiserattentate - Disc 02"
NAMED_DISC_PART_RE = re.compile(
    r"""(?ix)^
    (?P<base>.+?)
    \s*[-_()]?\s*
    (?P<kind>cd|disc|disk)
    [\s._-]*0*(?P<number>\d{1,3})
    \s*$
    """
)

EDITION_NOISE_RE = re.compile(
    r"""(?ix)
    [\[(]?
    \b(
        ungekürzt|ungekuerzt|gekürzt|gekuerzt|
        hörbuch|hoerbuch|audiobook|
        vollständige\s+fassung|vollstaendige\s+fassung
    )\b
    [\])]?
    """
)

VOLUME_RE = re.compile(
    r"""(?ix)
    (?:
        \b(?:band|teil|folge|volume|vol\.?)\s*0*(\d{1,3})\b
        |
        (?<!\d)\b0*(\d{1,3})\b(?=\s*[-_:])
    )
    """
)


@dataclass
class DirInfo:
    path: Path
    direct_audio: list[Path] = field(default_factory=list)
    media_children: list["DirInfo"] = field(default_factory=list)

    @property
    def has_media(self) -> bool:
        return bool(self.direct_audio or self.media_children)


@dataclass
class BookCandidate:
    path: Path
    reason: str
    confidence: float
    audio_files: list[Path]

    @property
    def audio_count(self) -> int:
        return len(self.audio_files)

    @property
    def total_bytes(self) -> int:
        total = 0
        for p in self.audio_files:
            try:
                total += p.stat().st_size
            except OSError:
                pass
        return total


def folded_name(text: str) -> str:
    """Vergleichsform: Unicode normalisieren, Kleinbuchstaben, nur alphanumerisch."""
    text = unicodedata.normalize("NFKC", text).casefold()
    return "".join(ch for ch in text if ch.isalnum())


# Wurzeln der Sammlung. Ordner mit "_" am Anfang DIREKT darunter (_Verwaltung, _Gehört, ...)
# sind Verwaltung, kein Bestand. Tiefer liegende "_"-Ordner (z.B. Comedy\_Volker Pispers) bleiben Buecher.
LIBRARY_ROOTS: set[str] = set()


def _norm(p: Path) -> str:
    try:
        return str(p.resolve()).casefold()
    except OSError:
        return str(p.absolute()).casefold()


def is_ignored_dir(path: Path) -> bool:
    name = path.name.strip()
    if name.startswith("_") and _norm(path.parent) in (LIBRARY_ROOTS or {_norm(DEFAULT_ROOT)}):
        return True
    return name.casefold() in {x.casefold() for x in IGNORE_DIR_NAMES}


def is_simple_part_dir(path: Path) -> bool:
    return bool(SIMPLE_PART_DIR_RE.match(path.name.strip()))


def safe_scandir(path: Path) -> tuple[list[Path], list[Path]]:
    files: list[Path] = []
    dirs: list[Path] = []
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    p = Path(entry.path)
                    if entry.is_dir(follow_symlinks=False):
                        dirs.append(p)
                    elif entry.is_file(follow_symlinks=False):
                        files.append(p)
                except OSError:
                    continue
    except OSError as exc:
        print(f"[WARN] Kann Verzeichnis nicht lesen: {path} ({exc})", file=sys.stderr)
    return files, dirs


def collect_audio_from_fs(path: Path) -> list[Path]:
    """Sammelt Audiodateien rekursiv direkt aus dem Dateisystem."""
    result: list[Path] = []
    try:
        for current, dirs, files in os.walk(path):
            for name in files:
                p = Path(current) / name
                if p.suffix.casefold() in AUDIO_EXTENSIONS:
                    result.append(p)
    except OSError:
        pass
    return result


def build_tree(path: Path, ignored_audio: list[Path]) -> DirInfo:
    files, dirs = safe_scandir(path)
    direct_audio = [p for p in files if p.suffix.casefold() in AUDIO_EXTENSIONS]
    info = DirInfo(path=path, direct_audio=direct_audio)

    for child in dirs:
        if is_ignored_dir(child):
            # Nicht katalogisieren, aber für die Vollständigkeitsprüfung mitzählen.
            ignored_audio.extend(collect_audio_from_fs(child))
            continue

        child_info = build_tree(child, ignored_audio)
        if child_info.has_media:
            info.media_children.append(child_info)

    return info


def gather_audio(node: DirInfo) -> list[Path]:
    out = list(node.direct_audio)
    for child in node.media_children:
        out.extend(gather_audio(child))
    return out


def all_children_are_simple_parts(node: DirInfo) -> bool:
    return bool(node.media_children) and all(
        is_simple_part_dir(c.path) for c in node.media_children
    )


def named_disc_partition_group(node: DirInfo) -> bool:
    """
    Erkennt sichere CD/Disc-Sätze, obwohl die Unterordner zusätzlich den Buchtitel tragen.

    Beispiel:
      Eltern: David Safier - MUH!
      Kinder:
        David Safier - Muh - CD1
        David Safier - Muh - CD2
        ...
    """
    if len(node.media_children) < 2:
        return False

    parsed: list[tuple[str, int]] = []
    for child in node.media_children:
        m = NAMED_DISC_PART_RE.match(child.path.name.strip())
        if not m:
            return False
        parsed.append((m.group("base").strip(), int(m.group("number"))))

    bases = {folded_name(base) for base, _ in parsed}
    if len(bases) != 1:
        return False

    numbers = [n for _, n in parsed]
    if len(numbers) != len(set(numbers)):
        return False

    base_folded = next(iter(bases))
    parent_folded = folded_name(node.path.name)

    # Sicherheitsbremse:
    # Der gemeinsame Basistitel muss im Elternordner erkennbar sein
    # oder umgekehrt. Damit werden zufällige CD1/CD2-Gruppen nicht vermischt.
    if not (
        base_folded in parent_folded
        or parent_folded in base_folded
    ):
        return False

    return True


def choose_candidates(node: DirInfo, root: Path) -> list[BookCandidate]:
    """
    Heuristik:
    1. Direkte Audiodateien im Ordner -> meist Buch.
    2. Mehrere reine CD/Disc/Teil-Kinder -> Elternordner ist Buch.
    3. Benannte CD/Disc-Kinder mit gemeinsamem Basistitel -> Elternordner ist Buch.
    4. Genau ein medienführender Unterordner -> Wrapper überspringen.
    5. Mehrere inhaltliche Medienkinder -> Serien-/Sammelcontainer, Kinder analysieren.
    """
    if not node.has_media:
        return []

    is_root = node.path.resolve() == root.resolve()

    # Audio direkt im Ordner.
    if node.direct_audio and not is_root:
        confidence = 0.95
        reason = "direct_audio"

        nonpart = [c for c in node.media_children if not is_simple_part_dir(c.path)]
        if nonpart:
            confidence = 0.55
            reason = "direct_audio_plus_nested_media"

        return [BookCandidate(
            path=node.path,
            reason=reason,
            confidence=confidence,
            audio_files=gather_audio(node),
        )]

    # Reine Unterteilungsordner wie CD1/CD2/Teil1/Teil2.
    if not is_root and all_children_are_simple_parts(node):
        return [BookCandidate(
            path=node.path,
            reason="simple_part_children_collapsed",
            confidence=0.95,
            audio_files=gather_audio(node),
        )]

    # Benannte CD/Disc-Sätze.
    if not is_root and named_disc_partition_group(node):
        return [BookCandidate(
            path=node.path,
            reason="named_disc_children_collapsed",
            confidence=0.95,
            audio_files=gather_audio(node),
        )]

    # Genau ein Medienzweig: Wrapper überspringen.
    if len(node.media_children) == 1:
        child = node.media_children[0]

        if not is_root and is_simple_part_dir(child.path):
            return [BookCandidate(
                path=node.path,
                reason="single_part_child_collapsed",
                confidence=0.90,
                audio_files=gather_audio(node),
            )]

        return choose_candidates(child, root)

    # Mehrere echte Medienkinder -> wahrscheinlich Seriencontainer.
    result: list[BookCandidate] = []
    for child in node.media_children:
        result.extend(choose_candidates(child, root))
    return result


def cleanup_title(text: str) -> str:
    text = text.replace("_", " ")
    text = EDITION_NOISE_RE.sub("", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s*[-–—]\s*$", "", text)
    text = re.sub(r"^\s*[-–—]\s*", "", text)
    return text.strip()


def parse_folder_name(name: str) -> dict[str, str | int | None]:
    clean = cleanup_title(name)

    volume: int | None = None
    m = VOLUME_RE.search(clean)
    if m:
        raw = m.group(1) or m.group(2)
        if raw:
            try:
                volume = int(raw)
            except ValueError:
                pass

    author: str | None = None
    title_guess = clean

    parts = re.split(r"\s+-\s+", clean, maxsplit=1)
    if len(parts) == 2:
        left, right = parts
        if len(left) >= 3 and not re.fullmatch(r"\d+", left):
            author = left.strip()
            title_guess = right.strip()

    return {
        "folder_name": name,
        "author_guess": author,
        "title_guess": title_guess,
        "volume_guess": volume,
    }


def make_fingerprint(book_path: Path, audio_files: Iterable[Path]) -> str:
    h = hashlib.blake2b(digest_size=20)

    rows: list[tuple[str, int]] = []
    for p in audio_files:
        try:
            rel = p.relative_to(book_path).as_posix().casefold()
        except ValueError:
            rel = p.name.casefold()

        try:
            size = p.stat().st_size
        except OSError:
            size = -1

        rows.append((rel, size))

    for rel, size in sorted(rows):
        h.update(rel.encode("utf-8", errors="replace"))
        h.update(b"\0")
        h.update(str(size).encode("ascii"))
        h.update(b"\n")

    return h.hexdigest()


def confidence_label(value: float) -> str:
    if value >= 0.90:
        return "hoch"
    if value >= 0.70:
        return "mittel"
    return "niedrig"


SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS scan_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    root_path TEXT NOT NULL,
    scanned_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    physical_audio_count INTEGER NOT NULL,
    assigned_unique_audio_count INTEGER NOT NULL,
    ignored_audio_count INTEGER NOT NULL,
    unassigned_audio_count INTEGER NOT NULL,
    duplicate_assignment_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS books (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_run_id INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    absolute_path TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    folder_name TEXT NOT NULL,

    author_guess TEXT,
    title_guess TEXT,
    volume_guess INTEGER,

    detection_reason TEXT NOT NULL,
    detection_confidence REAL NOT NULL,

    audio_count INTEGER NOT NULL,
    total_bytes INTEGER NOT NULL,

    FOREIGN KEY(scan_run_id) REFERENCES scan_runs(id)
);

CREATE INDEX IF NOT EXISTS idx_books_fingerprint ON books(fingerprint);
CREATE INDEX IF NOT EXISTS idx_books_relative_path ON books(relative_path);
CREATE INDEX IF NOT EXISTS idx_books_confidence ON books(detection_confidence);
"""


def path_key(p: Path) -> str:
    try:
        return str(p.resolve()).casefold()
    except OSError:
        return str(p.absolute()).casefold()


def audit_assignments(
    tree: DirInfo,
    ignored_audio: list[Path],
    candidates: list[BookCandidate],
) -> dict[str, object]:
    active_audio = gather_audio(tree)

    assignment_counter: Counter[str] = Counter()
    path_lookup: dict[str, Path] = {}

    for candidate in candidates:
        for p in candidate.audio_files:
            k = path_key(p)
            assignment_counter[k] += 1
            path_lookup[k] = p

    active_keys = {path_key(p): p for p in active_audio}
    ignored_keys = {path_key(p): p for p in ignored_audio}

    assigned_unique_keys = set(assignment_counter)
    unassigned_keys = set(active_keys) - assigned_unique_keys
    duplicate_keys = {k for k, count in assignment_counter.items() if count > 1}

    return {
        "physical_audio_count": len(active_keys) + len(ignored_keys),
        "active_audio_count": len(active_keys),
        "assigned_unique_audio_count": len(assigned_unique_keys),
        "ignored_audio_count": len(ignored_keys),
        "unassigned_audio": [active_keys[k] for k in sorted(unassigned_keys)],
        "duplicate_audio": [path_lookup[k] for k in sorted(duplicate_keys)],
        "ignored_audio": [ignored_keys[k] for k in sorted(ignored_keys)],
    }


def write_database(
    db_path: Path,
    root: Path,
    candidates: list[BookCandidate],
    audit: dict[str, object],
) -> int:
    # v0.2 ist absichtlich eine eigene DB-Datei. Wenn sie schon existiert,
    # bleiben frühere Scanläufe darin erhalten.
    con = sqlite3.connect(db_path)
    try:
        con.executescript(SCHEMA)

        cur = con.execute(
            """
            INSERT INTO scan_runs(
                root_path,
                physical_audio_count,
                assigned_unique_audio_count,
                ignored_audio_count,
                unassigned_audio_count,
                duplicate_assignment_count
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                str(root),
                int(audit["physical_audio_count"]),
                int(audit["assigned_unique_audio_count"]),
                int(audit["ignored_audio_count"]),
                len(audit["unassigned_audio"]),
                len(audit["duplicate_audio"]),
            ),
        )
        scan_run_id = int(cur.lastrowid)

        for c in candidates:
            parsed = parse_folder_name(c.path.name)
            fingerprint = make_fingerprint(c.path, c.audio_files)

            try:
                rel = c.path.relative_to(root)
            except ValueError:
                rel = c.path

            con.execute(
                """
                INSERT INTO books (
                    scan_run_id, fingerprint,
                    absolute_path, relative_path, folder_name,
                    author_guess, title_guess, volume_guess,
                    detection_reason, detection_confidence,
                    audio_count, total_bytes
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    scan_run_id,
                    fingerprint,
                    str(c.path),
                    str(rel),
                    c.path.name,
                    parsed["author_guess"],
                    parsed["title_guess"],
                    parsed["volume_guess"],
                    c.reason,
                    c.confidence,
                    c.audio_count,
                    c.total_bytes,
                ),
            )

        con.commit()
        return scan_run_id
    finally:
        con.close()


def write_uncertain_csv(
    csv_path: Path,
    root: Path,
    candidates: list[BookCandidate],
    threshold: float = 0.90,
) -> int:
    uncertain = [c for c in candidates if c.confidence < threshold]

    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow([
            "confidence",
            "reason",
            "relative_path",
            "audio_count",
            "total_bytes",
        ])

        for c in sorted(uncertain, key=lambda x: (x.confidence, str(x.path).casefold())):
            try:
                rel = c.path.relative_to(root)
            except ValueError:
                rel = c.path

            writer.writerow([
                f"{c.confidence:.2f}",
                c.reason,
                str(rel),
                c.audio_count,
                c.total_bytes,
            ])

    return len(uncertain)


def write_audit_csv(
    csv_path: Path,
    root: Path,
    audit: dict[str, object],
) -> None:
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(["status", "relative_path"])

        for status, key in [
            ("IGNORED", "ignored_audio"),
            ("UNASSIGNED", "unassigned_audio"),
            ("DUPLICATE_ASSIGNMENT", "duplicate_audio"),
        ]:
            for p in audit[key]:
                try:
                    rel = p.relative_to(root)
                except ValueError:
                    rel = p
                writer.writerow([status, str(rel)])


def print_preview(root: Path, candidates: list[BookCandidate], limit: int = 25) -> None:
    print()
    print("Beispiel erkannter Hörbuch-Einheiten:")
    print("-" * 88)

    for c in sorted(candidates, key=lambda x: str(x.path).casefold())[:limit]:
        try:
            rel = c.path.relative_to(root)
        except ValueError:
            rel = c.path
        print(
            f"[{confidence_label(c.confidence):7}] "
            f"{c.audio_count:4d} Dateien  {rel}"
        )

    if len(candidates) > limit:
        print(f"... und {len(candidates) - limit} weitere")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only Scanner für eine Hörbuchsammlung"
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=DEFAULT_ROOT,
        help=rf"Wurzelordner der Sammlung (Standard: {DEFAULT_ROOT})",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB,
        help=f"SQLite-Ausgabedatei (Standard: {DEFAULT_DB})",
    )
    parser.add_argument(
        "--uncertain-csv",
        type=Path,
        default=DEFAULT_UNCERTAIN_CSV,
        help=f"CSV mit unsicheren Fällen (Standard: {DEFAULT_UNCERTAIN_CSV})",
    )
    parser.add_argument(
        "--audit-csv",
        type=Path,
        default=DEFAULT_AUDIT_CSV,
        help=f"Audit-CSV (Standard: {DEFAULT_AUDIT_CSV})",
    )
    parser.add_argument(
        "--preview",
        type=int,
        default=25,
        help="Anzahl Beispieltreffer auf der Konsole",
    )

    args = parser.parse_args()

    root = args.root.expanduser()
    LIBRARY_ROOTS.update({_norm(DEFAULT_ROOT), _norm(root)})
    if not root.exists() or not root.is_dir():
        print(f"[FEHLER] Hörbuch-Wurzel existiert nicht: {root}", file=sys.stderr)
        return 2

    print(f"Scanne read-only: {root}")
    print("Baue Verzeichnisbaum ...")

    ignored_audio: list[Path] = []
    tree = build_tree(root, ignored_audio)

    print("Ermittle Hörbuch-Einheiten ...")
    candidates = choose_candidates(tree, root)

    # Doppelte Kandidatenpfade entfernen.
    dedup: dict[str, BookCandidate] = {}
    for c in candidates:
        dedup[path_key(c.path)] = c
    candidates = list(dedup.values())
    candidates.sort(key=lambda c: str(c.path).casefold())

    print("Prüfe Vollständigkeit und Überschneidungen ...")
    audit = audit_assignments(tree, ignored_audio, candidates)

    print_preview(root, candidates, args.preview)

    args.db.parent.mkdir(parents=True, exist_ok=True)
    args.uncertain_csv.parent.mkdir(parents=True, exist_ok=True)
    args.audit_csv.parent.mkdir(parents=True, exist_ok=True)

    scan_run_id = write_database(args.db, root, candidates, audit)
    uncertain_count = write_uncertain_csv(args.uncertain_csv, root, candidates)
    write_audit_csv(args.audit_csv, root, audit)

    total_bytes = sum(c.total_bytes for c in candidates)

    print()
    print("=" * 92)
    print(f"Scan-ID:                         {scan_run_id}")
    print(f"Erkannte Hörbuch-Einheiten:      {len(candidates)}")
    print(f"Physisch gefundene Audiodateien: {audit['physical_audio_count']}")
    print(f"Katalogisiert (eindeutig):       {audit['assigned_unique_audio_count']}")
    print(f"Absichtlich ignoriert:           {audit['ignored_audio_count']}")
    print(f"Unzugeordnet:                     {len(audit['unassigned_audio'])}")
    print(f"Doppelt zugeordnet:               {len(audit['duplicate_audio'])}")
    print(f"Katalogisierte Größe:             {total_bytes / (1024**3):.2f} GiB")
    print(f"Unsichere Buch-Einheiten (<0.90): {uncertain_count}")
    print(f"SQLite:                           {args.db.resolve()}")
    print(f"Unsicher-CSV:                     {args.uncertain_csv.resolve()}")
    print(f"Audit-CSV:                        {args.audit_csv.resolve()}")
    print("=" * 92)

    if audit["unassigned_audio"] or audit["duplicate_audio"]:
        print()
        print("[ACHTUNG] Der Scan ist noch nicht strukturell vollständig.")
        print("Bitte hoerbuch_scan_audit.csv prüfen.")
        return 1

    print()
    print("Strukturprüfung bestanden: keine unzugeordneten oder doppelt zugeordneten Audiodateien.")
    print("WICHTIG: Das Programm verändert keine Datei und keinen Ordner im Hörbuchbestand.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
