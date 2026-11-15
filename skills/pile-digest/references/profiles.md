# Output profiles

A profile is a Markdown file of instructions that the main model follows when it writes outputs from a digested pile. There are two built-in profiles: `study-notes` and `summary-report`. Run `pd profiles` to list everything available.

## Adding your own
Add an entry under `profiles` in your config file (`pd doctor` prints its path):

```json
{
  "profiles": {
    "my-notes": {
      "instructions": "~/notes-style/pile-profile.md",
      "post_build": ["make", "-C", "{out}", "pdf"]
    },
    "study-notes": {
      "post_build": ["pandoc-build", "{out}"]
    }
  }
}
```

- **`instructions`:** the path to your own instructions file. Without it, the built-in file with the same name is used, which lets you add a `post_build` to `study-notes`.
- **`post_build`:** optional. It's an argument list (never a shell string) that runs in `<output folder>/<profile>/`. `{out}` expands to that folder and `{pile}` to the pile folder. Claude asks before running it.

A good instructions file states:
- the file layout;
- what each file must contain;
- the citation style (`D<n>:<page>` references);
- how to handle unverified or flagged content.

Use the built-in profiles as templates.

## Staleness
`pd outputs <pile> --mark <profile>` records a fingerprint of every document's results. If files are added or pages re-read later, `pd outputs <pile>` and `pd status <pile>` list the documents whose outputs are stale, so only the affected sections need rewriting.
