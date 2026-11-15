# Pipeline and state

## Stages
| Stage | Command | Cost | What happens |
|---|---|---|---|
| scan | `pd scan` | free | Finds new piles (watch folders, the current folder, a given path) and changed files (size+mtime). Finds moved piles again through a `.pile-digest-id` marker. |
| inventory | `pd prepare` | free | Walks the pile safely: skips junk and VCS folders, never follows folder symlinks. Detects types from magic bytes. Unpacks zip/tar/gz/bz2/xz/7z/rar and email attachments within limits: size, compression ratio, member count, nesting depth, no path traversal. Identical files are kept once. |
| convert | `pd prepare` | free | PDFs via poppler, with qpdf/mutool/Ghostscript fallbacks and repair. Office files via LibreOffice → PDF, or pure-Python XML text plus embedded images. EPUB, HTML, RTF, notebooks, email and text through stdlib parsers. Images via Pillow or ImageMagick, or sent as they are. DjVu/PS/XPS go through their converters when installed. |
| triage | `pd prepare` | free | Low-resolution renders give ink, background and a perceptual hash per page. Pages are classed as blank, duplicate (exact or near-identical, confirmed by pixels), repeated-title, title, text-ok, hybrid or vision (see below). |
| estimate | `pd estimate` | free | See `cost-model.md`. |
| pilot | `pd pilot` | small | A stratified sample, including the lowest-resolution pages, read with the cheap model. Calibrates the estimate. |
| summaries | `pd run` | cheap | Text-ok pages get a short summary from the cheap model, about 20 pages per call. A page whose text layer the model calls garbled is reclassified as vision and read from its image in the same run. |
| bulk | `pd run` | cheap | Vision and hybrid pages, about 5 per fresh call. Hybrid pages send their text layer as a hint. |
| escalate | `pd run` | cheap → strong | Flagged pages (illegible, low confidence, cut off, many unsure spots) are re-read as high-resolution tiles by the cheap model. Pages still flagged go to the strong model; pages still flagged after that join the main model's review queue. |
| spotcheck | `pd run` | strong | A random 5% sample (minimum 3) of cheap-model pages, plus every page accepted after tiling, is audited against the image. A mismatch adds the page to the review queue with a suggested correction. |
| digest | `pd run` / `pd digest` | free (+ strong for section digests on big piles) | Builds `digest.md`. |

Classes that need vision:
- **hybrid:** a text layer plus images or drawings, or text dense with formulas;
- **vision:** no usable text layer.

## Fresh calls
Each call is a separate `claude -p` process with:
- a short custom system prompt (the same in every call, so it's cached);
- no tools, MCP, settings, CLAUDE.md or session file;
- one user message with the images inlined.

Nothing builds up between calls.

## Stops and resume
Before each call, the run checks:
- the budget (`--budget` or `budget_tokens`);
- the 5-hour window utilisation reported by the CLI (`max_window_utilization`);
- the free disk space.

A usage-limit, rate-limit or auth error stops the run. Results are saved per page as they arrive, so the same command resumes. A per-pile lock prevents two runs on the same pile at once.

## State layout (`$XDG_STATE_HOME/pile-digest/`, or `$PILE_DIGEST_HOME`)
```
registry.json                 known piles
piles/<id>/inventory.json     every file with its status
piles/<id>/index.json         documents in reading order
piles/<id>/docs/<doc>.json    per-document triage (doc id = content hash, so renames are free)
piles/<id>/classes.json       pile-wide classes (duplicates, titles)
piles/<id>/overrides.json     reclassified pages
piles/<id>/work/<doc>/        converted PDFs, extracted media, unlocked copies
piles/<id>/derived/           unpacked archives and email attachments
piles/<id>/results/<page>.md  transcription with summary and flags; .json alongside it has the metadata
piles/<id>/ledger.jsonl       one line per model call: tokens, cost, window utilisation
piles/<id>/digest.md          what the main model reads
piles/<id>/outputs.json       which outputs were built from which content (staleness)
```
Outputs go to `<pile>/pile-digest/`. If the pile folder is read-only, they go to the state folder instead.
