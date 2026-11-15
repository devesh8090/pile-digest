"""Token ledger: one line per model call, summarised per stage and model."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Optional

from .util import append_jsonl, now_iso, read_jsonl


def record(path: Path, **fields) -> None:
    fields.setdefault("ts", now_iso())
    append_jsonl(path, fields)


def total_tokens(r: dict) -> int:
    return int(r.get("input", 0)) + int(r.get("cache_create", 0)) + int(r.get("cache_read", 0)) + int(r.get("output", 0))


def summarize(path: Path, since: Optional[str] = None) -> dict:
    rows = read_jsonl(path)
    if since:
        rows = [r for r in rows if r.get("run") == since]
    by = defaultdict(lambda: defaultdict(int))
    cost = 0.0
    calls = 0
    last_rate = None
    for r in rows:
        key = f"{r.get('stage', '?')}/{r.get('model', '?')}"
        b = by[key]
        b["calls"] += 1
        for k in ("input", "cache_create", "cache_read", "output", "pages"):
            b[k] += int(r.get(k, 0) or 0)
        b["failed"] += 0 if r.get("ok") else 1
        cost += float(r.get("cost_usd", 0) or 0)
        calls += 1
        if r.get("rate"):
            last_rate = r["rate"]
    total = {k: sum(v.get(k, 0) for v in by.values()) for k in ("input", "cache_create", "cache_read", "output", "pages")}
    return {
        "by": {k: dict(v) for k, v in by.items()},
        "total": total,
        "total_tokens": sum(total[k] for k in ("input", "cache_create", "cache_read", "output")),
        "cost_usd": round(cost, 4),
        "calls": calls,
        "last_rate": last_rate,
    }


def per_page_stats(path: Path, stage_prefix: str = "") -> dict:
    """Measured averages per page class, used to calibrate estimates."""
    rows = [r for r in read_jsonl(path) if r.get("ok") and str(r.get("stage", "")).startswith(stage_prefix)]
    agg = defaultdict(lambda: {"pages": 0, "input": 0, "output": 0, "cost": 0.0, "calls": 0})
    for r in rows:
        cls = r.get("cls") or "mixed"
        a = agg[(r.get("stage"), cls)]
        a["pages"] += int(r.get("pages", 0) or 0)
        a["input"] += int(r.get("input", 0) or 0) + int(r.get("cache_create", 0) or 0) + int(r.get("cache_read", 0) or 0)
        a["output"] += int(r.get("output", 0) or 0)
        a["cost"] += float(r.get("cost_usd", 0) or 0)
        a["calls"] += 1
    return {f"{k[0]}:{k[1]}": v for k, v in agg.items()}
