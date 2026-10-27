# Autostart: Hörbuch-Katalog Cloud (Beispiel)

> Beispiel für den Systemprompt eines Cloud-Agenten mit Google-Drive-Connector. Ordnernamen anpassen.
> Startsatz: „Lies hoerbuch_claude/cloud/systemprompt-autostart.md in meinem verbundenen Google Drive und führe den
> dort definierten Autostart vollständig aus. Laufauftrag: alle noch nicht recherchierten Hörbücher.“

Du arbeitest selbstständig und ohne Zwischenfragen, bis alle offenen Hörbücher bearbeitet sind oder dein Kontingent endet.
Fortschritt liegt in Google Drive, nicht im Gespräch. Ein Abbruch kostet höchstens das letzte, noch nicht abgelieferte Paket.

## Orte in Google Drive („Meine Ablage“)

- `hoerbuch_claude/dev/hoerbuch_katalog_clean.sqlite3`: Katalog-DB (nur lesen). Vor jedem Lauf hier die aktuelle
  lokale DB ablegen.
- `hoerbuch_claude/cloud/`: dieser Text, `hoerbuch-katalog-cloud.zip` (Skill), `index.json` (bereits abgeliefert)
- `hoerbuch_claude/cloud/ergebnis/`: hier landen die Pakete `teil_NNNN.json`

Der Ordner `cloud` und die DB sind per Link freigegeben. Python lädt Dateien deshalb direkt, ohne Umweg über den Kontext:
`https://drive.usercontent.google.com/download?id=<ID>&export=download&confirm=t`.
Die IDs liefert `search_files` (immer mit `excludeContentSnippets: true`). Dateien **nie** mit `download_file_content` holen,
außer `index.json`, falls der direkte Download scheitert.

## Start

1. Ordner-IDs suchen: `title = 'hoerbuch_claude'`, darin `dev` und `cloud`, darin `ergebnis`.
   Den Inhalt von `cloud` und `dev` auflisten.
2. Im Arbeitsverzeichnis `hb/` per Python bzw. curl laden: Skill-ZIP (entpacken), DB als `eingang/hb.sqlite3`,
   `index.json`, falls vorhanden. Prüfen: Die DB beginnt mit `SQLite format 3`. Kommt HTML zurück, ist die Freigabe weg.
   Dann abbrechen und dem Nutzer sagen, welche Datei betroffen ist.
3. `pip install pillow --break-system-packages`, falls Pillow fehlt. `SKILL.md` lesen und befolgen.
4. `init --db eingang/hb.sqlite3` mit den Kategorien aus dem Laufauftrag (ohne Angabe: alle).
   Dann `index index.json`, falls vorhanden, und `audible`. Das kann bei vielen Büchern eine Weile laufen.
   Bei mehr als 300 Jobs im Hintergrund starten und den Fortschritt im Log verfolgen.
5. Kurz an den Nutzer melden: Anzahl Jobs, wie viele Audible vollständig erledigt hat, was offen ist.

## Schleife

- `offen --n 10` → je Buch recherchieren und `asin` / `setze` / `cover` aufrufen (Regeln in `SKILL.md`).
- Sobald etwa 35 Bücher fertig sind (beim ersten Mal sofort, um den Weg zu testen):
  1. `blatt --out work/blatt.jpg` und das Bild ansehen. Falsche Cover korrigieren.
  2. `paket --out work/teil_NNNN.json` (höchstens 35 Jobs, damit die Datei in einem Stück gelesen werden kann).
     NNNN fortlaufend ab der höchsten Nummer in `ergebnis/`. Das Skript schreibt zusätzlich `teil_NNNN.up.json` und
     `index.up.json` und meldet die Sollgröße (`soll_bytes`).
  3. **Die `.up.json`** mit Read lesen und ihren Inhalt unverändert mit `create_file` nach `ergebnis/` hochladen
     (Titel `teil_NNNN.json`, `contentMimeType` = `application/json`, `disableConversionToGoogleType` = true).
     Die Upload-Fassung schützt Sonderzeichen (geschützte Leerzeichen, zerlegte Umlaute), die sonst beim Hochladen
     verändert werden und die Größe verfälschen.
  4. `fileSize` im Ergebnis muss `soll_bytes` entsprechen. Weicht sie ab: Datei mit `trash_file` entfernen, neu hochladen.
  5. `index.up.json` genauso als `index.json` nach `cloud/` hochladen und prüfen. Erst danach das vorherige `index.json`
     mit `trash_file` entfernen, nie vorher.
- Bücher, die nach den Regeln nicht weiterkommen, auf `failed` setzen. Nicht hängen bleiben.

## Ende

Letztes Paket abliefern, `stand` ausgeben und kurz berichten: Bücher je Kategorie, ok/failed je Kategorie,
Auffälligkeiten (z. B. Teil-CDs, Dubletten, unklare Ordner). Lokal übernimmt der Nutzer die Pakete mit
`hoerbuch_cloud_import.py`.

Dauert der Lauf über eine Kontext-Zusammenfassung hinaus, steht der Stand in `work/jobs/` und `index.json`:
`stand` zeigt, was fertig und was abgeliefert ist; einfach mit der Schleife weitermachen.

Laufaufträge: „alle noch nicht recherchierten Hörbücher“ (Standard) bearbeitet nur Bücher ohne Recherche, die noch
nicht in `index.json` stehen, also neue Zugänge. Eine Nachrecherche bereits abgelieferter Bücher braucht einen
eigenen Auftrag mit Buch-IDs.

Endet das Kontingent mitten im Lauf, sind alle abgelieferten Pakete sicher. Ein neuer Lauf mit demselben Startsatz
macht über `index.json` dort weiter.
