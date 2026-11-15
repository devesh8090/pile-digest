"""Token estimates before any model call, calibrated by the pilot afterwards."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

from . import ledger, raster
from .triage import FREE_CLASSES, SKIP_CLASSES, VISION_CLASSES

# Prior guesses (tokens) used until the pilot measures real values.
PRIOR_OUTPUT = {"vision": 650, "hybrid": 600, "text-ok": 70}
PAGE_WRAPPER = 60          # per-page instructions and delimiters in the request
HINT_CHARS = 2500          # text-layer hint cap for hybrid pages
ESC_TILE_SHARE = 0.10      # share of vision pages expected to need tiles with the bulk model
ESC_STRONG_SHARE = 0.03    # share expected to reach the stronger model
CHECK_OUTPUT = 150
SECTION_OUTPUT = 350
DIGEST_LINE = 38           # tokens per page line in the digest
MAIN_OPEN_PAGE = 900       # tokens when the main model opens one full page


def page_image_tokens(p: dict, cfg: dict) -> int:
    w = p.get("w_pt") or p.get("w_px") or 612
    h = p.get("h_pt") or p.get("h_px") or 792
    if p.get("tall"):
        rows = max(2, int(round((h / w) / 1.3)))
        tw, th = raster.target_dims(w, h / rows * 1.08, int(cfg["render_long_side"]))
        return rows * ((tw * th) // 750 + 1)
    tw, th = raster.target_dims(w, h, int(cfg["render_long_side"]))
    return (tw * th) // 750 + 1


def prompt_tokens(prompt_text: str) -> int:
    return len(prompt_text) // 4 + 1


def estimate(pages: List[dict], cfg: dict, *, prompts: Dict[str, str], ledger_path: Optional[Path] = None,
             done_ids: Optional[set] = None) -> dict:
    done_ids = done_ids or set()
    cal = ledger.per_page_stats(ledger_path) if ledger_path else {}
    out_per = dict(PRIOR_OUTPUT)
    calibrated = False
    for key, a in cal.items():
        stage, cls = key.split(":", 1)
        if stage in ("pilot", "bulk") and cls in out_per and a["pages"] >= 3:
            out_per[cls] = a["output"] / a["pages"]
            calibrated = True

    todo = [p for p in pages if p["page_id"] not in done_ids]
    vision = [p for p in todo if p["cls"] in VISION_CLASSES]
    textok = [p for p in todo if p["cls"] == "text-ok"]
    skipped = [p for p in pages if p["cls"] in SKIP_CLASSES]
    free = [p for p in pages if p["cls"] in FREE_CLASSES]

    sys_t = prompt_tokens(prompts.get("transcribe", ""))
    sys_s = prompt_tokens(prompts.get("summarize", ""))
    bp = int(cfg["batch_pages"])
    tbp = int(cfg["text_batch_pages"])

    v_img = sum(page_image_tokens(p, cfg) for p in vision)
    v_hint = sum(min(len(p.get("text", "")), HINT_CHARS) // 4 for p in vision if p["cls"] == "hybrid")
    v_calls = -(-len(vision) // bp) if vision else 0
    v_in = v_img + v_hint + PAGE_WRAPPER * len(vision) + 200 * v_calls
    v_out = sum(out_per["hybrid" if p["cls"] == "hybrid" else "vision"] for p in vision)
    v_sys = sys_t * v_calls  # mostly cache reads after the first call

    summarize = bool(cfg.get("summarize_text_pages", True))
    t_calls = -(-len(textok) // tbp) if (textok and summarize) else 0
    t_in = (sum(min(len(p.get("text", "")), 1800) // 4 for p in textok) + 40 * len(textok)) if summarize else 0
    t_out = int(out_per["text-ok"] * len(textok)) if summarize else 0

    avg_img = (v_img / len(vision)) if vision else 1500
    esc1_pages = int(round(len(vision) * ESC_TILE_SHARE))
    esc2_pages = int(round(len(vision) * ESC_STRONG_SHARE))
    esc1 = esc1_pages * (avg_img * 2 + 900)
    esc2 = esc2_pages * (avg_img * 2 + 900)

    n_trans = len(vision)
    n_check = max(int(cfg["spotcheck_min"]), int(round(n_trans * float(cfg["spotcheck_fraction"])))) if n_trans else 0
    n_check = min(n_check, n_trans)
    check = n_check * (avg_img + 700 + CHECK_OUTPUT)

    content_pages = len(pages) - len(skipped)
    sections = 0
    if content_pages >= int(cfg["section_digest_min_pages"]):
        sections = -(-content_pages // int(cfg["section_chunk_pages"]))
    sect = sections * (int(cfg["section_chunk_pages"]) * 45 + SECTION_OUTPUT)

    digest_tokens = 600 + DIGEST_LINE * content_pages + sections * 120
    flagged_guess = esc2_pages + n_check // 4
    main_read = digest_tokens + MAIN_OPEN_PAGE * flagged_guess
    naive_main = sum(out_per.get(p["cls"], 650) if p["cls"] in VISION_CLASSES else len(p.get("text", "")) // 4
                     for p in pages if p["cls"] not in SKIP_CLASSES) + v_img

    bulk_total = v_in + v_out + v_sys + t_in + t_out + sys_s * t_calls + esc1
    strong_total = esc2 + check + sect
    res = {
        "pages": {"total": len(pages), "skipped": len(skipped),
                  "free": len([p for p in free if p["cls"] == "title"]) + (0 if summarize else len(textok)),
                  "text_summaries": len(textok) if summarize else 0, "vision": len(vision),
                  "already_done": len([p for p in pages if p["page_id"] in done_ids])},
        "calls": {"bulk": v_calls + t_calls, "escalation_est": esc1_pages + esc2_pages, "spotcheck": n_check,
                  "sections": sections},
        "tokens": {
            "bulk_model": int(bulk_total),
            "strong_model": int(strong_total),
            "main_model_reading": int(main_read),
            "main_model_if_reading_everything": int(naive_main),
        },
        "detail": {"vision_image": v_img, "vision_output": int(v_out), "text_summary_in": t_in,
                   "text_summary_out": t_out, "escalation_tiles": int(esc1), "escalation_strong": int(esc2),
                   "spotcheck": int(check), "section_digests": int(sect), "system_prompt_per_call": sys_t},
        "calibrated_from_pilot": calibrated,
        "assumed_output_per_page": {k: int(v) for k, v in out_per.items()},
    }
    res["window_projection"] = window_projection(ledger_path, res) if ledger_path else None
    return res


def window_projection(ledger_path: Path, est: dict) -> Optional[dict]:
    """Rough share of the 5-hour window the rest would use, from pilot deltas."""
    rows = [r for r in ledger.read_jsonl(ledger_path) if r.get("ok") and r.get("rate") and r.get("stage") == "pilot"]
    if len(rows) < 2:
        return None
    u0 = rows[0]["rate"].get("utilization")
    u1 = rows[-1]["rate"].get("utilization")
    if u0 is None or u1 is None or rows[0]["rate"].get("resets_at") != rows[-1]["rate"].get("resets_at"):
        return None
    tokens = sum(ledger.total_tokens(r) for r in rows[1:])
    delta = float(u1) - float(u0)
    if tokens <= 0 or delta <= 0:
        return {"note": "the pilot was too small to move the usage meter; the full run is likely modest"}
    per_token = delta / tokens
    rest = est["tokens"]["bulk_model"] + est["tokens"]["strong_model"] * 3  # stronger model weighs more
    return {"window_share": round(per_token * rest, 3),
            "note": "rough: measured from usage-meter changes during the pilot, which also include any other "
                    "Claude use on the account at the same time"}


def format_estimate(est: dict) -> str:
    from .util import human_tokens as ht

    p = est["pages"]
    t = est["tokens"]
    lines = [
        f"Pages: {p['total']} total | {p['skipped']} skipped (blank/duplicate/repeated title) | "
        f"{p['free']} free | {p['text_summaries']} with a good text layer (cheap summary only) | "
        f"{p['vision']} need vision" + (f" | {p['already_done']} already done" if p["already_done"] else ""),
        f"Bulk model (cheap): ~{ht(t['bulk_model'])} tokens in ~{est['calls']['bulk']} calls "
        f"(+ ~{est['calls']['escalation_est']} escalations expected)",
        f"Stronger model: ~{ht(t['strong_model'])} tokens (escalations, {est['calls']['spotcheck']} spot checks, "
        f"{est['calls']['sections']} section digests)",
        f"Main model: reads ~{ht(t['main_model_reading'])} tokens of digest and flagged pages, instead of "
        f"~{ht(t['main_model_if_reading_everything'])} if it read everything",
        "Estimate is " + ("calibrated from the pilot." if est["calibrated_from_pilot"] else
                          "a prior guess; run the pilot to calibrate it."),
    ]
    wp = est.get("window_projection")
    if wp:
        if "window_share" in wp:
            lines.append(f"5-hour window: the rest would use roughly {wp['window_share']:.0%} ({wp['note']})")
        else:
            lines.append(f"5-hour window: {wp['note']}")
    return "\n".join(lines)
