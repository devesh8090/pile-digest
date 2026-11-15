# Quality checklist for the main model

A wrong transcription of a formula or number is worse than a gap. These are the safeguards that already run:
- **Gaps are marked, not guessed.** The cheap model is told never to complete, derive or correct anything, and to mark what it can't read with `[[ILLEGIBLE]]` / `[[UNSURE]]`.
- **Hard pages go up the ladder.** Flagged pages are re-read at higher resolution, then by the stronger model.
- **Spot checks measure the error rate.** A random sample is audited against the page images, so the cheap model's error rate is measured rather than assumed.

What's left is for you to do.

## Before you write anything
1. **Read the whole digest** (`digest.md`). Note the file order, the sections and the notation.
2. **Clear the review queue** (`pd review <pile>`). For each page:
   - Look at the image: `pd page <pile> <ref> --image`, adding `--tiles` for small handwriting.
   - Compare it with the stored transcription, and with the suggested correction if there is one.
   - Fix the page (`pd fix`) or accept it (`pd accept`).
3. **Check the spot-check rate** (`pd status <pile>`). If more than about 15% of sampled pages had meaning-changing errors, tell the user and suggest `pd rerun <pile> --level tiles` or `--level strong` before you rely on the pages. A large pile at a 20% error rate isn't a sound base for notes.

## Verifying content
For each `source-suspect` item and each result you'll state in the outputs:
- **Derivations:** redo the key steps. Check signs, factors of 2 and π, indices and boundary terms.
- **Units and dimensions:** every equation must be dimensionally consistent.
- **Limiting and special cases:** do they reduce to known results?
- **Constants and data:** compare with other pages in the pile, and with standard values only when you're sure of them. Record which one you used.
- **Internal consistency:** the same quantity defined differently in two files? Notation reused for something else?
- **Tools:** use `sympy`, if installed, for algebra or calculus checks you can't do reliably by hand.

Record every verdict with `pd verify <pile> <ref> --verdict source-error|correct|unclear --note "<what and why>"`. Source errors go into `errata.md` with the corrected statement and how you checked it.

## Writing
- Cite page references (`D<n>:<page>`) for non-trivial claims.
- Mark anything still unverified in the text itself, rather than leaving it out silently.
- Keep source errors and transcription errors apart. They mean different things to the reader.
