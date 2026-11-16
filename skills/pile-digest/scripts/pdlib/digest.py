"""Build the compact digest the main model reads instead of the raw pile.

The digest is rebuilt for free (no model calls) whenever results change, so it
always shows the current readings, fixes and verdicts. Only section summaries
need a model; they are cached and reused.
"""

from __future__ import annotations

import re
import shutil
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import parse, worker
from .pile import Pile, load_prompt
from .textutil import clip
from .triage import SKIP_CLASSES, VISION_CLASSES
from .util import atomic_write_text, now_iso, read_json, sha256_text, write_json

PAGE_SUMMARY_CHARS = 160
FLAG_INDEX_PER_DOC = 25
FLAG_INDEX_TOTAL = 250
SPLIT_TOKENS = 12000          # above this, page lines move to one file per document
SEVERITY = {"source-suspect": 0, "needs-review": 1, "illegible": 2, "cut-off": 3, "unsure": 4, "typo": 5}
GENERIC_TOPICS = {
    "proof", "proofs", "example", "examples", "definition", "definitions", "theorem", "theorems", "lemma", "problem",
    "problems", "solution", "solutions", "exercise", "exercises", "notes", "lecture", "lecture notes", "slide", "slides",
    "introduction", "summary", "overview", "conclusion", "questions", "question", "answer", "answers", "method",
    "methods", "step", "steps", "table", "figure", "diagram", "graph", "formula", "formulas", "equation", "equations",
    "calculation", "calculations", "result", "results", "course", "syllabus", "homework", "assignment", "tutorial",
    "handwritten notes", "worked example", "thank you", "title page", "contents", "references", "outline",
}


def doc_labels(pile: Pile) -> Dict[str, str]:
    return {d["doc_id"]: f"D{i}" for i, d in enumerate(pile.index().get("docs", []), 1)}


def resolve_ref(pile: Pile, ref: str) -> Optional[str]:
    """Accept a page_id, or 'D3:12' (document label and page number)."""
    m = re.match(r"^D(\d+)[:#p ]+(\d+)$", ref.strip(), re.I)
    if m:
        docs = pile.index().get("docs", [])
        k = int(m.group(1))
        if 1 <= k <= len(docs):
            return f"{docs[k - 1]['doc_id']}-p{int(m.group(2)):04d}"
        return None
    return ref.strip()


def sections_for(doc: dict, pages: List[dict], chunk: int) -> List[Tuple[str, List[dict]]]:
    content = [p for p in pages if p["cls"] not in SKIP_CLASSES]
    if not content:
        return []
    tops = [o for o in (doc.get("outline") or []) if o.get("level") == 1 and o.get("page")]
    tops = sorted({o["page"]: o for o in tops}.values(), key=lambda o: o["page"])
    out: List[Tuple[str, List[dict]]] = []
    if len(tops) >= 2:
        bounds = [(o["title"], o["page"]) for o in tops]
        if bounds[0][1] > 1:
            bounds.insert(0, ("Front matter", 1))
        for i, (title, start) in enumerate(bounds):
            end = bounds[i + 1][1] - 1 if i + 1 < len(bounds) else 10 ** 9
            grp = [p for p in content if start <= p["index"] <= end]
            # Split long outline sections so each digest call stays small.
            for k in range(0, len(grp), chunk * 2):
                sub = grp[k:k + chunk * 2]
                if sub:
                    out.append((title if k == 0 else f"{title} (cont.)", sub))
        return out
    for k in range(0, len(content), chunk):
        sub = content[k:k + chunk]
        out.append((f"Pages {sub[0]['index']}–{sub[-1]['index']}", sub))
    return out


# ----------------------------------------------------------------- claims and verdicts


def verdict(r: dict) -> Optional[str]:
    v = r.get("verifications") or []
    return v[-1].get("verdict") if v else None


def open_claims(r: dict) -> List[dict]:
    """Claims still worth the reader's attention, after verdicts and reviews."""
    cl = [c for c in parse.claims(r) if c["kind"] in SEVERITY]
    if verdict(r) in ("correct", "source-error"):
        cl = [c for c in cl if c["kind"] != "source-suspect"]
    if r.get("reviewed") or r.get("level") == "main":
        cl = [c for c in cl if c["kind"] not in ("illegible", "unsure", "cut-off")]
    return cl


def _flag_text(r: dict) -> str:
    kinds = Counter(c["kind"] for c in open_claims(r))
    parts = [f"{k}×{n}" if n > 1 else k for k, n in sorted(kinds.items(), key=lambda x: SEVERITY.get(x[0], 9))]
    vd = verdict(r)
    if vd == "source-error":
        parts.insert(0, "✔ source error (verified)")
    elif vd == "correct":
        parts.insert(0, "✔ checked, correct")
    elif vd == "unclear":
        parts.insert(0, "? checked, unclear")
    if r.get("status") == "needs-main" and not r.get("reviewed"):
        parts.append("needs review")
    if r.get("status") == "failed":
        parts.append("unread")
    if r.get("fixed_by_main"):
        parts.append("corrected by main model")
    return (" ⚑ " + ", ".join(parts)) if parts else ""


# ----------------------------------------------------------------- section digests


def _section_key(title: str, lines: List[str]) -> str:
    return sha256_text(title + "\n" + "\n".join(lines))[:20]


def section_digests(pile: Pile, ctx, labels: Dict[str, str]) -> Dict[str, dict]:
    """Section summaries by the stronger model, cached by their page summaries.

    With ctx=None no model is called: cached summaries are reused even if some
    pages changed since (marked stale), and sections without one get a header
    and no summary, so their pages are never filed under the previous section."""
    cfg = pile.cfg
    pages = pile.pages()
    content = [p for p in pages if p["cls"] not in SKIP_CLASSES]
    out: Dict[str, dict] = {}
    if len(content) < int(cfg["section_digest_min_pages"]):
        return out
    res = pile.results()
    sdir = pile.dir / "sections"
    prompt = load_prompt("section")
    by_doc: Dict[str, List[dict]] = {}
    for p in pages:
        by_doc.setdefault(p["doc_id"], []).append(p)
    docs = {d["doc_id"]: d for d in pile.index().get("docs", [])}
    stop = False
    for did, dpages in by_doc.items():
        if len([p for p in dpages if p["cls"] not in SKIP_CLASSES]) < 15:
            continue
        for k, (title, grp) in enumerate(sections_for(docs.get(did, {}), dpages, int(cfg["section_chunk_pages"])), 1):
            lines = []
            page_keys = {}
            for p in grp:
                r = res.get(p["page_id"]) or {}
                if r.get("summary"):
                    lines.append(f"p{p['index']}: {clip(r['summary'], 300)}")
                    page_keys[p["page_id"]] = sha256_text(r["summary"])[:12]
            sid = f"{did}-s{k:03d}"
            base = {"title": title, "doc_id": did, "first": grp[0]["index"], "last": grp[-1]["index"], "summary": None}
            if not lines:
                out[sid] = base
                continue
            key = _section_key(title, lines)
            cached = read_json(sdir / f"{sid}.json", None)
            if cached and cached.get("key") != key and "page_keys" not in cached and cached.get("summary") \
                    and cached.get("first") == base["first"]:
                # Written by pile-digest 1.x with a different cache key: adopt it
                # rather than paying for the same summary again.
                cached.update(key=key, page_keys=page_keys, last=base["last"])
                write_json(sdir / f"{sid}.json", cached)
            if cached and cached.get("key") == key:
                out[sid] = cached
                continue
            if ctx is None or stop:
                if cached and cached.get("summary") and cached.get("first") == base["first"]:
                    old_keys = cached.get("page_keys") or {}
                    changed = sum(1 for pid, h in page_keys.items() if old_keys.get(pid) != h)
                    out[sid] = dict(cached, stale=changed or 1, last=base["last"])
                else:
                    out[sid] = base
                continue
            msg = (f"Section id: {sid}\nDocument: {docs.get(did, {}).get('relpath', did)} ({labels.get(did, '')})\n"
                   f"Section: {title}\n\n" + "\n".join(lines))
            model = cfg["models"]["escalate"]
            got = None
            for _attempt in range(2):
                if ctx.guard(len(msg) // 4 + 1500):
                    stop = True
                    break
                wr = worker.call(model, prompt, [worker.text_block(msg)], timeout=float(cfg["call_timeout_s"]),
                                 prompt_cache=bool(cfg.get("worker_prompt_cache", False)))
                ctx.account(wr, stage="sections", model=model, pages=[{"cls": "section"}])
                if not wr.ok:
                    if wr.fatal:
                        stop = True
                        break
                    continue
                parsed, _problems = parse.parse_blocks(wr.text, [sid], mode="summarize")
                if sid in parsed:
                    got = parsed[sid]
                    break
            if got is None:
                out[sid] = base
                continue
            data = {"key": key, "title": title, "doc_id": did, "first": grp[0]["index"], "last": grp[-1]["index"],
                    "summary": got["summary"], "topics": got.get("topics", []), "page_keys": page_keys, "at": now_iso()}
            write_json(sdir / f"{sid}.json", data)
            out[sid] = data
    return out


# ----------------------------------------------------------------- cross-file links


_FRAC_RE = re.compile(r"\\[dt]?frac\s*\{\s*(-?\d+)\s*\}\s*\{\s*(\d+)\s*\}")
_SIG_RE = re.compile(r"-?\d+(?:[./]\d+)?|[A-Za-z]_\{?\w+\}?")
_DATE_RE = re.compile(r"\b\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}\b")


def page_signature(text: str) -> set:
    """Numbers, fractions and indexed symbols on a page. Worked examples in
    different files that share many of them are probably the same problem."""
    text = re.sub(r"<!--.*?-->", " ", text or "", flags=re.S)
    text = _DATE_RE.sub(" ", text)
    text = _FRAC_RE.sub(lambda m: f" {m.group(1)}/{m.group(2)} ", text)
    toks = set()
    for t in _SIG_RE.findall(text):
        t = t.replace("{", "").replace("}", "")
        bare = t.lstrip("-")
        if bare.isdigit() and (len(bare) < 2 or (len(bare) == 4 and bare[:2] in ("19", "20"))):
            continue  # single digits and years say little
        toks.add(t)
    return toks


def _weight(t: str) -> int:
    """Fractions, decimals and long numbers are specific to one problem; short
    integers and symbol names occur everywhere."""
    bare = t.lstrip("-")
    if "/" in bare or "." in bare or (bare.isdigit() and len(bare) >= 3):
        return 2
    return 1


def related_pages(pile: Pile, pages: List[dict], res: Dict[str, dict], labels: Dict[str, str],
                  limit: int = 40) -> List[Tuple[str, str, int, float]]:
    sigs: Dict[str, set] = {}
    for p in pages:
        if p["cls"] in SKIP_CLASSES or p["page_id"] not in res:
            continue
        body = pile.result_text(p["page_id"]) or p.get("text", "")  # includes main-model fixes
        s = page_signature(body)
        if len(s) >= 4:
            sigs[p["page_id"]] = s
    df: Dict[str, List[str]] = defaultdict(list)
    for pid, s in sigs.items():
        for t in s:
            df[t].append(pid)
    score: Counter = Counter()
    specific: Counter = Counter()
    for t, pids in df.items():
        if 2 <= len(pids) <= 6:
            w = _weight(t)
            for i in range(len(pids)):
                for j in range(i + 1, len(pids)):
                    a, b = pids[i], pids[j]
                    if a.rsplit("-p", 1)[0] != b.rsplit("-p", 1)[0]:
                        score[(a, b)] += w
                        specific[(a, b)] += 1 if w == 2 else 0
    out = []
    for (a, b), sc in score.items():
        if sc >= 4 and specific[(a, b)] >= 2:
            out.append((a, b, sc, specific[(a, b)] / min(len(sigs[a]), len(sigs[b]))))
    out.sort(key=lambda x: (-x[2], -x[3]))

    def ref(pid: str) -> str:
        did, num = pid.rsplit("-p", 1)
        return f"{labels.get(did, '?')}:{int(num)}"

    return [(ref(a), ref(b), n, j) for a, b, n, j in out[:limit]]


# ----------------------------------------------------------------- build


_INLINE_MATH = re.compile(r"(?<!\$)\$([^$\n]{1,40})\$(?!\$)|\\\((.{1,40}?)\\\)")


def _outline(d: dict) -> List[str]:
    if not d.get("outline"):
        return []
    items = [f"{o['title']} (p{o['page']})" for o in d["outline"] if o.get("level", 1) <= 2 and o.get("page")]
    if not items:
        return []
    return ["Outline: " + " · ".join(items[:60]) + (" · …" if len(items) > 60 else "")]


def _doc_lines(dpages: List[dict], res: Dict[str, dict], secs: Dict[int, dict], labels: Dict[str, str]) -> List[str]:
    L: List[str] = []
    run_skip: List[str] = []

    def flush():
        if run_skip:
            L.append("- " + "; ".join(run_skip))
            run_skip.clear()

    for p in dpages:
        if p["index"] in secs:
            flush()
            s = secs[p["index"]]
            L.append("")
            L.append(f"### {s['title']} (p{s['first']}–p{s['last']})")
            if s.get("summary"):
                stale = f" *(summary predates changes on {s['stale']} page(s))*" if s.get("stale") else ""
                L.append(clip(s["summary"], 900) + stale)
            else:
                L.append("*(no section summary)*")
            L.append("")
        if p["cls"] == "blank":
            run_skip.append(f"p{p['index']} blank")
            continue
        if p["cls"] in ("duplicate", "repeated-title"):
            dup = p.get("dup_of", "")
            ref = dup
            m = re.match(r"^([0-9a-f]{16})-p(\d+)$", dup or "")
            if m:
                ref = f"{labels.get(m.group(1), '?')}:{int(m.group(2))}"
            run_skip.append(f"p{p['index']} {'same as' if p['cls'] == 'duplicate' else 'repeated title'} {ref}")
            continue
        flush()
        r = res.get(p["page_id"]) or {}
        summ = (r.get("summary") or "").strip()
        if not summ:
            summ = "(not read yet)" if not r else "(no summary)"
        tag = " [img]" if p["cls"] in VISION_CLASSES else ""
        tag += " [title]" if p["cls"] == "title" else ""
        L.append(f"- p{p['index']}{tag} {clip(summ, PAGE_SUMMARY_CHARS)}{_flag_text(r)}")
    flush()
    return L


def _flags_and_errors(pages: List[dict], res: Dict[str, dict], labels: Dict[str, str]) -> List[str]:
    confirmed = []
    rows: List[Tuple[int, str, str]] = []
    for p in pages:
        r = res.get(p["page_id"]) or {}
        ref = f"{labels.get(p['doc_id'])}:{p['index']}"
        for v in r.get("verifications") or []:
            if v.get("verdict") == "source-error":
                confirmed.append(f"- {ref} {clip(v.get('note', ''), 220)}")
        for c in open_claims(r):
            if c.get("note"):
                rows.append((SEVERITY.get(c["kind"], 9), p["doc_id"], f"- {ref} {c['kind']}: {clip(c['note'], 160)}"))
        if r.get("status") == "needs-main" and not r.get("reviewed"):
            rows.append((SEVERITY["needs-review"], p["doc_id"],
                         f"- {ref} needs-review: {clip(r.get('review_reason') or '', 160)}"))
    rows.sort(key=lambda x: x[0])
    shown: List[str] = []
    per_doc: Counter = Counter()
    hidden = 0
    for _sev, did, line in rows:
        if per_doc[did] >= FLAG_INDEX_PER_DOC or len(shown) >= FLAG_INDEX_TOTAL:
            hidden += 1
            continue
        per_doc[did] += 1
        shown.append(line)
    out: List[str] = []
    if confirmed:
        out += ["", "## Confirmed source errors", *confirmed]
    if shown:
        out += ["", "## Open flags (most severe first)", *shown]
        if hidden:
            out.append(f"- … and {hidden} more (lower severity, or beyond {FLAG_INDEX_PER_DOC} per file): "
                     "`pd.py review <pile>`")
    return out


def _terms(pile: Pile, pages: List[dict], res: Dict[str, dict]) -> List[str]:
    topics = Counter()
    notation = Counter()
    for p in pages:
        r = res.get(p["page_id"]) or {}
        if not r:
            continue
        if p["index"] > 1 and p["cls"] != "title" and p["cls"] not in SKIP_CLASSES:
            for t in r.get("topics") or []:
                t = t.strip().lower()
                if len(t) > 2 and t not in GENERIC_TOPICS:
                    topics[t] += 1
        if p["cls"] in VISION_CLASSES:
            seen = set()
            for m in _INLINE_MATH.finditer(pile.result_text(p["page_id"])):
                snip = re.sub(r"\s+", "", m.group(1) or m.group(2) or "")
                snip = snip.replace("\\geq", "\\ge").replace("\\leq", "\\le")
                letters = re.sub(r"\\[A-Za-z]+", "", snip)
                if not re.search(r"[A-Za-z]", letters) or re.fullmatch(r"\W*[A-Za-z]\W*", letters) \
                        or re.search(r"[A-Za-z]{4,}", letters):
                    continue  # bare numbers, lone macros, single letters
                if 2 <= len(snip) <= 15 and snip not in seen:
                    seen.add(snip)
                    notation[snip] += 1
    out: List[str] = []
    top = [(t, n) for t, n in topics.most_common(50) if n >= 2]
    if top:
        out += ["", "## Frequent topics", ", ".join(f"{t} ({n})" for t, n in top)]
    common = [(t, n) for t, n in notation.most_common(40) if n >= 3]
    if common:
        out += ["", "## Notation (most frequent symbols in the transcriptions)", ", ".join(f"`{t}` ({n})" for t, n in common)]
    return out


def build(pile: Pile, ctx=None, *, with_sections: bool = True) -> Path:
    labels = doc_labels(pile)
    idx = pile.index()
    pages = pile.pages()
    res = pile.results()
    sections = section_digests(pile, ctx, labels) if with_sections else {}
    cls = Counter(p["cls"] for p in pages)
    status = Counter((res.get(p["page_id"]) or {}).get("status", "unread") for p in pages if p["cls"] not in SKIP_CLASSES)
    by_doc: Dict[str, List[dict]] = {}
    for p in pages:
        by_doc.setdefault(p["doc_id"], []).append(p)
    sec_by_doc: Dict[str, List[dict]] = {}
    for _sid, s in sorted(sections.items()):
        sec_by_doc.setdefault(s["doc_id"], []).append(s)
    docs = [d for d in idx.get("docs", [])]
    doc_lines = {d["doc_id"]: _doc_lines(by_doc[d["doc_id"]], res, {s["first"]: s for s in sec_by_doc.get(d["doc_id"], [])},
                                         labels)
                 for d in docs if by_doc.get(d["doc_id"])}

    H: List[str] = [f"# Digest: {pile.name}", ""]
    H.append(f"Built {now_iso()}. {len(docs)} files, {len(pages)} pages: "
             f"{sum(cls[c] for c in SKIP_CLASSES)} skipped (blank/duplicate/repeated title), "
             f"{cls['text-ok'] + cls['title']} from text layers, {cls['vision'] + cls['hybrid']} read from images.")
    if status.get("unread") or status.get("failed") or status.get("needs-main"):
        H.append(f"Not finished: {status.get('unread', 0)} pages not read yet, {status.get('failed', 0)} failed, "
                 f"{status.get('needs-main', 0)} waiting for main-model review.")
    H += ["", "Page references are `D<n>:<page>`. Open a page with `pd.py page <pile> D<n>:<page>` (text) or "
              "`pd.py page <pile> D<n>:<page> --image` (renders the page image).", "", "## Files"]
    for d in docs:
        lab = labels[d["doc_id"]]
        extra = (f" — {d['n_pages']} page" + ("s" if d["n_pages"] != 1 else "")) if d.get("n_pages") else ""
        title = f" — “{d['title']}”" if d.get("title") else ""
        st = "" if d["status"] == "ok" else f" — **{d['status']}**: {d.get('note', '')}"
        al = f" (same file also at: {', '.join(d['aliases'])})" if d.get("aliases") else ""
        H.append(f"- {lab} `{d['relpath']}`{extra}{title}{st}{al}")

    tail = _flags_and_errors(pages, res, labels)
    rel = related_pages(pile, pages, res, labels)
    if rel:
        tail += ["", "## Related pages across files",
                 "Pages in different files that share many numbers and symbols: probably the same example or result. "
                 "Compare them; disagreements often point to errors."]
        tail += [f"- {a} ↔ {b}" for a, b, _n, _j in rel]
    tail += _terms(pile, pages, res)

    full: List[str] = list(H)
    for d in docs:
        if d["doc_id"] in doc_lines:
            full += ["", f"## {labels[d['doc_id']]} · {d['relpath']}"] + _outline(d) + doc_lines[d["doc_id"]]
    text = "\n".join(full + tail).rstrip() + "\n"

    pages_dir = pile.dir / "digest"
    shutil.rmtree(pages_dir, ignore_errors=True)
    split = len(text) // 4 > SPLIT_TOKENS
    if split:
        pages_dir.mkdir(parents=True, exist_ok=True)
        ov: List[str] = list(H)
        ov += ["", "This pile is large: the per-page lines are in one file per document. Read the ones you need."]
        for d in docs:
            if d["doc_id"] not in doc_lines:
                continue
            lab = labels[d["doc_id"]]
            f = pages_dir / f"{lab}.md"
            atomic_write_text(f, "\n".join([f"# {lab} · {d['relpath']}", *_outline(d), *doc_lines[d["doc_id"]]]).rstrip()
                              + "\n")
            ov += ["", f"## {lab} · {d['relpath']}", f"Pages: `{f}`"] + _outline(d)
            for s in sec_by_doc.get(d["doc_id"], []):
                summ = clip(s["summary"], 600) if s.get("summary") else "*(no section summary)*"
                ov.append(f"- **{s['title']}** (p{s['first']}–p{s['last']}): {summ}")
        text = "\n".join(ov + tail).rstrip() + "\n"
    atomic_write_text(pile.digest_path, text)
    stale = sum(1 for s in sections.values() if s.get("stale"))
    pile.set_state(digest_built=now_iso(), digest_key=sha256_text(text)[:20],
                   digest_info={"tokens": len(text) // 4, "split": split,
                                "pages_dir": str(pages_dir) if split else None, "stale_sections": stale})
    return pile.digest_path


def info(pile: Pile) -> dict:
    return dict(pile.state().get("digest_info") or {"tokens": 0, "split": False, "pages_dir": None, "stale_sections": 0})
