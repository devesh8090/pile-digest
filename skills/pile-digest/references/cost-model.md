# Cost model

## Where tokens go
| Who | Reads | Typical cost |
|---|---|---|
| Local tools | everything | 0 |
| Cheap model | page images that need vision; text pages for summaries | image ≈ (w·h)/750 tokens, about 1,200–1,600 for a full page; plus about 600 output per page |
| Strong model | escalated pages (from under 5% of typed scans to 30% of handwriting), spot checks, section summaries | up to most of the cost on handwriting-heavy piles |
| Main model | `digest.md` (about 40 tokens per page) plus the pages it opens | a few % of "reading everything" |

Images are rendered so the long side is at most 1568 px and the area at most about 1.15 MP. The API scales anything larger down, so a bigger render adds no quality, only disk space and request size. Tall pages are split into overlapping tiles so small writing stays legible.

**Measured on a real pile:** 288 pages, two handwritten notebooks and lecture slides; this was version 1.0, with the tiles rung and prompt caching on.
- **Cost:** about 5.7k tokens per page overall. The stronger model was 82% of the cost, and handwriting caused about 90% of the escalations.
- **Version 2.0 changes aimed at this:** the tiles rung is gone, and each document gets its own escalation rate from the pilot.

## Estimating before work
`pd estimate` sums the following:
- image tokens per vision page;
- a text-layer hint for hybrid pages;
- per-page and per-call overhead;
- expected output per page class;
- an escalation reserve, one rate per document as measured by the pilot (12% before it), plus pages already waiting to escalate;
- spot checks for each level;
- section summaries, at the measured cost per call or about 3.3k each before any are measured.

Before the pilot, the output-per-page figures are prior guesses. After the pilot, they're replaced by measured averages. The pilot takes 2 pages from every document with at least 4 vision pages, so a handwritten notebook can't hide behind clean slides.

## The pilot and the usage window
Every CLI call reports how full the account's 5-hour window is. The pilot records this before and after, and extrapolates what share of the window the rest of the run would take. The figure is rough, because other use of the account at the same time also moves the meter. The run then stops by itself once the window passes `max_window_utilization` (default 0.92).

## Why fresh calls matter
A long-lived subagent re-reads its whole growing context every turn. Fifteen pages in one subagent conversation means about 15 turns, each re-reading all the earlier images, so cache reads grow quadratically. A fresh single-turn call per batch reads each image exactly once.

**Prompt caching** is off for worker calls (`DISABLE_PROMPT_CACHING=1`). Each call's images are new, so caching them only adds the cache-write surcharge. Measured: the same one-image call cost $0.00045 with caching and $0.00023 without.

## Approval and budgets
- **What `pd approve` records:** the estimate the user agreed to.
- **During any run, rerun or section-summary refresh:** spend since the approval is checked before each call. If it would pass the approved amount × `run_overrun_factor` (default 1.25), the run stops and Claude asks the user again: `pd approve <pile>` for a fresh estimate, or `pd approve <pile> --add <tokens>`.
- **`pd rerun ... --dry-run`:** shows a rerun's size before it spends anything.
- **Budgets:** `pd run <pile> --budget N` (or `budget_tokens` in the config) also stops before a call would push the run past N tokens.

All "spent" figures are input + cache writes + output. Cache reads are cheap and are shown separately. `pd ledger <pile>` shows the real numbers per stage and model, and `costUSD` is the CLI's API-price equivalent.
