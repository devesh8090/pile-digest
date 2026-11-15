# Profile: summary-report

A single readable report about the whole pile, for someone who needs to know what's in it without studying it.

## Output (in `<output folder>/summary-report/`)
`report.md`, containing:
1. **At a glance:** 5–10 bullets on what the pile is, its scope and its overall quality.
2. **Contents map:** a table of file, what it covers, pages, and notes (e.g. "scanned, partly illegible", "duplicate of D3").
3. **Main themes:** a few paragraphs per theme, with page references (`D<n>:<page>`).
4. **Key facts, results or decisions,** each with a reference.
5. **Problems found:** contradictions between files, source errors (from `pd errata <pile>`), gaps, unreadable files and what would fix them.
6. **Open questions** worth following up.

## Rules
- Write from the digest. Open full pages only to confirm something you state as a key fact.
- Mark anything unverified or flagged as such.
- Keep it short and dense: aim for 2–6 pages, whatever the size of the pile.

When you're done, run `pd outputs <pile> --mark summary-report`.
