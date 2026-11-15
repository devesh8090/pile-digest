# Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `NOT READY: claude CLI` | `claude` is not on PATH for the script | Install Claude Code, or set `PILE_DIGEST_CLAUDE_BIN=/path/to/claude`. |
| A run stops with "not logged in" | The CLI has no valid login | Run `claude` once interactively and log in. `pd doctor --live` checks it. |
| A run stops with "window … full" | The 5-hour window is nearly used up | Wait for the reset time it prints, then run `/pile-digest` again. To let runs go closer to the limit, raise `max_window_utilization`. |
| `is busy: another pile-digest run` | A run is in progress, or one crashed on another machine | Wait. If you're sure nothing is running, delete the `run.lock` file it names. |
| A file shows as `needs-password` | Encrypted PDF | Run `pd unlock <pile> "<file>"` in your own terminal, then `pd prepare <pile>`. |
| `legacy Office file; install LibreOffice` | `.doc`/`.ppt`/`.xls` and no LibreOffice | Install LibreOffice, or save the file as `.docx`/`.pptx`/`.pdf`. |
| HEIC or other odd images unreadable | No converter | `pip install pillow pillow-heif`, or install ImageMagick or libheif. |
| Many hybrid pages on plain slides | Decorative vector backgrounds look like drawings | `pd config set math_pages_use_vision false --pile <pile>`, or accept it: the pilot shows the cost. |
| A text page reads as nonsense | Broken text layer (font encoding) | The summariser reclassifies garbled pages to vision on its own. To force it: `pd reclass <pile> D<n>:<page> --cls vision`, then `pd run <pile>`. |
| High spot-check mismatch rate | Low-resolution scans or dense handwriting | `pd rerun <pile> --level tiles` (still cheap) or `--level strong`. |
| `damaged PDF that no installed tool could repair` | Truncated or corrupt file | Get a fresh copy. Installing qpdf, mutool or Ghostscript adds repair attempts. |
| Pile folder moved | — | `pd scan` finds it again through `.pile-digest-id` in watch folders, the current folder and the old parent folder. Otherwise run `pd add <new path>`: it recognises the marker and keeps all results. |
