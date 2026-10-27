# Pattern: long agent runs that survive resets

The run that produced this repository processed 1,304 books over two sessions, two context compactions and a quota
pause, and lost nothing. The design choices that made that work are not audiobook-specific.

## 1. The script does everything deterministic, the agent only judges

`hb_cloud.py` fetches, matches, validates image sizes, counts, packages. The agent never parses JSON by hand
and never sees a book the script already solved: `offen` lists only the cases that need a decision, in one compact
line each. This keeps token use proportional to the hard cases, not to the collection size.

Rule of thumb: if the agent does the same mechanical step three times, it belongs in the tool.

## 2. State lives in files, not in the conversation

- One job file per item (`work/jobs/<id>.json`) with a status per aspect: `pending | pruefen | ok | failed | skipped`.
- `failed` is a valid end state. Nothing blocks on it; the run moves on.
- After a context reset, `stand` tells the agent where it is. No memory of the conversation is needed.

## 3. Deliver in packages, keep an index

- Every ~35 finished items: pack them into `teil_NNNN.json`, upload, verify, then update `index.json`
  (ids already delivered) and only then delete the previous index.
- A new run reads `index.json` first and skips everything delivered. An interrupted run costs at most one package.
- Package size is chosen so the agent can read the file in one piece (large files get truncated by readers).

## 4. Verify uploads by size, and beware of invisible characters

Uploading text through a tool call can silently change it: non-breaking spaces become spaces, decomposed umlauts
(`a` + combining diaeresis) get normalised. The JSON stays valid, but the content and size change.
Fix: write those characters as JSON `\uXXXX` escapes before uploading (same content, ASCII-safe), and compare the
uploaded file size with the local one. `paket` does this and writes a `.up.json` next to each package.

## 5. Idempotent import with a fingerprint

The local import checks book id **and** a fingerprint of the folder's audio files (relative path + size). If the folder
was rescanned in the meantime, the item is skipped instead of attaching data to the wrong book. Each package is
recorded as imported, so running the import twice is harmless. Manual or locked cover choices are never overwritten.

## 6. Make the agent look at what it chose

A contact sheet (one image with ~40 thumbnails and labels) after every package is cheap for a multimodal agent and
catches wrong matches that no string comparison finds.

## 7. Budget per item

At most two web searches and two page fetches per item, then `failed` with a note. Without such a limit, a few
hopeless items eat the budget of hundreds of easy ones.

## 8. Give the agent a goal, not a procedure

The agent instructions (`skill/SKILL.md`) define what "done" means per aspect, the tools, the budget and known traps.
They do not script every step. The agent decides how to reach the goal.
