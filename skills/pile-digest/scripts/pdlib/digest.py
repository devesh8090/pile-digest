"""Build the compact digest the main model reads instead of the raw pile."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import parse, worker
from .pile import Pile, load_prompt
from .triage import SKIP_CLASSES, VISION_CLASSES
from .util import atomic_write_text, now_iso, read_json, sha256_text, write_json

LATEX_SKIP = {
    "frac", "left", "right", "mathrm", "text", "textbf", "textit", "mathbf", "begin", "end", "quad", "qquad",
    "cdot", "label", "tag", "displaystyle", "big", "Big", "bigg", "Bigg", "limits", "mathit", "operatorname",
    "hline", "item", "emph", "underline", "overline", "hat", "bar", "vec", "dot", "ddot", "tilde", "mathcal",
    "mathbb", "mathsf", "boldsymbol", "boxed", "ldots", "cdots", "dots", "vdots", "ddots", "\\", "rm", "it",
    "bf", "sqrt", "sum", "int", "prod", "lim", "times", "approx", "equiv", "le", "ge", "leq", "geq", "neq",
    "to", "in", "infty", "pm", "mp", "hspace", "vspace", "newline", "noindent", "small", "large",
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


def _flag_text(r: dict) -> str:
    kinds = Counter(f.get("kind") for f in (r.get("flags") or []) + (r.get("markers") or []) if f.get("kind"))
    for k in ("figure",):
        kinds.pop(k, None)
    parts = [f"{k}×{n}" if n > 1 else k for k, n in kinds.most_common()]
    if r.get("status") == "needs-main":
        parts.append("needs review")
    if r.get("status") == "failed":
        parts.append("unread")
    return (" ⚑ " + ", ".join(parts)) if parts else ""


def section_digests(pile: Pile, ctx, labels: Dict[str, str]) -> Dict[str, dict]:
    """Ask the stronger model for short section digests (cached by content)."""
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
    for did, dpages in by_doc.items():
        if len([p for p in dpages if p["cls"] not in SKIP_CLASSES]) < 15:
            continue
        for k, (title, grp) in enumerate(sections_for(docs.get(did, {}), dpages, int(cfg["section_chunk_pages"])), 1):
            lines = []
            for p in grp:
                r = res.get(p["page_id"]) or {}
                if r.get("summary"):
                    lines.append(f"p{p['index']}: {r['summary']}{_flag_text(r)}")
            if not lines:
                continue
            sid = f"{did}-s{k:03d}"
            key = sha256_text(title + "\n".join(lines))[:20]
            cached = read_json(sdir / f"{sid}.json", None)
            if cached and cached.get("key") == key:
                out[sid] = cached
                continue
            if ctx is None or ctx.guard(len("\n".join(lines)) // 4 + 600):
                continue
            msg = (f"Section id: {sid}\nDocument: {docs.get(did, {}).get('relpath', did)} ({labels.get(did, '')})\n"
                   f"Section: {title}\n\n" + "\n".join(lines))
            model = cfg["models"]["escalate"]
            wr = worker.call(model, prompt, [worker.text_block(msg)], timeout=float(cfg["call_timeout_s"]))
            ctx.account(wr, stage="sections", model=model, pages=[{"cls": "section"}])
            if not wr.ok:
                if wr.fatal:
                    break
                continue
            parsed, problems = parse.parse_blocks(wr.text, [sid], mode="summarize")
            if sid not in parsed:
                continue
            data = {"key": key, "title": title, "doc_id": did, "first": grp[0]["index"], "last": grp[-1]["index"],
                    "summary": parsed[sid]["summary"], "topics": parsed[sid].get("topics", []),
                    "flags": parsed[sid].get("flags", []), "at": now_iso()}
            write_json(sdir / f"{sid}.json", data)
            out[sid] = data
    return out


def build(pile: Pile, ctx=None, *, with_sections: bool = True) -> Path:
    labels = doc_labels(pile)
    idx = pile.index()
    pages = pile.pages()
    res = pile.results()
    sections = section_digests(pile, ctx, labels) if with_sections else {}
    cls = Counter(p["cls"] for p in pages)
    status = Counter((res.get(p["page_id"]) or {}).get("status", "unread") for p in pages if p["cls"] not in SKIP_CLASSES)
    flags = Counter()
    for p in pages:
        r = res.get(p["page_id"]) or {}
        for f in (r.get("flags") or []) + (r.get("markers") or []):
            flags[f.get("kind")] += 1
    L: List[str] = []
    L.append(f"# Digest: {pile.name}")
    L.append("")
    L.append(f"Built {now_iso()}. {len(idx.get('docs', []))} files, {len(pages)} pages: "
             f"{sum(cls[c] for c in SKIP_CLASSES)} skipped (blank/duplicate/repeated title), "
             f"{cls['text-ok'] + cls['title']} from text layers, {cls['vision'] + cls['hybrid']} read from images.")
    if status.get("unread") or status.get("failed") or status.get("needs-main"):
        L.append(f"Not finished: {status.get('unread', 0)} pages not read yet, {status.get('failed', 0)} failed, "
                 f"{status.get('needs-main', 0)} waiting for main-model review.")
    if flags:
        L.append("Flags: " + ", ".join(f"{k} {n}" for k, n in flags.most_common() if k))
    L.append("")
    L.append("Page references are `D<n>:<page>`. Open a page with `pd.py page <pile> D<n>:<page>` (text) or "
             "`pd.py page <pile> D<n>:<page> --image` (renders the page image).")
    L.append("")
    L.append("## Files")
    for d in idx.get("docs", []):
        lab = labels[d["doc_id"]]
        extra = (f" — {d['n_pages']} page" + ("s" if d["n_pages"] != 1 else "")) if d.get("n_pages") else ""
        title = f" — “{d['title']}”" if d.get("title") else ""
        st = "" if d["status"] == "ok" else f" — **{d['status']}**: {d.get('note', '')}"
        al = f" (same file also at: {', '.join(d['aliases'])})" if d.get("aliases") else ""
        L.append(f"- {lab} `{d['relpath']}`{extra}{title}{st}{al}")
    by_doc: Dict[str, List[dict]] = {}
    for p in pages:
        by_doc.setdefault(p["doc_id"], []).append(p)
    sec_by_doc: Dict[str, List[dict]] = {}
    for _sid, s in sorted(sections.items()):
        sec_by_doc.setdefault(s["doc_id"], []).append(s)
    for d in idx.get("docs", []):
        dpages = by_doc.get(d["doc_id"])
        if not dpages:
            continue
        lab = labels[d["doc_id"]]
        L.append("")
        L.append(f"## {lab} · {d['relpath']}")
        if d.get("outline"):
            items = [f"{o['title']} (p{o['page']})" for o in d["outline"] if o.get("level", 1) <= 2 and o.get("page")]
            if items:
                L.append("Outline: " + " · ".join(items[:60]) + (" · …" if len(items) > 60 else ""))
        secs = {s["first"]: s for s in sec_by_doc.get(d["doc_id"], [])}
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
                L.append(s["summary"])
                if s.get("topics"):
                    L.append("Key terms: " + ", ".join(s["topics"]))
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
            if len(summ) > 260:
                summ = summ[:257] + "…"
            tag = " [img]" if p["cls"] in VISION_CLASSES else ""
            tag += " [title]" if p["cls"] == "title" else ""
            L.append(f"- p{p['index']}{tag} {summ}{_flag_text(r)}")
        flush()
    # Flag index.
    fl = []
    for p in pages:
        r = res.get(p["page_id"]) or {}
        for f in (r.get("flags") or []) + (r.get("markers") or []):
            if f.get("kind") in ("illegible", "source-suspect", "cut-off", "unsure") and f.get("note"):
                fl.append(f"- {labels.get(p['doc_id'])}:{p['index']} {f['kind']}: {f['note'][:160]}")
    if fl:
        L.append("")
        L.append("## Flag index")
        L.extend(fl[:400])
        if len(fl) > 400:
            L.append(f"- … and {len(fl) - 400} more (see `pd.py review <pile>`)")
    # Frequent terms and notation.
    topics = Counter()
    notation = Counter()
    for p in pages:
        r = res.get(p["page_id"]) or {}
        for t in r.get("topics") or []:
            topics[t.lower()] += 1
        if p["cls"] in VISION_CLASSES and r:
            body = pile.result_text(p["page_id"])
            for cmd in re.findall(r"\\([A-Za-z]+)", body):
                if cmd not in LATEX_SKIP:
                    notation["\\" + cmd] += 1
    if topics:
        L.append("")
        L.append("## Frequent topics")
        L.append(", ".join(f"{t} ({n})" for t, n in topics.most_common(60)))
    if notation:
        L.append("")
        L.append("## Notation seen in transcriptions")
        L.append(", ".join(f"`{t}` ({n})" for t, n in notation.most_common(40)))
    text = "\n".join(L).rstrip() + "\n"
    atomic_write_text(pile.digest_path, text)
    pile.set_state(digest_built=now_iso(), digest_key=sha256_text(text)[:20])
    return pile.digest_path


def doc_fingerprints(pile: Pile) -> Dict[str, str]:
    """A hash per document over its pages' results, used to spot stale outputs."""
    res = pile.results()
    fp: Dict[str, List[str]] = {}
    for p in pile.pages():
        r = res.get(p["page_id"]) or {}
        fp.setdefault(p["doc_id"], []).append(f"{p['page_id']}:{p['cls']}:{r.get('level')}:{r.get('updated')}")
    return {k: sha256_text("|".join(v))[:16] for k, v in fp.items()}
