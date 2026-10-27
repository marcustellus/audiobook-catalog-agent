# audiobook-catalog-agent

Tools and field notes for cataloguing a large, messy audiobook folder collection (1,300+ titles) with
**titles, authors, series/volume, descriptions, genres and cover art**, using the public **Audible catalog API**
and an **LLM agent** for the cases scripts cannot decide.

This is a reference implementation from a real project, published as-is so that people and agents looking for
the same thing don't have to rediscover it. It is not maintained. Take what is useful.

> Code comments, CLI messages and the agent instructions are in **German**, and the Audible lookups target
> **audible.de**. The approach transfers to other marketplaces (`api.audible.com`, `.co.uk`, `.fr`, ...).

## What is in here

| Path | What it is |
|---|---|
| [`docs/AUDIBLE_API.md`](docs/AUDIBLE_API.md) | Practical notes on the unauthenticated Audible catalog API: search, `response_groups`, series lookup, placeholder products, cover sizes. **Probably the most reusable part.** |
| [`docs/AGENT_PATTERN.md`](docs/AGENT_PATTERN.md) | A pattern for long-running agent jobs that survive context resets and quota limits: deterministic tool + agent judgement, job files, delivery in packages, idempotent import. |
| `scripts/hoerbuch_scanner.py` | Read-only scanner: walks a folder tree and decides what one "audiobook" is (CD1/CD2 sub-folders, "Title - CD 3", wrapper folders, series containers). Audits that every audio file is assigned exactly once. Standard library only. |
| `scripts/make_schema.py` | Adds the enrichment/cover tables to the scanner's SQLite DB. |
| `scripts/hb_cloud.py` | The agent's tool: creates one job per book, runs an Audible pass (no LLM tokens), shows the agent only what is left, validates covers, builds a contact sheet for visual checks, packs results into JSON packages. |
| `scripts/hoerbuch_cloud_import.py` | Imports the packages into the local DB (checks book id + fingerprint, never overwrites manual covers). |
| `scripts/hoerbuch_metadata_v0_5_5.py`, `scripts/hoerbuch_identitaet_v0_1.py` | Helper modules (Audible client, text cleanup, cover handling). Can also run on their own with an OpenAI key for semantic classification. |
| `skill/SKILL.md` | Instructions for the agent (Claude Agent Skill format), including lessons from the 1,304-book run. |
| `skill/autostart-example.md` | Example system prompt for a cloud agent that uses Google Drive for persistence. |

## Results from the real run

1,304 folders, mixed German audiobooks and radio plays (crime, horror, fantasy, podcasts, ...):

- identity found for 1,245 (95 %), description for 1,160, cover for 1,158
- 1,113 books (85 %) matched to an Audible product, partly by the automatic pass, partly picked by the agent;
  the agent handled the rest with at most 2 web searches per book
- two questionable covers out of 1,158, both flagged on the contact sheet

## Quick start (local, without an agent)

Requires Python 3.10+ and Pillow (`pip install pillow`).

```bash
python scripts/hoerbuch_scanner.py --root /path/to/audiobooks --db katalog.sqlite3
python scripts/make_schema.py katalog.sqlite3

python scripts/hb_cloud.py --work work init --db katalog.sqlite3
python scripts/hb_cloud.py --work work audible            # Audible pass, no LLM needed
python scripts/hb_cloud.py --work work stand              # what is done, what is open
python scripts/hb_cloud.py --work work offen --n 10       # the cases that need judgement
python scripts/hb_cloud.py --work work suche -t "Drangwäsche" -a "Thurner"
python scripts/hb_cloud.py --work work asin 2 B0899LHLY2  # pin a book to an Audible product
python scripts/hb_cloud.py --work work blatt --out sheet.jpg   # contact sheet of chosen covers

python scripts/hb_cloud.py --work work paket --out work/teil_0001.json
python scripts/hoerbuch_cloud_import.py --db katalog.sqlite3 --ordner work          # preview
python scripts/hoerbuch_cloud_import.py --db katalog.sqlite3 --ordner work --apply
```

The whole cycle (scan, Audible pass, package, import with covers) was tested on a fresh mini library before publishing.

## With an agent

Give an LLM agent with a shell the `skill/` folder and the scripts, and let it work through `offen`.
The agent decides (which product is the right edition, whether a cover shows this work, writing a description when
there is none); the script does everything deterministic. See `docs/AGENT_PATTERN.md`.

## Caveats

- The Audible catalog API is **unofficial and undocumented**. It may change or disappear. Be gentle with request rates.
- Descriptions and cover images belong to their publishers. These tools fetch them for a **private** catalogue.
  Do not redistribute the fetched data.
- Folder-name heuristics are tuned to German naming habits ("Band", "Teil", "Folge", "ungekürzt").

## License

[The Unlicense](LICENSE): public domain, no attribution required.

---

**Kurz auf Deutsch:** Werkzeuge und Erfahrungen, um eine große Hörbuch-Ordnersammlung mit Titel, Autor, Reihe, Klappentext,
Genre und Cover zu katalogisieren: Scanner, Audible-Katalog-API, ein Werkzeug für einen LLM-Agenten, Import in SQLite.
Ohne Pflege veröffentlicht, gemeinfrei.
