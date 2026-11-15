# Profile: study-notes

Structured study notes, one Markdown file per section of the pile, written by the main model from the digest and the verified pages.

## Output layout (in `<output folder>/study-notes/`)
- `00-overview.md`: what the pile covers, how it's organised (sections and the files they come from), prerequisites, and a suggested study order.
- `NN-<section-slug>.md`: one file per section, numbered in teaching order. A section usually follows the outline (bookmarks or title pages) or a coherent topic block in the digest.
- `notation.md`: one table of symbols with their meaning, units and where they're introduced (`D<n>:<page>`). Note any clashes, such as the same symbol used for two things in different files.
- `glossary.md`: key terms with one-line definitions.
- `errata.md`: from `pd errata <pile>`. Errors in the source (with the corrected statement and how you verified it), kept separate from transcription corrections.

## How to write each section file
1. **Start with a source line:** `Sources: D2:1–14, D5:3–9`.
2. **State the learning goals** in 3–6 bullets.
3. **Cover the core content:** definitions, results and derivations, in logical order. Rewrite for clarity. Don't paste transcriptions. Keep every step a student needs, and mark steps you added: "(step added)".
4. **Use maths** in LaTeX (`$...$`, `$$...$$`) with consistent notation across files. Follow `notation.md`.
5. **Worked examples:** keep the ones in the source and check each one.
6. **Pitfalls:** common mistakes. Include source errors that were found, with a pointer to `errata.md`.
7. **Summary:** a short recap and the key formulas.

## Quality bar
- **Everything is traceable.** Each non-trivial claim should be traceable to a page reference, or marked as added by you.
- **Never include unverified flagged content as fact.** If a `source-suspect` or `illegible` item isn't resolved, say so in the text.
- **Make a pile without maths read naturally.** For non-technical material (reports, manuals), drop the derivation parts and use "Key points / Details / Open questions" instead.
- **Keep the source's language,** unless the user asks for a translation.

When you're done, run `pd outputs <pile> --mark study-notes`.
