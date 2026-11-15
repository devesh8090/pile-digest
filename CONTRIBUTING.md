# Contributing

Thanks for helping. Issues and pull requests are welcome.

## Ground rules
- **The core stays stdlib-only Python ≥ 3.9 plus poppler-utils.** Other tools (qpdf, mutool, Ghostscript, LibreOffice, ImageMagick, 7-Zip, Pillow…) are optional and must be detected at runtime. When one is missing, quality may drop but nothing may fail.
- **One bad file must never stop a pile.** Converters catch their own errors and report a reason.
- **No model call without approval.** Anything that spends tokens goes through `engine.execute`, which applies the budget and window guards and writes the ledger.
- **Never call a shell.** Subprocesses take argument lists (`pdlib.util.run`).
- **Write files atomically** (`atomic_write_*`, `write_json`), so a crash never leaves corrupt state behind.
- **Keep the repo general.** No personal paths or names, and no examples tied to a single subject.

## Development
```
python3 -m unittest discover -s tests -v       # uses tests/fake_claude.py, so no network and no cost
uvx ruff check .                               # or: pip install ruff && ruff check .
python3 skills/pile-digest/scripts/pd.py doctor
```
Test fixtures are generated at test time with the standard library (see `tests/fixtures.py`); please don't commit binary samples. To test a real pile without touching your normal state, set `PILE_DIGEST_HOME=/tmp/pd-state`.

## Changing prompts
The worker prompts in `skills/pile-digest/prompts/` define the output format that `pdlib/parse.py` reads. If you change the format, update the parser, `tests/fake_claude.py` and the parser tests together. Note in the pull request how you checked the change against real model output.

## Pull requests
- Keep each one focused, add tests, and update `CHANGELOG.md` and any affected `references/*.md`.
- CI runs the tests on Linux and macOS with Python 3.9 and 3.13, a run with every optional tool hidden, and ruff.
