# Security

## What pile-digest does with your data
- Every page that needs reading is sent to Anthropic's models through your own `claude` CLI login, under the same terms as your other Claude Code use. Pages the local triage classifies as blank, duplicate or text-only are never sent as images.
- State lives in `~/.local/state/pile-digest/` (or `$XDG_STATE_HOME`). It includes extracted text, transcriptions, unpacked archives, and, **for PDFs you unlock, a decrypted copy**. `pd.py forget <pile> --yes` deletes all of it. Your original files are never modified. The only thing written into a pile folder is a small `.pile-digest-id` marker.
- Passwords are typed in your terminal (`pd.py unlock`), passed to qpdf through a temporary file readable only by you, and never stored or shown to Claude.

## Hardening in place
- **Archives:** extraction refuses path traversal, absolute paths, symlinks and device files, and enforces limits on size, compression ratio, member count and nesting depth.
- **Subprocesses** take argument lists only; there's no shell anywhere.
- **Worker calls** run with no tools, no MCP servers, no settings or CLAUDE.md, and no session persistence. Document content is framed as data, and instructions embedded in documents are ignored.
- **Post-build commands** come only from your own config file, as argument lists, and Claude asks before running them.

## Reporting a vulnerability
Please don't open a public issue. Use GitHub's private vulnerability reporting ("Report a vulnerability" on the repository's Security tab). Include steps to reproduce, and we'll respond as soon as we can.
