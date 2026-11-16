# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [semantic versioning](https://semver.org/).

## [2.0.0] - 2026-11-16
The skill now ends once Claude understands the pile. What happens next (questions, notes, anything else) is the user's choice. This release also fixes the problems found by the first field test on a real pile: 288 pages, handwritten notebooks and lecture slides.

### Removed
- Output profiles, along with `pd profiles`, `pd outputs`, `pd postbuild` and the `profiles`, `output_location` and `output_dirname` settings.

### Added
- **A mid-run approval cap.** A run stops and asks again once spend since `pd approve` would pass the approved amount × `run_overrun_factor` (default 1.25).
  - **Approving more:** `pd approve --add <tokens>`.
  - **Checking a rerun's size:** `pd rerun --dry-run` shows it before anything is spent, and `rerun` now needs an approval.
- **"Related pages across files" in the digest:** pages in different files that share many numbers and symbols, which is how contradictions between files are found.
- **A "Confirmed source errors" section in the digest,** with verdicts shown per page.
- **A `typo` flag kind,** kept apart from `source-suspect`.
- **Redaction of private names, ID numbers and contact details** in transcriptions.

### Changed
- **The escalation ladder** is now bulk → stronger model → main model. Cheap high-resolution tiles are opt-in (`escalation_ladder`), since they rarely fixed handwriting.
  - The stronger model sees the full page and the earlier transcription, and corrects it.
  - A re-read that shares almost nothing with the earlier reading, or with the page's text layer, is rejected. The earlier reading is kept and the page goes to the main model.
- **Prompt caching is off for worker calls,** since every call has new images. Measured: the same image call costs about half.
- **The pilot** samples 2 pages from every document with at least 4 vision pages, and the estimate uses an escalation rate per document. The section-summary cost is measured, not guessed.
- **Spot checks** sample every reading level separately, report a mismatch rate per level, and keep their history when pages are fixed. The advice names the weak level and never suggests tiles for handwriting.
- **The digest:**
  - it is rebuilt for free after `fix`, `accept`, `verify` and `reclass`, and `pd digest` makes no model calls unless `--sections` is given;
  - large piles get an overview plus one page file per document;
  - summaries are cut at sentence or word boundaries;
  - the flag index is sorted by severity and capped per file;
  - flags from earlier readings and duplicate flag/marker pairs are merged;
  - the notation list shows symbols, not LaTeX macro names, and topics skip cover pages and generic words.
- **The worker prompts** ask for shorter summaries and figure descriptions, technical topics only, and a named rule for every source-suspect claim, never on a doubtful reading.
- **"Spent"** now means the same everywhere (input + cache writes + output), with cache reads shown separately.

### Fixed
- **Build-up slides,** each adding to the one before, were skipped as repeated titles, because digits were ignored when comparing pages.
- **Duplicate detection changed between runs** when the original page had been reclassified by the summariser.
- **Pages whose text differs only slightly** could be marked as near-duplicates.
- **A batch answered with "the image did not arrive"** was escalated. It is now retried at the same level, one page per call.
- **Verdicts and spot-check records** were lost when a page was fixed or re-read.
- **Section summaries** were cut mid-word, and a section without a summary had its pages listed under the previous section.
- **Review work made the next run pay** for the same section summaries again.
- **The pilot listed unfinished pages** as `None pNone`.

## [1.0.0] - 2026-11-15
### Added
- `/pile-digest` skill with a resumable pipeline: scan → prepare (inventory, convert, triage) → estimate → pilot → approve → run (summaries, bulk transcription, escalation, spot checks, digest) → review and verification → output profiles.
- A pile registry that persists across sessions, watch folders, incremental re-scans, move and rename detection, and staleness tracking for outputs.
- Converters for PDF, images, Office/ODF, EPUB, HTML, RTF, notebooks, text and code, email and mbox, archives (zip, tar, gz, bz2, xz, 7z, rar), DjVu, PS/EPS and XPS, each with a graceful fallback.
- Fresh single-turn `claude -p` workers with inline images and a usage ledger, plus budget, usage-window and disk guards.
- Built-in `study-notes` and `summary-report` profiles, and user profiles with `post_build` commands.

[2.0.0]: https://github.com/devesh8090/pile-digest/releases/tag/v2.0.0
[1.0.0]: https://github.com/devesh8090/pile-digest/releases/tag/v1.0.0
