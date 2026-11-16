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
from .textutil import content_tokens, first_line_summary, overlap
from .triage import FREE_CLASSES, SKIP_CLASSES, VISION_CLASSES
from .util import free_bytes, now_iso, read_json, sha256_text, write_json

LEVEL_ORDER = ["free", "text", "bulk", "tiles", "strong", "main"]
MAX_IMAGES_PER_CALL = 10


class Stop(Exception):
    pass


class RunContext:
    def __init__(self, pile: Pile, *, budget: Optional[int] = None, run_id: Optional[str] = None,
                 approval_cap: bool = False):
        self.pile = pile
        self.cfg = pile.cfg
        self.run_id = run_id or time.strftime("%Y%m%d-%H%M%S")
        self.budget = budget if budget is not None else self.cfg.get("budget_tokens")
        self.spent = 0
        # Spend since the user's approval may not run past the approved amount
        # by more than run_overrun_factor; the run then stops and asks again.
        self.cap: Optional[int] = None
        self.base_spent = 0
        appr = pile.state().get("approved") if approval_cap else None
        if appr:
            total = int(appr.get("total_tokens") or (appr.get("bulk_tokens", 0) + appr.get("strong_tokens", 0)))
            self.approved = total
            self.cap = int(total * float(self.cfg.get("run_overrun_factor", 1.25))) + 10000
            self.base_spent = ledger.spent_since(pile.ledger_path, appr)
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
        elif self.cap and self.base_spent + self.spent + est_tokens > self.cap:
            from .util import human_tokens as ht

            self.stop("approval", f"spend since approval would pass the approved ~{ht(self.approved)} tokens by more than "
                      f"{float(self.cfg.get('run_overrun_factor', 1.25)) - 1:.0%} (spent ~{ht(self.base_spent + self.spent)}). "
                      f"Show the user `pd.py estimate {self.pile.id}` and, if they agree, `pd.py approve {self.pile.id}` "
                      "(or `--add <tokens>`), then run again")
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
    # Verdicts, spot-check history and review notes belong to the page, not to
    # one reading of it: a new version keeps them.
    for k in ("verifications", "verified", "spots", "rejected"):
        if old and old.get(k):
            meta[k] = old[k]
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
        n = sum(page_image_tokens(p, cfg) * (2 if self.level == "tiles" else 1) for p in self.pages)
        extra = 2400 if self.level == "strong" else 800
        return n + extra * len(self.pages)


def page_header(pile: Pile, p: dict, level: str, note: str = "") -> str:
    lines = [f"PAGE {p['page_id']}", f"(source: {p.get('relpath', '?')}, page {p['index']})"]
    if p.get("lowres_source"):
        lines.append("Note: this page was scanned at low resolution.")
    if level == "tiles" or p.get("tall"):
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
    tiles = batch.level == "tiles"
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
        earlier = ""
        if batch.mode == "transcribe" and batch.level in ("tiles", "strong"):
            prev = pile.result(p["page_id"]) or {}
            issues = [f"{f['kind']}: {f['note']}" for f in parse.claims(prev)[:6] if f.get("note")]
            if issues:
                note = "A previous reading had trouble with: " + "; ".join(issues)
            if batch.level == "strong":
                body = result_body(pile, p["page_id"])
                if body.strip() and not prev.get("no_image"):
                    earlier = ("EARLIER TRANSCRIPTION of this page by a cheaper model. It may contain misreadings; the "
                               "image is the authority. Keep what is right, correct what is wrong, and never replace "
                               "it with content that is not on this page:\n" + body[:6000] + "\nEND OF EARLIER TRANSCRIPTION")
        blocks.append(worker.text_block(page_header(pile, p, batch.level if batch.mode == "transcribe" else "", note)))
        for im in imgs:
            blocks.append(worker.image_block(im))
            n_images += 1
        if earlier:
            blocks.append(worker.text_block(earlier))
        if batch.mode == "spotcheck":
            body = pile.result_text(p["page_id"]).split("\n\n", 1)
            content = body[1] if len(body) > 1 else body[0]
            blocks.append(worker.text_block("TRANSCRIPTION TO AUDIT:\n" + content[:12000] + "\nEND OF TRANSCRIPTION"))
    if n_images < len(batch.pages):
        return None  # never send a batch with a page whose image is missing
    return blocks


def result_body(pile: Pile, pid: str) -> str:
    """A stored transcription without its small header."""
    parts = pile.result_text(pid).split("\n\n", 1)
    return parts[1] if len(parts) > 1 else ""


def accept_escalation(page: dict, old_body: str, new: dict) -> Optional[str]:
    """Why a higher-level reading must NOT replace the current one (None = accept).

    A higher rung once returned a different page's content with high confidence.
    A real re-reading of the same page shares much of its words and numbers with
    the earlier reading and, where there is one, with the page's text layer."""
    if new.get("kind") in ("blank", "figure"):
        return None
    nt = content_tokens(new.get("content", ""))
    if len(nt) < 6:
        return None
    text = page.get("text") or ""
    if len(text.strip()) >= 40:
        tl = content_tokens(text)
        ot = content_tokens(old_body)
        if len(tl) >= 8 and overlap(nt, tl) < 0.12 and overlap(ot, tl) >= 0.3:
            return "the escalated reading shares almost nothing with the page's text layer, while the earlier one did"
    ot = content_tokens(old_body)
    if len(ot) >= 15 and overlap(nt, ot) < 0.15:
        return "the escalated reading shares almost nothing with the earlier reading of this page"
    return None


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
            wr = worker.call(b.model, prompts[b.mode], blocks, timeout=float(cfg["call_timeout_s"]),
                             prompt_cache=bool(cfg.get("worker_prompt_cache", False)))
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
                again = []
                for p in b.pages:
                    got = parsed.get(p["page_id"])
                    if got is None:
                        continue
                    if b.mode == "transcribe" and parse.no_image_reply(got) and b.attempt < 1:
                        # The model claims it got no image (seen on dark scans). Ask again,
                        # one page per call, at the same level instead of escalating.
                        again.append(p)
                        stats["no_image_retry"] += 1
                        continue
                    if b.mode == "transcribe" and parse.no_image_reply(got):
                        got = dict(got)
                        got["no_image"] = True
                    _apply(pile, b, p, got, stats)
                for p in again:
                    queue.append(Batch([p], b.mode, b.level, b.model, b.stage, b.attempt + 1))
                missing = [p for p in b.pages if p["page_id"] not in parsed]
                if missing:
                    stats["parse_problems"] += len(missing)
                    _retry_or_fail(pile, queue, Batch(missing, b.mode, b.level, b.model, b.stage, b.attempt),
                                   max_retries, "unparseable answer", stats)
                done += 1
                stats["pages_ok"] += len(parsed) - len(again)
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
            pile.set_override(p["page_id"], "vision", "text layer looked garbled to the summariser", auto=True)
            stats["reclassified"] += 1
            return
        save_result(pile, p, got, model=b.model, level="text", stage=b.stage, content=p["text"])
        return
    if b.mode == "spotcheck":
        cur = pile.result(p["page_id"]) or {}
        spot = {"verdict": got["verdict"], "differences": got.get("differences", []), "model": b.model, "at": now_iso(),
                "level": cur.get("level")}
        spots = list(cur.get("spots") or []) + [{k: spot[k] for k in ("verdict", "level", "model", "at")}]
        if got["verdict"] == "mismatch":
            if got.get("corrected"):
                from .util import atomic_write_text

                atomic_write_text(pile.results_dir / f"{p['page_id']}.suggested.md", got["corrected"] + "\n")
                spot["suggested"] = True
            mark(pile, p["page_id"], spot=spot, spots=spots, status="needs-main",
                 review_reason="spot check found a mismatch")
            stats["mismatch"] += 1
        else:
            mark(pile, p["page_id"], spot=spot, spots=spots)
            stats[got["verdict"]] += 1
        return
    if b.level in ("tiles", "strong"):
        old = pile.result(p["page_id"]) or {}
        why = None if old.get("no_image") else accept_escalation(p, result_body(pile, p["page_id"]), got)
        if why:
            from .util import atomic_write_text

            atomic_write_text(pile.results_dir / f"{p['page_id']}.rejected-{b.level}.md",
                              parse.render_result_md(p, got, b.model, b.level))
            rej = list(old.get("rejected") or []) + [{"level": b.level, "model": b.model, "why": why, "at": now_iso()}]
            mark(pile, p["page_id"], status="needs-main", rejected=rej, failed_level=b.level,
                 review_reason=f"{why}; the earlier reading was kept, the rejected one is in "
                               f"{p['page_id']}.rejected-{b.level}.md")
            stats["rejected"] += 1
            return
    meta = save_result(pile, p, got, model=b.model, level=b.level, stage=b.stage)
    if got.get("no_image"):
        mark(pile, p["page_id"], no_image=True)
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


def ladder(cfg: dict) -> List[str]:
    """Levels a flagged page climbs after bulk, ending with the main model.

    Default: straight to the stronger model. The cheap model at higher
    resolution ("tiles") resolved few handwritten pages in practice and cost a
    lot, so it is opt-in (escalation_ladder: ["tiles", "strong"])."""
    lad = [x for x in (cfg.get("escalation_ladder") or ["strong"]) if x in ("tiles", "strong")]
    return lad or ["strong"]


def escalation_candidates(pile: Pile, pages: List[dict]) -> Dict[str, List[dict]]:
    res = pile.results()
    steps = ["bulk"] + ladder(pile.cfg) + ["main"]
    out: Dict[str, List[dict]] = {"tiles": [], "strong": [], "main": []}
    for p in pages:
        r = res.get(p["page_id"])
        if not r or p["cls"] not in VISION_CLASSES:
            continue
        if r.get("status") not in ("done", "failed") or not r.get("escalate"):
            continue
        lvl = r.get("failed_level") or r.get("level")
        if lvl == "tiles" and lvl not in steps:
            lvl = "bulk"  # read at tiles by an earlier version or a manual rerun
        if lvl not in steps:
            continue
        nxt = steps[steps.index(lvl) + 1] if steps.index(lvl) + 1 < len(steps) else None
        if nxt in out:
            out[nxt].append(p)
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
    """Audit a random sample of every reading level against the page images.

    Each level is sampled on its own, so the mismatch rate says which level is
    weak instead of mixing them into one pile-wide figure."""
    cfg = ctx.cfg
    res = ctx.pile.results()
    by_level: Dict[str, List[tuple]] = {}
    for p in pages:
        r = res.get(p["page_id"])
        if not r or r.get("status") != "done" or p["cls"] not in VISION_CLASSES or r.get("kind") == "blank":
            continue
        if r.get("level") not in ("bulk", "tiles", "strong") or r.get("no_image"):
            continue
        by_level.setdefault(r["level"], []).append((p, r))
    if not by_level:
        return {}
    rng = random.Random(sha256_text(seed or ctx.pile.id))
    mn = int(cfg["spotcheck_min"])
    frac = float(cfg["spotcheck_fraction"])
    pick: List[dict] = []
    checked_before = 0
    for _lvl, items in sorted(by_level.items()):
        done_here = sum(1 for _p, r in items if _was_checked_at(r, r.get("level")))
        checked_before += done_here
        want = min(len(items), max(mn, int(round(len(items) * frac))))
        pool = [p for p, r in items if not _was_checked_at(r, r.get("level"))]
        rng.shuffle(pool)
        pick += pool[:max(0, want - done_here)]
    if not pick:
        return {"checked_before": checked_before}
    order = {p["page_id"]: i for i, p in enumerate(pages)}
    pick.sort(key=lambda p: order[p["page_id"]])
    bs = make_batches(pick, mode="spotcheck", level="check", model=cfg["models"]["check"], stage="spotcheck", size=3)
    st = execute(ctx, bs, "spotcheck")
    st["sampled"] = len(pick)
    return st


def _was_checked_at(r: dict, level: Optional[str]) -> bool:
    if any(s.get("level") == level for s in r.get("spots") or []):
        return True
    return bool(r.get("spot")) and not r.get("spots")


def spot_summary(pile: Pile) -> dict:
    """Spot-check results per reading level, from the full history: later fixes
    don't erase the original verdicts, they are counted separately."""
    res = pile.results()
    per: Dict[str, Counter] = {}
    corrected = 0
    for r in res.values():
        hist = list(r.get("spots") or [])
        if not hist and r.get("spot"):
            hist = [dict(r["spot"], level=r.get("level"))]
        for s in hist:
            if s.get("verdict") in ("match", "minor", "mismatch"):
                per.setdefault(s.get("level") or "?", Counter())[s["verdict"]] += 1
        if any(s.get("verdict") == "mismatch" for s in hist) and (r.get("fixed_by_main") or r.get("reviewed")):
            corrected += 1
    total = Counter()
    for c in per.values():
        total.update(c)
    n = sum(total.values())
    rate = total["mismatch"] / n if n else None
    levels = {}
    for lvl, c in per.items():
        k = sum(c.values())
        levels[lvl] = {"checked": k, "verdicts": dict(c), "mismatch_rate": (c["mismatch"] / k) if k else None}
    advice = None
    weak = [(lvl, v) for lvl, v in levels.items() if v["checked"] >= 3 and (v["mismatch_rate"] or 0) > 0.15]
    if weak:
        lvl, v = max(weak, key=lambda x: x[1]["mismatch_rate"])
        if lvl in ("bulk", "tiles"):
            advice = (f"{v['mismatch_rate']:.0%} of spot-checked {lvl}-level pages had meaning-changing errors. "
                      "Consider re-reading them with the stronger model: `pd.py rerun <pile> --level strong` "
                      "(add `--doc <file>` to limit it to the documents with the most mismatches; `--dry-run` "
                      "shows the cost first).")
        else:
            advice = (f"{v['mismatch_rate']:.0%} of spot-checked {lvl}-level pages had meaning-changing errors. "
                      "There is no higher automatic level: review those documents' flagged pages against their "
                      "images (`pd.py review <pile>`), and recompute numbers rather than trusting the transcription.")
    return {"checked": n, "verdicts": dict(total), "mismatch_rate": rate, "levels": levels,
            "corrected_since": corrected, "advice": advice}


def pilot_size(pages: List[dict], n: int) -> int:
    per_doc = Counter(p["doc_id"] for p in pages if p["cls"] in VISION_CLASSES)
    heavy = sum(1 for v in per_doc.values() if v >= 4)
    return min(30, max(n, 2 * heavy))


def select_pilot(pile: Pile, pages: List[dict], n: int) -> List[dict]:
    """A sample that represents every kind of document in the pile.

    Every document with at least 4 vision pages contributes 2 pages spread over
    it (so a handwritten notebook can't be missed), plus the hardest-looking
    pages pile-wide, then a random fill per class."""
    res = pile.results()
    cands = [p for p in pages if p["cls"] in VISION_CLASSES and p["page_id"] not in res]
    if not cands:
        return []
    n = pilot_size(pages, n)
    rng = random.Random(sha256_text(pile.id + "pilot"))
    picked: List[dict] = []
    seen = set()

    def take(p):
        if p["page_id"] not in seen and len(picked) < n:
            picked.append(p)
            seen.add(p["page_id"])

    by_doc: Dict[str, List[dict]] = {}
    for p in cands:
        by_doc.setdefault(p["doc_id"], []).append(p)
    for group in by_doc.values():
        if len(group) >= 4:
            take(group[len(group) // 3])
            take(group[(2 * len(group)) // 3])
    hard = sorted(cands, key=lambda p: (p.get("source_ppi") or 9999, -(p.get("ink") or 0)))
    for p in hard[: max(1, n // 5)]:
        take(p)
    by_cls: Dict[str, List[dict]] = {}
    for p in cands:
        by_cls.setdefault(p["cls"], []).append(p)
    for _cls, group in sorted(by_cls.items()):
        group = list(group)
        rng.shuffle(group)
        for p in group[: max(1, n // (3 * len(by_cls)))]:
            take(p)
    rest = [p for p in cands if p["page_id"] not in seen]
    rng.shuffle(rest)
    for p in rest:
        take(p)
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
        cl = parse.claims(r)
        shaky = r.get("confidence") == "low" or any(c["kind"] in ("illegible", "unsure") for c in cl)
        for f in cl:
            if f.get("kind") == "source-suspect" and not r.get("verified"):
                suspects.append({"page_id": pid, "relpath": r.get("relpath"), "page": r.get("index"),
                                 "note": f.get("note", ""), "misread_risk": shaky})
    key = {p: i for i, p in enumerate(order)}
    review.sort(key=lambda x: key.get(x["page_id"], 0))
    suspects.sort(key=lambda x: key.get(x["page_id"], 0))
    return {"review": review, "source_suspect": suspects}


def rerun(ctx: RunContext, pages: List[dict], level: str, doc: Optional[str] = None, dry_run: bool = False) -> dict:
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
    if dry_run:
        return {"selected": len(sel), "estimated_tokens": sum(b.est_tokens(cfg) + 1500 * len(b.pages) for b in bs)}
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
