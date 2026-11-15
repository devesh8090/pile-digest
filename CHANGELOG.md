# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [semantic versioning](https://semver.org/).

## [1.0.0] - 2026-11-15
### Added
- `/pile-digest` skill with a resumable pipeline: scan → prepare (inventory, convert, triage) → estimate → pilot → approve → run (summaries, bulk transcription, escalation, spot checks, digest) → review and verification → output profiles.
- A pile registry that persists across sessions, watch folders, incremental re-scans, move and rename detection, and staleness tracking for outputs.
- Converters for PDF, images, Office/ODF, EPUB, HTML, RTF, notebooks, text and code, email and mbox, archives (zip, tar, gz, bz2, xz, 7z, rar), DjVu, PS/EPS and XPS, each with a graceful fallback.
- Fresh single-turn `claude -p` workers with inline images and a usage ledger, plus budget, usage-window and disk guards.
- Built-in `study-notes` and `summary-report` profiles, and user profiles with `post_build` commands.

[1.0.0]: https://github.com/devesh8090/pile-digest/releases/tag/v1.0.0
