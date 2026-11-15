# pile-digest

[![CI](https://github.com/devesh8090/pile-digest/actions/workflows/ci.yml/badge.svg)](https://github.com/devesh8090/pile-digest/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue)

**A Claude Code skill that lets Claude understand a big, messy pile of documents without using up your usage window.**

Lecture slides, scanned or handwritten notes, book chapters, reports, manuals, Office files, photos of whiteboards, zip files of all of the above. Point pile-digest at the folder and Claude can study it, check it and write from it: structured study notes, a summary report, or your own output format. Free local tools do everything they can, a cheap model does the bulk reading, and the expensive model spends its tokens only where they pay off.

```
/pile-digest ~/Downloads/semester-dump
```

---

## Why

Reading hundreds of pages with the most capable model is slow and expensive, and most of that spending is waste:

| Waste | What pile-digest does instead |
|---|---|
| Blank pages, repeated title slides and duplicate pages read like any other page | Detected locally from low-resolution renders and text hashes, and skipped for free |
| Typed pages sent as images although their text layer is fine | The text layer is used directly; at most, a cheap model writes a one-line summary |
| Long-lived subagents re-read their growing context every turn, so cache reads grow quadratically | Every batch is a **fresh single-turn** `claude -p` call: no tools, a short cached system prompt, images inlined once |
| The expensive model reads full transcriptions just to find its way around | It reads a **digest** of about 40 tokens per page (outline, page summaries, flags) and opens full pages only where needed |
| Hard pages read at the same level as easy ones | A **ladder**: high-resolution tiles with the cheap model, then a stronger model, then the main model, for flagged pages only |

## Careful, not just cheap

A wrong transcription of a formula is worse than none, so the pipeline is built to surface doubt rather than hide it:

- **No guessing.** The bulk model must never complete, derive or "fix" anything. Unreadable parts become `[[ILLEGIBLE: best guess?]]`, doubtful ones `[[UNSURE: …]]`, and apparent errors in the source `[[SOURCE-SUSPECT: …]]`.
- **Measured accuracy.** A random sample of cheap transcriptions is audited against the page images by a stronger model. A mismatch sends the page to the review queue with a suggested correction. If the error rate is high, you're told, and you can re-read at a higher level.
- **Verification is part of the job.** Source documents contain errors too: wrong constants, garbled formulas, contradictions between files. The main model checks every flagged claim and records a verdict, and the verdicts become an `errata.md` that keeps source errors separate from transcription errors.

## Cost before work

Nothing is spent without your go-ahead:

1. **`prepare`** (free): inventories, converts and triages the pile, then shows a token estimate. This covers pages skipped, pages free, pages that need vision, tokens per model, and how much the main model will read compared with reading everything.
2. **`pilot`** (small): the cheap model reads a sample, chosen to include the hardest pages, and the estimate is re-calibrated from the real usage. The CLI reports how full your 5-hour window is, so the pilot can also project roughly what share of the window the full run needs.
3. **`approve`, then `run`:** the run stops by itself before your window fills (configurable) or a token budget is reached. Everything finished is saved per page, and the next `/pile-digest` resumes where it stopped.

**Rough cost per page:** about 1.2–1.6k input and about 0.6k output tokens on the cheap model for a page that needs vision. A text-layer page costs a few hundred cheap-model tokens for its summary, or nothing. The main model reads about 40 tokens per page of digest plus whatever it opens. `pd.py ledger <pile>` shows the real numbers for each stage and model.

## It keeps track of your piles

- **Registry:** every pile you add is remembered, and `/pile-digest` with no argument reports what changed since last time.
- **Watch folders:** every new subfolder becomes a pile automatically (`pd.py watch add ~/Documents/piles`).
- **Incremental runs:** only new or changed files are processed. Renamed or moved files keep their results (documents are keyed by content hash), and moved pile folders are found again.
- **Stale outputs:** outputs record what they were built from, so you're told which sections are out of date after new files arrive.

## Anything goes in

| Input | How it's handled (best installed tool first, then a fallback) |
|---|---|
| PDF | poppler (text layer, rendering, image detection) · qpdf/mutool/Ghostscript repair · bookmarks become the outline · encrypted files are unlocked by you, privately |
| Images (PNG, JPEG, WebP, GIF, TIFF incl. multi-page, BMP, HEIC, AVIF, JP2, SVG…) | Pillow or ImageMagick (orientation, scaling, tiling) · sent as they are if no library is present |
| Word, PowerPoint, Excel, ODF | LibreOffice → PDF (layout kept) · or pure-Python XML text plus embedded images |
| EPUB, HTML, RTF, Markdown, LaTeX, code, CSV, Jupyter notebooks | standard-library parsers; embedded images go to vision |
| Email (.eml, .mbox) | body as text, attachments processed like any other file |
| Archives (zip, tar, gz, bz2, xz, 7z, rar) | unpacked recursively with zip-bomb, path-traversal and depth limits |
| DjVu, PostScript/EPS, XPS | converted with ddjvu, ps2pdf or mutool when installed |
| Audio, video, unknown binaries, password-protected archives | listed with the reason and the fix, never dropped silently |

## Install

**Requirements:** [Claude Code](https://claude.com/claude-code), Python ≥ 3.9 and `poppler-utils`. Everything else is optional.

| Optional tool | Adds |
|---|---|
| qpdf, mutool, Ghostscript | PDF repair, bookmarks, spotting drawings on typed pages, PostScript |
| LibreOffice | Office files with full layout; legacy `.doc`/`.ppt`/`.xls` |
| Pillow or ImageMagick | odd image formats, tiling of image files |
| 7-Zip | 7z and rar archives |
| sympy | algebra checks during verification |

**As a personal skill** (gives `/pile-digest`):
```sh
git clone https://github.com/devesh8090/pile-digest.git
cd pile-digest && ./install.sh          # symlinks skills/pile-digest into ~/.claude/skills
```

**As a plugin:**
```
/plugin marketplace add devesh8090/pile-digest
/plugin install pile-digest@pile-digest
```
Plugin skills are namespaced, so the command becomes `/pile-digest:pile-digest`; Claude also triggers the skill on its own when you ask it to digest a folder of documents.

Then check your setup:
```sh
python3 ~/.claude/skills/pile-digest/scripts/pd.py doctor          # add --live to test the CLI login with one tiny call
```

## Use

In Claude Code:
```
/pile-digest                       # what's new or changed across your piles
/pile-digest ~/path/to/folder      # start with a specific folder
```
Claude walks you through it: summary and estimate → pilot → your approval → run (in the background for big piles) → review and verification → output. It asks before spending anything and before running any post-build command.

Everything is also available on the command line (`pd.py --help`), for scripting or for inspecting state yourself.

## Output profiles

- **`study-notes`:** an overview, one file per section (learning goals, core content, checked examples, pitfalls, summary), a notation table, a glossary and errata. Everything carries page references.
- **`summary-report`:** a single dense report: contents map, themes, key facts, problems found, open questions.

Add your own profile in `~/.config/pile-digest/config.json`. A profile can have a `post_build` command, for example your own Markdown→PDF toolchain, so your templates stay in your setup and the core stays general. See [`references/profiles.md`](skills/pile-digest/references/profiles.md).

## Privacy

The pages that need reading are sent through your own Claude Code login. Pages the local triage skips are never sent. State lives in `~/.local/state/pile-digest/`, and `pd.py forget <pile> --yes` removes it. Your files are never modified. See [SECURITY.md](SECURITY.md).

## Documentation

- [`SKILL.md`](skills/pile-digest/SKILL.md): the workflow Claude follows
- [`references/pipeline.md`](skills/pile-digest/references/pipeline.md): stages and state layout
- [`references/cost-model.md`](skills/pile-digest/references/cost-model.md): how estimates work
- [`references/quality.md`](skills/pile-digest/references/quality.md): the verification checklist
- [`references/config.md`](skills/pile-digest/references/config.md): every setting
- [`references/troubleshooting.md`](skills/pile-digest/references/troubleshooting.md): problems and fixes

## Development

```sh
python3 -m unittest discover -s tests -v    # a fake `claude` binary stands in for the CLI: no network, no cost
```
See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
