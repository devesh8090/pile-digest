"""Model stages: pilot, bulk transcription, summaries, escalation, spot checks.

Every model call is a fresh `claude -p` process (see worker.py). Results are
saved per page as soon as they arrive, so a stop at any point (usage limit,
budget, Ctrl-C) loses at most the batches in flight. Re-running resumes.
"""

from __future__ import annotations

import random
import shutil
import threading
import time
from collections import Counter, deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Dict, List, Optional

from . import ledger, parse, raster, worker
from .pile import Pile, load_prompt, wait_note
from .textutil import first_line_summary
from .triage import FREE_CLASSES, SKIP_CLASSES, VISION_CLASSES
from .util import free_bytes, now_iso, read_json, sha256_text, write_json

LEVEL_ORDER = ["free", "text", "bulk", "tiles", "strong", "main"]
MAX_IMAGES_PER_CALL = 10


class Stop(Exception):
    pass


class RunContext:
    def __init__(self, pile: Pile, *, budget: Optional[int] = None, run_id: Optional[str] = None):
        self.pile = pile
        self.cfg = pile.cfg
        self.run_id = run_id or time.strftime("%Y%m%d-%H%M%S")
        self.budget = budget if budget is not None else self.cfg.get("budget_tokens")
        self.spent = 0
        self.stop_reason: Optional[str] = None
        self.stop_detail = ""
        self.lock = threading.Lock()
        self.last_rate = None
        rows = ledger.read_jsonl(pile.ledger_path)
        for r in reversed(rows):
            if r.get("rate"):
                ra = r["rate"]
                try:
                    if ra.get("resets_at") and float(ra["resets_at"]) > time.time():
                        self.last_rate = ra
                except (TypeError, ValueError):
                    pass
                break
        prev = (pile.state().get("last_run") or {})
        if prev.get("run_id") == self.run_id:
            self.spent = int(prev.get("spent", 0))

    def guard(self, est_tokens: int) -> Optional[str]:
        if self.stop_reason:
            return self.stop_reason
        if self.budget and self.spent + est_tokens > self.budget:
            self.stop("budget", f"the token budget for this run ({self.budget:,}) would be exceeded")
        elif self.last_rate and self.last_rate.get("utilization") is not None:
            u = float(self.last_rate["utilization"])
            if u >= float(self.cfg["max_window_utilization"]):
                self.stop("window", f"the 5-hour usage window is {u:.0%} full; " + wait_note(self.last_rate.get("resets_at")))
        if not self.stop_reason and free_bytes(self.pile.dir) < (int(self.cfg.get("min_free_disk_mb", 500)) << 20):
            self.stop("disk", "the disk is nearly full")
        return self.stop_reason

    def stop(self, reason: str, detail: str) -> None:
        with self.lock:
            if not self.stop_reason:
                self.stop_reason = reason
                self.stop_detail = detail

    def account(self, wr: worker.WorkerResult, *, stage: str, model: str, pages: List[dict]) -> None:
        cls = Counter(p["cls"] for p in pages).most_common(1)[0][0] if pages else "mixed"
        with self.lock:
            used = wr.usage.get("input", 0) + wr.usage.get("cache_create", 0) + wr.usage.get("output", 0)
            self.spent += used
            if wr.rate:
                self.last_rate = wr.rate
        ledger.record(self.pile.ledger_path, run=self.run_id, stage=stage, model=wr.model_used or model,
                      pages=len(pages), cls=cls, ok=wr.ok, error=wr.error_kind, cost_usd=wr.cost_usd,
                      duration_ms=wr.duration_ms, rate=wr.rate, **wr.usage)
        if wr.error_kind in ("rate_limit", "usage_limit"):
            self.stop("window", "the usage limit was reached; " + wait_note((wr.rate or {}).get("resets_at")))
        elif wr.error_kind == "auth":
            self.stop("auth", "the claude CLI is not logged in (run `claude` once and log in)")
        elif wr.error_kind == "no_cli":
            self.stop("no_cli", wr.error)

    def save_progress(self, stage: str, done: int, total: int) -> None:
        self.pile.set_state(last_run={"run_id": self.run_id, "stage": stage, "batches_done": done,
                                      "batches_total": total, "spent": self.spent, "updated": now_iso(),
                                      "stopped": self.stop_reason, "stop_detail": self.stop_detail})


# ----------------------------------------------------------------- results


def save_result(pile: Pile, page: dict, parsed: dict, *, model: str, level: str, stage: str,
                content: Optional[str] = None) -> dict:
    rd = pile.results_dir
    rd.mkdir(parents=True, exist_ok=True)
    old = pile.result(page["page_id"])
    history = (old or {}).get("history", [])
    if old:
        history.append({k: old.get(k) for k in ("level", "model", "confidence", "escalate", "updated", "status")})
        prev_md = rd / f"{page['page_id']}.md"
        if prev_md.exists():
            shutil.copyfile(prev_md, rd / f"{page['page_id']}.{old.get('level', 'prev')}.md")
    if content is not None:
        parsed = dict(parsed)
        parsed["content"] = content
        parsed.setdefault("kind", "transcription")
    esc = parse.needs_escalation(parsed) if level in ("bulk", "tiles", "strong") else None
    meta = {
        "page_id": page["page_id"], "doc_id": page["doc_id"], "index": page["index"], "relpath": page.get("relpath"),
        "cls": page["cls"], "status": "done", "level": level, "model": model, "stage": stage,
        "kind": parsed.get("kind"), "confidence": parsed.get("confidence"), "summary": parsed.get("summary", ""),
        "topics": parsed.get("topics", []), "flags": parsed.get("flags", []), "markers": parsed.get("markers", []),
        "escalate": esc, "updated": now_iso(), "history": history[-6:],
    }
    if old and old.get("spot") and level == old.get("level"):
        meta["spot"] = old["spot"]
    from .util import atomic_write_text

    atomic_write_text(rd / f"{page['page_id']}.md", parse.render_result_md(page, parsed, model, level))
    write_json(rd / f"{page['page_id']}.json", meta)
    return meta


def mark(pile: Pile, pid: str, /, **fields) -> None:
    r = pile.result(pid) or {"page_id": pid}
    r.update(fields)
    r["updated"] = now_iso()
    write_json(pile.results_dir / f"{pid}.json", r)


def materialize_free(pile: Pile, pages: List[dict], summarize: bool) -> int:
    """Pages that need no model: title pages, and text pages when summaries are off."""
    n = 0
    for p in pages:
        if pile.result(p["page_id"]):
            continue
        if p["cls"] == "title" or (p["cls"] == "text-ok" and not summarize):
            parsed = {"kind": "transcription", "confidence": "high", "summary": first_line_summary(p["text"]),
                      "topics": [], "flags": [], "markers": []}
            save_result(pile, p, parsed, model="none", level="free", stage="free", content=p["text"])
            n += 1
    return n


# ----------------------------------------------------------------- rendering


def render_images(pile: Pile, page: dict, outdir: Path, tiles: bool) -> List[Path]:
    r = page.get("render") or {}
    cfg = pile.cfg
    long_side = int(cfg["render_long_side"])
    outdir.mkdir(parents=True, exist_ok=True)
    base = outdir / page["page_id"]
    try:
        if r.get("type") == "pdf":
            w = float(page.get("w_pt") or 612)
            h = float(page.get("h_pt") or 792)
            if tiles or page.get("tall"):
                rows = int(cfg["tile_rows"]) if not page.get("tall") else max(2, int(round((h / w) / 1.3)))
                if page.get("tall") and tiles:
                    rows += 1
                rows = min(rows, 8)
                out = []
                for i in range(rows):
                    t = raster.render_pdf_tile(Path(r["path"]), int(r["page"]), Path(f"{base}-t{i + 1}.png"), w, h,
                                               rows, i, long_side)
                    if t:
                        out.append(t)
                if len(out) == rows:
                    return out
                for t in out:
                    try:
                        t.unlink()
                    except OSError:
                        pass
            full = raster.render_pdf_page(Path(r["path"]), int(r["page"]), Path(f"{base}.png"), w, h, long_side)
            return [full] if full else []
        if r.get("type") == "image":
            src = Path(r["path"])
            if tiles or page.get("tall"):
                dims = raster.image_dims(src)
                rows = int(cfg["tile_rows"])
                if dims and page.get("tall"):
                    rows = max(rows, min(8, int(round((dims[1] / max(1, dims[0])) / 1.3))))
                out = [raster.crop_band(src, Path(f"{base}-t{i + 1}.png"), rows, i, long_side) for i in range(rows)]
                if all(out):
                    return out  # type: ignore[return-value]
            img = raster.prepare_image(src, Path(f"{base}.png"), long_side)
            return [img] if img else []
    except OSError:
        return []
    return []


# ----------------------------------------------------------------- batches


class Batch:
    def __init__(self, pages: List[dict], mode: str, level: str, model: str, stage: str, attempt: int = 0):
        self.pages = pages
        self.mode = mode          # transcribe | summarize | spotcheck | section
        self.level = level
        self.model = model
        self.stage = stage
        self.attempt = attempt

    def est_tokens(self, cfg: dict) -> int:
        from .estimate import page_image_tokens

        if self.mode == "summarize":
            return sum(min(len(p.get("text", "")), 1800) // 4 + 120 for p in self.pages)
        n = sum(page_image_tokens(p, cfg) * (2 if self.level in ("tiles", "strong") else 1) for p in self.pages)
        return n + 800 * len(self.pages)


def page_header(pile: Pile, p: dict, level: str, note: str = "") -> str:
    lines = [f"PAGE {p['page_id']}", f"(source: {p.get('relpath', '?')}, page {p['index']})"]
    if p.get("lowres_source"):
        lines.append("Note: this page was scanned at low resolution.")
    if level in ("tiles", "strong"):
        lines.append("The page is split into overlapping horizontal tiles, top to bottom, for higher resolution.")
    if note:
        lines.append(note)
    if p["cls"] == "hybrid" and p.get("text", "").strip():
        hint = p["text"][:2500]
        lines.append("TEXT LAYER HINT (may be incomplete or garbled around formulas; the image is the authority):\n"
                     + hint + "\nEND OF TEXT LAYER HINT")
    return "\n".join(lines)


def build_blocks(pile: Pile, batch: Batch, outdir: Path) -> Optional[List[dict]]:
    blocks: List[dict] = []
    if batch.mode == "summarize":
        blocks.append(worker.text_block(f"Summarise these {len(batch.pages)} page(s)."))
        for p in batch.pages:
            blocks.append(worker.text_block(f"PAGE {p['page_id']}\n(source: {p.get('relpath', '?')}, part {p['index']})\n"
                                            + p.get("text", "")[:1800]))
        return blocks
    tiles = batch.level in ("tiles", "strong")
    if batch.mode == "spotcheck":
        blocks.append(worker.text_block(f"Audit these {len(batch.pages)} transcription(s)."))
    else:
        blocks.append(worker.text_block(f"Transcribe these {len(batch.pages)} page(s)."))
    n_images = 0
    for p in batch.pages:
        imgs = render_images(pile, p, outdir, tiles=tiles if batch.mode == "transcribe" else bool(p.get("tall")))
        if not imgs:
            return None
        note = ""
        if batch.mode == "transcribe" and batch.level in ("tiles", "strong"):
            prev = pile.result(p["page_id"]) or {}
            issues = [f"{f['kind']}: {f['note']}" for f in (prev.get("flags") or [])[:6] if f.get("note")]
            if issues:
                note = "A previous reading had trouble with: " + "; ".join(issues)
        blocks.append(worker.text_block(page_header(pile, p, batch.level if batch.mode == "transcribe" else "", note)))
        for im in imgs:
            blocks.append(worker.image_block(im))
            n_images += 1
        if batch.mode == "spotcheck":
            body = pile.result_text(p["page_id"]).split("\n\n", 1)
            content = body[1] if len(body) > 1 else body[0]
            blocks.append(worker.text_block("TRANSCRIPTION TO AUDIT:\n" + content[:12000] + "\nEND OF TRANSCRIPTION"))
    return blocks


def make_batches(pages: List[dict], *, mode: str, level: str, model: str, stage: str, size: int) -> List[Batch]:
    out: List[Batch] = []
    cur: List[dict] = []
    imgs = 0
    for p in pages:
        n = 1
        if mode != "summarize":
            n = 3 if (level in ("tiles", "strong") or p.get("tall")) else 1
        if cur and (len(cur) >= size or imgs + n > MAX_IMAGES_PER_CALL):
            out.append(Batch(cur, mode, level, model, stage))
            cur, imgs = [], 0
        cur.append(p)
        imgs += n
    if cur:
        out.append(Batch(cur, mode, level, model, stage))
    return out


def execute(ctx: RunContext, batches: List[Batch], label: str) -> dict:
    """Run batches with bounded concurrency, retries, splitting and guards."""
    pile = ctx.pile
    cfg = ctx.cfg
    prompts = {"transcribe": load_prompt("transcribe"), "summarize": load_prompt("summarize"),
               "spotcheck": load_prompt("spotcheck")}
    queue = deque(batches)
    total = len(batches)
    done = 0
    stats = Counter()
    render_root = pile.renders_dir / ctx.run_id
    max_retries = int(cfg["max_retries"])
    conc = int(cfg["concurrency"])

    def one(b: Batch, k: int):
        outdir = render_root / f"{label}-{k:05d}"
        try:
            blocks = build_blocks(pile, b, outdir)
            if blocks is None:
                return b, None, "render"
            wr = worker.call(b.model, prompts[b.mode], blocks, timeout=float(cfg["call_timeout_s"]))
            return b, wr, None
        finally:
            if not cfg.get("keep_renders"):
                shutil.rmtree(outdir, ignore_errors=True)

    k = 0
    with ThreadPoolExecutor(max_workers=conc) as pool:
        inflight = set()
        while queue or inflight:
            while queue and len(inflight) < conc and not ctx.stop_reason:
                b = queue.popleft()
                if ctx.guard(b.est_tokens(cfg)):
                    queue.appendleft(b)
                    break
                k += 1
                inflight.add(pool.submit(one, b, k))
            if not inflight:
                break
            finished, inflight = wait(inflight, return_when=FIRST_COMPLETED)
            for fut in finished:
                b, wr, err = fut.result()
                if err == "render":
                    for p in b.pages:
                        mark(pile, p["page_id"], status="failed", level=b.level, error="could not render the page",
                             page_id=p["page_id"], doc_id=p["doc_id"], index=p["index"], relpath=p.get("relpath"),
                             cls=p["cls"])
                    stats["render_failed"] += len(b.pages)
                    done += 1
                    continue
                ctx.account(wr, stage=b.stage, model=b.model, pages=b.pages)
                if not wr.ok:
                    if wr.fatal:
                        queue.appendleft(b)
                        continue
                    stats["call_errors"] += 1
                    _retry_or_fail(pile, queue, b, max_retries, wr.error_kind or "error", stats)
                    continue
                ids = [p["page_id"] for p in b.pages]
                pmode = {"transcribe": "transcribe", "summarize": "summarize", "spotcheck": "spotcheck"}[b.mode]
                parsed, problems = parse.parse_blocks(wr.text, ids, mode=pmode)
                if wr.stop_reason == "max_tokens" and len(b.pages) > 1:
                    parsed = {}  # truncated: redo in smaller pieces
                for p in b.pages:
                    got = parsed.get(p["page_id"])
                    if got is None:
                        continue
                    _apply(pile, b, p, got, stats)
                missing = [p for p in b.pages if p["page_id"] not in parsed]
                if missing:
                    stats["parse_problems"] += len(missing)
                    _retry_or_fail(pile, queue, Batch(missing, b.mode, b.level, b.model, b.stage, b.attempt),
                                   max_retries, "unparseable answer", stats)
                done += 1
                stats["pages_ok"] += len(parsed)
                ctx.save_progress(label, done, total)
                _progress(ctx, label, done, total, stats)
    shutil.rmtree(render_root, ignore_errors=True)
    stats["left"] = sum(len(b.pages) for b in queue)
    ctx.save_progress(label, done, total)
    return dict(stats)


def _progress(ctx: RunContext, label: str, done: int, total: int, stats: Counter) -> None:
    from .util import eprint, human_tokens

    u = ctx.last_rate.get("utilization") if ctx.last_rate else None
    win = f", window {float(u):.0%}" if u is not None else ""
    eprint(f"[{label}] {done}/{total}+ batches, {stats['pages_ok']} pages ok, {human_tokens(ctx.spent)} tokens{win}")


def _retry_or_fail(pile: Pile, queue: deque, b: Batch, max_retries: int, why: str, stats: Counter) -> None:
    if len(b.pages) > 1:
        half = len(b.pages) // 2
        queue.append(Batch(b.pages[:half], b.mode, b.level, b.model, b.stage, b.attempt + 1))
        queue.append(Batch(b.pages[half:], b.mode, b.level, b.model, b.stage, b.attempt + 1))
        return
    if b.attempt < max_retries:
        queue.append(Batch(b.pages, b.mode, b.level, b.model, b.stage, b.attempt + 1))
        return
    p = b.pages[0]
    stats["failed"] += 1
    if b.mode == "spotcheck":
        mark(pile, p["page_id"], spot={"verdict": "error", "note": why, "at": now_iso()})
        return
    if b.mode == "summarize":
        parsed = {"kind": "transcription", "confidence": "medium", "summary": first_line_summary(p["text"]),
                  "topics": [], "flags": [{"kind": "other", "note": "summary failed; first line used"}], "markers": []}
        save_result(pile, p, parsed, model="none", level="free", stage="fallback", content=p["text"])
        return
    old = pile.result(p["page_id"])
    if old and old.get("status") == "done":
        mark(pile, p["page_id"], escalate=f"retry at level {b.level} failed: {why}", failed_level=b.level)
    else:
        mark(pile, p["page_id"], status="failed", level=b.level, error=why, page_id=p["page_id"], doc_id=p["doc_id"],
             index=p["index"], relpath=p.get("relpath"), cls=p["cls"], escalate=f"failed: {why}")


def _apply(pile: Pile, b: Batch, p: dict, got: dict, stats: Counter) -> None:
    if b.mode == "summarize":
        garbled = any("garbled" in (f.get("note", "") + f.get("kind", "")).lower() for f in got.get("flags", []))
        if garbled:
            pile.set_override(p["page_id"], "vision", "text layer looked garbled to the summariser")
            stats["reclassified"] += 1
            return
        save_result(pile, p, got, model=b.model, level="text", stage=b.stage, content=p["text"])
        return
    if b.mode == "spotcheck":
        spot = {"verdict": got["verdict"], "differences": got.get("differences", []), "model": b.model, "at": now_iso()}
        if got["verdict"] == "mismatch":
            if got.get("corrected"):
                from .util import atomic_write_text

                atomic_write_text(pile.results_dir / f"{p['page_id']}.suggested.md", got["corrected"] + "\n")
                spot["suggested"] = True
            mark(pile, p["page_id"], spot=spot, status="needs-main", review_reason="spot check found a mismatch")
            stats["mismatch"] += 1
        else:
            mark(pile, p["page_id"], spot=spot)
            stats[got["verdict"]] += 1
        return
    meta = save_result(pile, p, got, model=b.model, level=b.level, stage=b.stage)
    if meta.get("escalate"):
        stats["flagged"] += 1


# ----------------------------------------------------------------- stages


def todo_pages(pile: Pile, pages: List[dict], classes) -> List[dict]:
    res = pile.results()
    return [p for p in pages if p["cls"] in classes and p["page_id"] not in res]


def stage_summaries(ctx: RunContext, pages: List[dict], stage: str = "bulk") -> dict:
    cfg = ctx.cfg
    summarize = bool(cfg.get("summarize_text_pages", True))
    materialize_free(ctx.pile, pages, summarize)
    if not summarize:
        return {}
    todo = todo_pages(ctx.pile, pages, {"text-ok"})
    if not todo:
        return {}
    bs = make_batches(todo, mode="summarize", level="text", model=cfg["models"]["bulk"], stage=stage,
                      size=int(cfg["text_batch_pages"]))
    return execute(ctx, bs, "summaries")


def stage_bulk(ctx: RunContext, pages: List[dict], stage: str = "bulk") -> dict:
    cfg = ctx.cfg
    todo = todo_pages(ctx.pile, pages, VISION_CLASSES)
    if not todo:
        return {}
    bs = make_batches(todo, mode="transcribe", level="bulk", model=cfg["models"]["bulk"], stage=stage,
                      size=int(cfg["batch_pages"]))
    return execute(ctx, bs, "transcribe")


def escalation_candidates(pile: Pile, pages: List[dict]) -> Dict[str, List[dict]]:
    res = pile.results()
    out: Dict[str, List[dict]] = {"tiles": [], "strong": [], "main": []}
    for p in pages:
        r = res.get(p["page_id"])
        if not r or p["cls"] not in VISION_CLASSES:
            continue
        if r.get("status") not in ("done", "failed") or not r.get("escalate"):
            continue
        lvl = r.get("failed_level") or r.get("level")
        if lvl == "bulk":
            out["tiles"].append(p)
        elif lvl == "tiles":
            out["strong"].append(p)
        elif lvl == "strong":
            out["main"].append(p)
    return out


def stage_escalate(ctx: RunContext, pages: List[dict]) -> dict:
    cfg = ctx.cfg
    stats: Counter = Counter()
    for _round in range(3):
        cands = escalation_candidates(ctx.pile, pages)
        for p in cands["main"]:
            r = ctx.pile.result(p["page_id"]) or {}
            if r.get("status") != "needs-main":
                mark(ctx.pile, p["page_id"], status="needs-main",
                     review_reason=f"still unclear after the stronger model: {r.get('escalate')}")
                stats["to_main"] += 1
        work = []
        if cands["tiles"]:
            work += make_batches(cands["tiles"], mode="transcribe", level="tiles", model=cfg["models"]["bulk"],
                                 stage="escalate", size=max(1, int(cfg["batch_pages"]) // 2))
        if cands["strong"]:
            work += make_batches(cands["strong"], mode="transcribe", level="strong", model=cfg["models"]["escalate"],
                                 stage="escalate", size=max(1, int(cfg["batch_pages"]) // 2))
        if not work:
            break
        stats.update(execute(ctx, work, "escalate"))
        if ctx.stop_reason:
            break
    return dict(stats)


def stage_spotcheck(ctx: RunContext, pages: List[dict], seed: Optional[str] = None) -> dict:
    cfg = ctx.cfg
    res = ctx.pile.results()
    eligible = []
    for p in pages:
        r = res.get(p["page_id"])
        if not r or r.get("status") != "done" or p["cls"] not in VISION_CLASSES or r.get("kind") == "blank":
            continue
        if r.get("level") not in ("bulk", "tiles"):
            continue
        eligible.append((p, r))
    if not eligible:
        return {}
    already = [p for p, r in eligible if r.get("spot")]
    rng = random.Random(sha256_text(seed or ctx.pile.id))
    want = max(int(cfg["spotcheck_min"]), int(round(len(eligible) * float(cfg["spotcheck_fraction"]))))
    want = min(want, len(eligible))
    pick = [p for p, r in eligible if not r.get("spot") and r.get("level") == "tiles"]
    pool = [p for p, r in eligible if not r.get("spot") and r.get("level") == "bulk"]
    rng.shuffle(pool)
    need_random = max(0, want - len(already))
    pick += pool[:need_random]
    if not pick:
        return {"checked_before": len(already)}
    bs = make_batches(pick, mode="spotcheck", level="check", model=cfg["models"]["check"], stage="spotcheck", size=3)
    st = execute(ctx, bs, "spotcheck")
    st["sampled"] = len(pick)
    return st


def spot_summary(pile: Pile) -> dict:
    res = pile.results()
    verdicts = Counter()
    for r in res.values():
        s = r.get("spot")
        if s and s.get("verdict") in ("match", "minor", "mismatch"):
            verdicts[s["verdict"]] += 1
    n = sum(verdicts.values())
    rate = verdicts["mismatch"] / n if n else None
    advice = None
    if n >= 5 and rate is not None and rate > 0.15:
        advice = (f"{rate:.0%} of spot-checked pages had meaning-changing errors. Consider re-reading the vision "
                  "pages at a higher level: `pd.py rerun <pile> --level tiles` (cheap model, higher resolution) "
                  "or `--level strong` (stronger model).")
    return {"checked": n, "verdicts": dict(verdicts), "mismatch_rate": rate, "advice": advice}


def select_pilot(pile: Pile, pages: List[dict], n: int) -> List[dict]:
    res = pile.results()
    cands = [p for p in pages if p["cls"] in VISION_CLASSES and p["page_id"] not in res]
    if not cands:
        return []
    rng = random.Random(sha256_text(pile.id + "pilot"))
    hard = sorted(cands, key=lambda p: (p.get("source_ppi") or 9999, p.get("text_quality", 0)))
    picked: List[dict] = []
    seen = set()
    for p in hard[: max(1, n // 4)]:
        picked.append(p)
        seen.add(p["page_id"])
    by_cls: Dict[str, List[dict]] = {}
    for p in cands:
        by_cls.setdefault(p["cls"], []).append(p)
    for _cls, group in by_cls.items():
        rng.shuffle(group)
        for p in group[: max(1, n // (2 * len(by_cls)))]:
            if p["page_id"] not in seen and len(picked) < n:
                picked.append(p)
                seen.add(p["page_id"])
    rest = [p for p in cands if p["page_id"] not in seen]
    rng.shuffle(rest)
    picked += rest[: max(0, n - len(picked))]
    order = {p["page_id"]: i for i, p in enumerate(pages)}
    return sorted(picked, key=lambda p: order[p["page_id"]])


def stage_pilot(ctx: RunContext, pages: List[dict]) -> dict:
    cfg = ctx.cfg
    pick = select_pilot(ctx.pile, pages, int(cfg["pilot_pages"]))
    st: Dict = {}
    if pick:
        bs = make_batches(pick, mode="transcribe", level="bulk", model=cfg["models"]["bulk"], stage="pilot",
                          size=int(cfg["batch_pages"]))
        st = execute(ctx, bs, "pilot")
    text = todo_pages(ctx.pile, pages, {"text-ok"})[:6]
    if text and cfg.get("summarize_text_pages", True) and not ctx.stop_reason:
        bs = make_batches(text, mode="summarize", level="text", model=cfg["models"]["bulk"], stage="pilot", size=6)
        st2 = execute(ctx, bs, "pilot-text")
        st = {**st, **{f"text_{k}": v for k, v in st2.items()}}
    st["pilot_pages"] = [p["page_id"] for p in pick] + [p["page_id"] for p in text]
    return st


def review_queue(pile: Pile, pages: Optional[List[dict]] = None) -> dict:
    """Pages the main model should look at, and claims it should verify."""
    pages = pages or pile.pages()
    order = {p["page_id"]: p for p in pages}
    res = pile.results()
    review = []
    suspects = []
    for pid, r in res.items():
        if pid not in order:
            continue
        if r.get("status") in ("needs-main", "failed") and not r.get("reviewed"):
            review.append({"page_id": pid, "relpath": r.get("relpath"), "page": r.get("index"),
                           "reason": r.get("review_reason") or r.get("escalate") or r.get("error"),
                           "result": str(pile.results_dir / f"{pid}.md"),
                           "suggested": str(pile.results_dir / f"{pid}.suggested.md")
                           if (pile.results_dir / f"{pid}.suggested.md").exists() else None})
        for f in (r.get("flags") or []) + (r.get("markers") or []):
            if f.get("kind") == "source-suspect" and not r.get("verified"):
                suspects.append({"page_id": pid, "relpath": r.get("relpath"), "page": r.get("index"),
                                 "note": f.get("note", "")})
    key = {p: i for i, p in enumerate(order)}
    review.sort(key=lambda x: key.get(x["page_id"], 0))
    suspects.sort(key=lambda x: key.get(x["page_id"], 0))
    return {"review": review, "source_suspect": suspects}


def rerun(ctx: RunContext, pages: List[dict], level: str, doc: Optional[str] = None) -> dict:
    cfg = ctx.cfg
    res = ctx.pile.results()
    target = LEVEL_ORDER.index(level)
    sel = []
    for p in pages:
        if p["cls"] not in VISION_CLASSES:
            continue
        if doc and doc not in (p["doc_id"], p.get("relpath")):
            continue
        cur = (res.get(p["page_id"]) or {}).get("level", "bulk")
        if cur not in LEVEL_ORDER:
            cur = "bulk"
        if LEVEL_ORDER.index(cur) < target:
            sel.append(p)
    if not sel:
        return {"selected": 0}
    model = cfg["models"]["bulk"] if level == "tiles" else cfg["models"]["escalate"]
    bs = make_batches(sel, mode="transcribe", level=level, model=model, stage="rerun",
                      size=max(1, int(cfg["batch_pages"]) // 2))
    st = execute(ctx, bs, "rerun")
    st["selected"] = len(sel)
    return st


def is_done(pile: Pile, pages: List[dict]) -> bool:
    res = pile.results()
    return all(p["page_id"] in res for p in pages if p["cls"] not in SKIP_CLASSES)


__all__ = [
    "RunContext", "stage_pilot", "stage_summaries", "stage_bulk", "stage_escalate", "stage_spotcheck",
    "review_queue", "spot_summary", "rerun", "save_result", "mark", "materialize_free", "render_images",
    "is_done", "FREE_CLASSES", "read_json",
]
