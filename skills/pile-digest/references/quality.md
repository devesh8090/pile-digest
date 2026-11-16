# Quality checklist for the main model

A wrong transcription of a formula or number is worse than a gap. These are the safeguards that already run:
- **Gaps are marked, not guessed.** The cheap model is told never to complete, derive or correct anything, and to mark what it can't read with `[[ILLEGIBLE]]` / `[[UNSURE]]`.
- **Hard pages go up the ladder.** Flagged pages are re-read by the stronger model, which also sees the earlier transcription. A re-read that switches to different content is rejected and goes to you.
- **Spot checks measure the error rate per level.** A random sample of each reading level is audited against the page images, so error rates are measured rather than assumed.
- **Cross-file hints.** The digest lists pages in different files that share many numbers and symbols. They are probably the same worked example, and a disagreement between them is a strong error signal.

What's left is for you to do.

## Before you write anything
1. **Read the digest** (`digest.md`). On large piles it's an overview; read the `digest/D<n>.md` files you need. Note the file order, the sections, the notation and the related pages.
2. **Clear the review queue** (`pd review <pile>`). For each page:
   - **Numbers first.** If the doubt is about numbers in a worked example (a table, a calculation, a matrix), recompute the example with a short script. That usually settles an ambiguous digit without an image, and it catches errors the flags missed.
   - **Then the image.** Open it when the arithmetic can't settle the question: `pd page <pile> <ref> --image`, adding `--tiles` for small handwriting.
   - **Suggested corrections** come from a cheaper model and can themselves be wrong. Check one against the image before `pd fix --suggested`.
   - **Decide:** fix the page (`pd fix`) or accept it (`pd accept`).
3. **Check the spot-check rates per level** (`pd status <pile>`). If a level has more than about 15% meaning-changing errors, tell the user and pass on the advice printed (`pd rerun <pile> --level strong --dry-run` shows the cost first). A large pile at a 20% error rate isn't a sound basis for understanding it.

## Verifying content
For each `source-suspect` claim, and each result you'll rely on when you report your understanding:
- **Misread first?** Claims marked "possibly a misread" in `pd review` sit on pages with illegible or unsure parts. In practice about half of all source-suspect flags were misreadings, so check the reading before you blame the source.
- **Derivations:** redo the key steps. Check signs, factors of 2 and π, indices and boundary terms.
- **Units and dimensions:** every equation must be dimensionally consistent.
- **Limiting and special cases:** do they reduce to known results?
- **Constants and data:** compare with other pages in the pile, and with standard values only when you're sure of them. Record which one you used.
- **Internal consistency:** the same quantity defined differently in two files? Notation reused for something else?
- **Tools:** use `sympy`, if installed, for algebra or calculus checks you can't do reliably by hand.

Record every verdict with `pd verify <pile> <ref> --verdict source-error|correct|unclear --note "<what and why>"`. Verdicts survive later fixes and appear in the digest, and `pd errata <pile>` lists them.

## Reporting your understanding
- Cite page references (`D<n>:<page>`) for non-trivial claims.
- Say what is still unverified, rather than leaving it out silently.
- Keep source errors and transcription errors apart. They mean different things to the reader.
