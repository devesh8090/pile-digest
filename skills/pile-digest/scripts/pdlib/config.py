"""Configuration: defaults, user config file, per-pile overrides, state paths."""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any, Optional

from .util import read_json, write_json

DEFAULTS: dict = {
    # Where new piles appear. Each immediate subfolder of a watch root is a pile;
    # loose files directly inside a watch root form one extra pile.
    "watch_roots": [],
    # Models for each rung of the ladder (CLI aliases or full model names).
    "models": {"bulk": "haiku", "escalate": "sonnet", "check": "sonnet"},
    # Transcription batching.
    "batch_pages": 5,            # vision pages per cheap call
    "text_batch_pages": 20,      # text-only pages per summary call
    "concurrency": 3,
    "call_timeout_s": 600,
    "max_retries": 2,
    # Rendering. 1568 px is the long side above which images get downscaled
    # by the API anyway, so rendering larger only wastes tokens.
    "render_long_side": 1568,
    "triage_dpi": 24,
    "tile_rows": 2,              # the "tiles" level splits a page into this many tiles
    "keep_renders": False,
    "min_free_disk_mb": 500,
    # Summaries for pages whose text layer is already good.
    "summarize_text_pages": True,
    # Levels a flagged page climbs after the bulk reading (the main model comes
    # last). "tiles" = cheap model on high-resolution tiles; opt-in, since it
    # rarely helps with handwriting: ["tiles", "strong"].
    "escalation_ladder": ["strong"],
    # Worker prompt caching. Off: each call has new images, so cache writes
    # were almost never read back and cost more than plain input.
    "worker_prompt_cache": False,
    # Guards. budget_tokens caps one run (input + cache writes + output, all models).
    "budget_tokens": None,
    # A run stops and asks again once spend since approval passes the approved
    # amount times this factor.
    "run_overrun_factor": 1.25,
    # Stop before the next call once the 5-hour window is this full (0..1).
    "max_window_utilization": 0.92,
    # Pilot and verification.
    "pilot_pages": 12,
    "spotcheck_fraction": 0.05,
    "spotcheck_min": 3,
    "section_digest_min_pages": 120,  # piles at least this big get section digests
    "section_chunk_pages": 25,        # fallback section size without an outline
    # Archives.
    "archive_max_total_mb": 4096,
    "archive_max_ratio": 250,
    "archive_max_members": 20000,
    "archive_max_depth": 3,
    # Text files are cut into pseudo-pages of about this many characters.
    "text_page_chars": 3500,
    "max_text_pages_per_file": 200,
    # Typed pages dense with formulas lose structure in text layers, so they
    # are read from the image (with the text layer as a hint).
    "math_pages_use_vision": True,
    # Very tall pages (aspect ratio above this) are tiled even on first pass.
    "tall_page_ratio": 2.2,
    # Extra ignore globs (on top of built-in junk patterns and .pile-digest-ignore).
    "ignore": [],
}


def state_root() -> Path:
    env = os.environ.get("PILE_DIGEST_HOME")
    if env:
        return Path(env).expanduser()
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(Path.home(), ".local", "state")
    return Path(base) / "pile-digest"


def config_path() -> Path:
    env = os.environ.get("PILE_DIGEST_CONFIG")
    if env:
        return Path(env).expanduser()
    home = os.environ.get("PILE_DIGEST_HOME")
    if home:
        return Path(home).expanduser() / "config.json"
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(Path.home(), ".config")
    return Path(base) / "pile-digest" / "config.json"


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_user_config() -> dict:
    data = read_json(config_path(), {}) or {}
    if not isinstance(data, dict):
        data = {}
    return data


def load(pile_overrides: Optional[dict] = None) -> dict:
    cfg = _merge(DEFAULTS, load_user_config())
    if pile_overrides:
        cfg = _merge(cfg, pile_overrides)
    validate(cfg)
    return cfg


def validate(cfg: dict) -> None:
    def clamp_int(key: str, lo: int, hi: int) -> None:
        try:
            cfg[key] = max(lo, min(hi, int(cfg[key])))
        except (TypeError, ValueError):
            cfg[key] = DEFAULTS[key]

    clamp_int("batch_pages", 1, 20)
    clamp_int("text_batch_pages", 1, 60)
    clamp_int("concurrency", 1, 8)
    clamp_int("render_long_side", 512, 4000)
    clamp_int("pilot_pages", 1, 60)
    clamp_int("tile_rows", 2, 6)
    try:
        f = float(cfg["max_window_utilization"])
        cfg["max_window_utilization"] = min(1.0, max(0.05, f))
    except (TypeError, ValueError):
        cfg["max_window_utilization"] = DEFAULTS["max_window_utilization"]
    try:
        cfg["run_overrun_factor"] = min(5.0, max(1.0, float(cfg.get("run_overrun_factor", 1.25))))
    except (TypeError, ValueError):
        cfg["run_overrun_factor"] = DEFAULTS["run_overrun_factor"]
    lad = cfg.get("escalation_ladder")
    if not isinstance(lad, list) or not [x for x in lad if x in ("tiles", "strong")]:
        cfg["escalation_ladder"] = list(DEFAULTS["escalation_ladder"])
    if cfg.get("budget_tokens") is not None:
        try:
            cfg["budget_tokens"] = int(cfg["budget_tokens"])
        except (TypeError, ValueError):
            cfg["budget_tokens"] = None


def set_user_value(key: str, value: Any) -> dict:
    data = load_user_config()
    data[key] = value
    write_json(config_path(), data)
    return data
