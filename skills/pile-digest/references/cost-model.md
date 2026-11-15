# Cost model

## Where tokens go
| Who | Reads | Typical cost |
|---|---|---|
| Local tools | everything | 0 |
| Cheap model | page images that need vision; text pages for summaries | image ≈ (w·h)/750 tokens, about 1,200–1,600 for a full page; plus about 600 output per page |
| Strong model | escalated pages (about 3%), spot checks (about 5%), section digests | small share of the total |
| Main model | `digest.md` (about 40 tokens per page) plus the pages it opens | a few % of "reading everything" |

Images are rendered so the long side is at most 1568 px and the area at most about 1.15 MP. The API scales anything larger down, so a bigger render costs nothing extra in quality but more on disk and in the request. Tall pages, and escalations, are split into overlapping tiles so small writing stays legible.

## Estimating before work
`pd estimate` sums the following:
- image tokens per vision page;
- a text-layer hint for hybrid pages;
- per-page and per-call overhead;
- expected output per page class;
- an escalation reserve (10% of vision pages re-read as tiles, 3% sent to the strong model);
- spot checks;
- section digests.

Before the pilot, the output-per-page figures are prior guesses. After the pilot, they're replaced by measured averages.

## The pilot and the usage window
Every CLI call reports how full the account's 5-hour window is. The pilot records this before and after, and extrapolates what share of the window the rest of the run would take. The figure is rough, because other use of the account at the same time also moves the meter. The run then stops by itself once the window passes `max_window_utilization` (default 0.92).

## Why fresh calls matter
A long-lived subagent re-reads its whole growing context every turn. Fifteen pages in one subagent conversation means about 15 turns, each re-reading all the earlier images, so cache reads grow quadratically. A fresh single-turn call per batch reads each image exactly once. Its only repeated part is a short system prompt, which is served from cache.

## Budgets
`pd run <pile> --budget N` (or `budget_tokens` in the config) stops before a call would push the run past N tokens. The count is input + cache writes + output; cache reads are cheap and aren't counted. `pd ledger <pile>` shows the real numbers per stage and model, and `costUSD` is the CLI's API-price equivalent.
