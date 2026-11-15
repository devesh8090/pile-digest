"""One fresh, single-turn, tool-less `claude -p` call per batch.

Each call carries a short custom system prompt (cached across calls) and one
user message with inline images. Nothing accumulates between calls, which is
what keeps cache-read tokens low compared with long-lived subagents.
"""

from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path
from typing import List, Optional

from .util import run, which

MEDIA = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}


class WorkerResult:
    def __init__(self) -> None:
        self.ok = False
        self.text = ""
        self.error_kind: Optional[str] = None  # rate_limit | usage_limit | auth | transient | timeout | crash | refused
        self.error = ""
        self.usage: dict = {}
        self.cost_usd = 0.0
        self.rate: Optional[dict] = None
        self.duration_ms = 0
        self.model_used: Optional[str] = None
        self.stop_reason: Optional[str] = None

    @property
    def fatal(self) -> bool:
        """Errors where more calls now are pointless."""
        return self.error_kind in ("rate_limit", "usage_limit", "auth", "no_cli")


def claude_bin() -> Optional[str]:
    return which("claude")


def text_block(text: str) -> dict:
    return {"type": "text", "text": text}


def image_block(path: Path) -> dict:
    data = Path(path).read_bytes()
    mt = MEDIA.get(Path(path).suffix.lower(), "image/png")
    return {"type": "image", "source": {"type": "base64", "media_type": mt, "data": base64.b64encode(data).decode()}}


def build_command(model: str, system_prompt: str) -> List[str]:
    exe = claude_bin()
    return [
        exe or "claude", "-p",
        "--model", model,
        "--system-prompt", system_prompt,
        "--tools", "",
        "--strict-mcp-config",
        "--setting-sources", "",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",
    ]


def _classify_error(text: str, status) -> str:
    t = (text or "").lower()
    if status == 429 or "rate limit" in t or "rate_limit" in t:
        return "rate_limit"
    if "usage limit" in t or "limit reached" in t or "out of usage" in t or "reached your" in t:
        return "usage_limit"
    if status in (401, 403) or "/login" in t or "invalid api key" in t or "authentication" in t or "not logged in" in t:
        return "auth"
    if status in (500, 502, 503, 504, 529) or "overloaded" in t or "timeout" in t or "connection" in t:
        return "transient"
    if "prompt is too long" in t or "too large" in t or "image exceeds" in t:
        return "too_big"
    return "crash"


def call(model: str, system_prompt: str, blocks: List[dict], *, timeout: float = 600) -> WorkerResult:
    res = WorkerResult()
    if not claude_bin():
        res.error_kind = "no_cli"
        res.error = "the `claude` command was not found on PATH"
        return res
    msg = {"type": "user", "message": {"role": "user", "content": blocks}}
    payload = (json.dumps(msg) + "\n").encode()
    env = dict(os.environ)
    env.setdefault("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
    t0 = time.time()
    r = run(build_command(model, system_prompt), input_bytes=payload, timeout=timeout, env=env)
    res.duration_ms = int((time.time() - t0) * 1000)
    if r.timed_out:
        res.error_kind = "timeout"
        res.error = f"no answer within {int(timeout)} s"
        return res
    result_ev = None
    for line in r.out().splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        et = ev.get("type")
        if et == "rate_limit_event":
            info = ev.get("rate_limit_info") or {}
            five = ((info.get("unifiedWindows") or {}).get("five_hour") or {})
            res.rate = {
                "status": info.get("status"),
                "type": info.get("rateLimitType"),
                "utilization": five.get("utilization", info.get("utilization")),
                "resets_at": five.get("resetsAt", info.get("resetsAt")),
                "seven_day": ((info.get("unifiedWindows") or {}).get("seven_day") or {}).get("utilization"),
            }
        elif et == "result":
            result_ev = ev
    if result_ev is None:
        res.error_kind = _classify_error(r.err() + r.out()[-2000:], None)
        res.error = (r.err().strip() or r.out().strip())[-600:] or f"claude exited with code {r.returncode}"
        return res
    u = result_ev.get("usage") or {}
    res.usage = {
        "input": int(u.get("input_tokens", 0) or 0),
        "cache_create": int(u.get("cache_creation_input_tokens", 0) or 0),
        "cache_read": int(u.get("cache_read_input_tokens", 0) or 0),
        "output": int(u.get("output_tokens", 0) or 0),
    }
    res.cost_usd = float(result_ev.get("total_cost_usd", 0) or 0)
    mu = result_ev.get("modelUsage") or {}
    res.model_used = next(iter(mu), None)
    res.stop_reason = result_ev.get("stop_reason")
    if result_ev.get("is_error") or result_ev.get("subtype") != "success":
        res.error_kind = _classify_error(str(result_ev.get("result", "")) + " " + r.err(), result_ev.get("api_error_status"))
        res.error = str(result_ev.get("result") or result_ev.get("subtype") or "error")[:600]
        return res
    if res.rate and res.rate.get("status") == "rejected":
        res.error_kind = "usage_limit"
        res.error = "the usage window is exhausted"
        return res
    res.text = str(result_ev.get("result") or "")
    res.ok = True
    return res


def selftest(model: str = "haiku") -> WorkerResult:
    """A tiny real call that proves the CLI works and is logged in."""
    return call(model, "Reply with the single word OK.", [text_block("Say OK.")], timeout=120)
