# Settings

Settings live in a JSON file: `~/.config/pile-digest/config.json` (or `$XDG_CONFIG_HOME`, or `$PILE_DIGEST_CONFIG`). Each pile can override them in its own state folder.

```
pd config show [--pile <pile>]
pd config set <key> <json-value> [--pile <pile>]
```

| Key | Default | Meaning |
|---|---|---|
| `watch_roots` | `[]` | Folders whose subfolders become piles automatically (`pd watch add`). |
| `models` | `{"bulk": "haiku", "escalate": "sonnet", "check": "sonnet"}` | The model for each rung of the ladder (CLI aliases or full names). |
| `batch_pages` | 5 | Vision pages per cheap call (1–20). Lower it for dense handwriting. |
| `text_batch_pages` | 20 | Text pages per summary call. |
| `concurrency` | 3 | Parallel calls (1–8). |
| `render_long_side` | 1568 | Render size in pixels. |
| `tile_rows` | 2 | Tiles per page on escalation. |
| `summarize_text_pages` | true | `false` makes text-ok pages completely free: the summary becomes their first line. |
| `math_pages_use_vision` | true | Read formula-heavy typed pages from the image, with the text layer as a hint. |
| `budget_tokens` | null | A cap per run. |
| `max_window_utilization` | 0.92 | Stop before the next call once the 5-hour window is this full. |
| `pilot_pages` | 12 | Pilot sample size. |
| `spotcheck_fraction` / `spotcheck_min` | 0.05 / 3 | Spot-check sample. |
| `section_digest_min_pages` | 120 | Piles at least this big get section digests from the strong model. |
| `section_chunk_pages` | 25 | Section size when a document has no outline. |
| `output_location` | `"pile"` | `"pile"` (inside the pile folder) or `"state"`. |
| `output_dirname` | `"pile-digest"` | The output folder's name. It's always excluded from scans. |
| `ignore` | `[]` | Extra glob patterns to skip. A `.pile-digest-ignore` file in a pile works the same way. |
| `keep_renders` | false | Keep page renders for debugging. |
| `archive_max_total_mb`, `archive_max_ratio`, `archive_max_members`, `archive_max_depth` | 4096, 250, 20000, 3 | Archive safety limits. |
| `text_page_chars`, `max_text_pages_per_file` | 3500, 200 | How text files are cut into pages, and the cap per file. |

Environment variables:
- `PILE_DIGEST_HOME` moves all state, which is useful for tests.
- `PILE_DIGEST_<TOOL>_BIN` overrides a tool's path, e.g. `PILE_DIGEST_CLAUDE_BIN`.
