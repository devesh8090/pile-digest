# Pipeline and state

## Stages
| Stage | Command | Cost | What happens |
|---|---|---|---|
| scan | `pd scan` | free | Finds new piles (watch folders, the current folder, a given path) and changed files (size+mtime). Finds moved piles again through a `.pile-digest-id` marker. |
| inventory | `pd prepare` | free | Walks the pile safely: skips junk and VCS folders, never follows folder symlinks. Detects types from magic bytes. Unpacks zip/tar/gz/bz2/xz/7z/rar and email attachments within limits: size, compression ratio, member count, nesting depth, no path traversal. Identical files are kept once. |
| convert | `pd prepare` | free | PDFs via poppler, with qpdf/mutool/Ghostscript fallbacks and repair. Office files via LibreOffice → PDF, or pure-Python XML text plus embedded images. EPUB, HTML, RTF, notebooks, email and text through stdlib parsers. Images via Pillow or ImageMagick, or sent as they are. DjVu/PS/XPS go through their converters when installed. |
| triage | `pd prepare` | free | Low-resolution renders give ink, background and a perceptual hash per page. Pages are classed as blank, duplicate (exact or near-identical, confirmed by pixels), repeated-title, title, text-ok, hybrid or vision (see below). Pages count as duplicates only if their text matches too, ignoring page numbers. Build-up slides, where each page adds to the one before, are always kept. Closing slides ("Thank you", "Questions?") are free. |
| estimate | `pd estimate` | free | See `cost-model.md`. |
| pilot | `pd pilot` | small | A sample read with the cheap model: 2 pages from every document with at least 4 vision pages, the hardest-looking pages, and a random fill. It calibrates the estimate, including an escalation rate per document. |
| summaries | `pd run` | cheap | Text-ok pages get a short summary from the cheap model, about 20 pages per call. A page whose text layer the model calls garbled is reclassified as vision and read from its image in the same run. |
| bulk | `pd run` | cheap | Vision and hybrid pages, about 5 per fresh call. Hybrid pages send their text layer as a hint. |
| escalate | `pd run` | strong | Flagged pages (illegible, low confidence, cut off, many unsure spots) go to the strong model. It sees the full page plus the earlier transcription, and corrects that rather than starting from scratch. A new reading that shares almost nothing with the earlier one, or with the page's text layer, is **rejected**: the earlier reading is kept and the page goes to the main model. Pages still flagged after the strong model join the review queue. Cheap high-resolution tiles (`escalation_ladder: ["tiles", "strong"]`) are opt-in. |
| spotcheck | `pd run` | strong | Each reading level (bulk, strong, tiles) is sampled on its own: 5% of its pages, at least 3. Each sampled page is audited against its image. A mismatch adds the page to the review queue with a suggested correction. Rates are reported per level, and fixes never erase the original verdicts. |
| digest | `pd run` / `pd digest` | free (+ strong for section summaries on big piles) | Builds `digest.md`. Over about 12k tokens, it becomes an overview plus `digest/D<n>.md` per document. `pd fix`, `accept`, `verify` and `reclass` rebuild it for free. |

Classes that need vision:
- **hybrid:** a text layer plus images or drawings, or text dense with formulas;
- **vision:** no usable text layer.

## Fresh calls
Each call is a separate `claude -p` process with:
- a short custom system prompt;
- prompt caching off (`worker_prompt_cache`). Each call carries new images, so cache writes were almost never read back, and a cache write costs more than plain input;
- no tools, MCP, settings, CLAUDE.md or session file;
- one user message with the images inlined.

Nothing builds up between calls.

## Stops and resume
Before each call, the run checks:
- the approval: spend since `pd approve` may not pass the approved amount × `run_overrun_factor` (default 1.25). The run then stops with reason `approval`, and Claude must ask the user before `pd approve` (or `--add`) and a re-run;
- the budget (`--budget` or `budget_tokens`);
- the 5-hour window utilisation reported by the CLI (`max_window_utilization`);
- the free disk space.

A usage-limit, rate-limit or auth error stops the run. A reply claiming that "the image did not arrive" is retried at the same level, one page per call, and is not escalated. Results are saved per page as they arrive, so the same command resumes. A per-pile lock prevents two runs on the same pile at once.

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
piles/<id>/digest/D<n>.md     per-document page lines (large piles only)
piles/<id>/sections/          cached section summaries
```
pile-digest never writes into the pile folder, apart from the small `.pile-digest-id` marker. A top-level folder named `pile-digest/` inside a pile is never read as part of the pile, which makes it a safe place for anything derived from it.
