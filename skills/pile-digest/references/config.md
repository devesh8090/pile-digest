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
| `escalation_ladder` | `["strong"]` | The levels a flagged page climbs after the bulk reading (the main model comes last). `["tiles", "strong"]` adds a cheap high-resolution re-read first. That helps with small print on typed pages, but rarely with handwriting. |
| `tile_rows` | 2 | Tiles per page at the `tiles` level. |
| `worker_prompt_cache` | false | Prompt caching for worker calls. Off, because each call's images are new and cache writes cost more than plain input. |
| `run_overrun_factor` | 1.25 | A run stops and asks again once spend since `pd approve` passes the approved amount × this factor. |
| `summarize_text_pages` | true | `false` makes text-ok pages completely free: the summary becomes their first line. |
| `math_pages_use_vision` | true | Read formula-heavy typed pages from the image, with the text layer as a hint. |
| `budget_tokens` | null | A cap per run. |
| `max_window_utilization` | 0.92 | Stop before the next call once the 5-hour window is this full. |
| `pilot_pages` | 12 | Minimum pilot sample. It grows to 2 per vision-heavy document, up to 30. |
| `spotcheck_fraction` / `spotcheck_min` | 0.05 / 3 | Spot-check sample, per reading level. |
| `section_digest_min_pages` | 120 | Piles at least this big get section digests from the strong model. |
| `section_chunk_pages` | 25 | Section size when a document has no outline. |
| `ignore` | `[]` | Extra glob patterns to skip. A `.pile-digest-ignore` file in a pile works the same way. |
| `keep_renders` | false | Keep page renders for debugging. |
| `archive_max_total_mb`, `archive_max_ratio`, `archive_max_members`, `archive_max_depth` | 4096, 250, 20000, 3 | Archive safety limits. |
| `text_page_chars`, `max_text_pages_per_file` | 3500, 200 | How text files are cut into pages, and the cap per file. |

Environment variables:
- `PILE_DIGEST_HOME` moves all state, which is useful for tests.
- `PILE_DIGEST_<TOOL>_BIN` overrides a tool's path, e.g. `PILE_DIGEST_CLAUDE_BIN`.
