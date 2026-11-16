"""Parse and validate the block format the worker models answer in.

Delimited blocks instead of JSON: LaTeX inside JSON strings is a constant
source of broken escapes for small models, while this format survives any
content.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from .textutil import clip

BLOCK_RE = re.compile(r"^=== PAGE (?P<id>[^\s=]+) ===[ \t]*$(?P<body>.*?)^=== END PAGE (?P=id) ===[ \t]*$",
                      re.M | re.S)
FIELD_RE = re.compile(r"^(KIND|CONFIDENCE|SUMMARY|TOPICS|VERDICT):[ \t]*(.*)$", re.M)
MARKER_RE = re.compile(r"\[\[(ILLEGIBLE|UNSURE|SOURCE-SUSPECT)(?::\s*(.*?))?\]\]", re.S)
KINDS = {"transcription", "figure", "blank", "mixed"}
CONF = {"high", "medium", "low"}
VERDICTS = {"match", "minor", "mismatch"}


class ParsedPage(dict):
    pass


def _section(body: str, name: str, stop: Tuple[str, ...]) -> Optional[str]:
    m = re.search(r"^%s:[ \t]*\n?(.*)" % re.escape(name), body, re.M | re.S)
    if not m:
        return None
    rest = m.group(1)
    cut = len(rest)
    for s in stop:
        mm = re.search(r"^%s:" % re.escape(s), rest, re.M)
        if mm:
            cut = min(cut, mm.start())
    return rest[:cut]


def parse_flags(text: Optional[str]) -> List[dict]:
    flags = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith(("-", "*")):
            continue
        item = line.lstrip("-* ").strip()
        if not item or item.lower() in ("none", "(none)", "n/a", "no flags"):
            continue
        kind, _, note = item.partition(":")
        kind = kind.strip().lower().replace(" ", "-")
        if kind in ("typos", "spelling", "spelling-error"):
            kind = "typo"
        if kind not in ("illegible", "unsure", "source-suspect", "typo", "figure", "cut-off", "low-resolution",
                        "rotated", "language", "other", "difference", "garbled"):
            note = item
            kind = "other"
        flags.append({"kind": kind, "note": note.strip()[:400]})
    return flags


def parse_blocks(text: str, expected_ids: List[str], *, mode: str = "transcribe") -> Tuple[Dict[str, ParsedPage], List[str]]:
    """Return ({page_id: parsed}, [problems]). Pages missing or malformed are reported."""
    out: Dict[str, ParsedPage] = {}
    problems: List[str] = []
    found = {m.group("id"): m.group("body") for m in BLOCK_RE.finditer(text or "")}
    for pid in expected_ids:
        body = found.get(pid)
        if body is None:
            problems.append(f"{pid}: block missing")
            continue
        cut = re.search(r"^CONTENT:", body, re.M)
        header = body[: cut.start()] if cut else body
        fields = {}
        for k, v in FIELD_RE.findall(header):
            fields.setdefault(k, v.strip())
        p = ParsedPage(page_id=pid)
        if mode == "spotcheck":
            v = fields.get("VERDICT", "").lower().split()[0] if fields.get("VERDICT") else ""
            if v not in VERDICTS:
                problems.append(f"{pid}: bad VERDICT")
                continue
            p["verdict"] = v
            p["differences"] = parse_flags(_section(body, "DIFFERENCES", ("CORRECTED",)))
            corrected = _section(body, "CORRECTED", ())
            p["corrected"] = corrected.strip() if corrected and corrected.strip().lower() not in ("none", "(none)") else None
            out[pid] = p
            continue
        kind = (fields.get("KIND") or "").lower().split()[0] if fields.get("KIND") else ("transcription" if mode == "transcribe" else "text")
        conf = (fields.get("CONFIDENCE") or "").lower().split()[0] if fields.get("CONFIDENCE") else ""
        if mode == "transcribe" and kind not in KINDS:
            problems.append(f"{pid}: bad KIND '{fields.get('KIND')}'")
            continue
        if conf not in CONF:
            conf = "low" if mode == "transcribe" else "medium"
        summary = fields.get("SUMMARY", "").strip()
        if not summary and kind != "blank":
            problems.append(f"{pid}: empty SUMMARY")
            continue
        content = _section(body, "CONTENT", ()) if mode == "transcribe" else None
        if mode == "transcribe" and content is None and kind != "blank":
            problems.append(f"{pid}: CONTENT missing")
            continue
        flags = parse_flags(_section(body, "FLAGS", ("CONTENT",)))
        content = (content or "").strip("\n")
        markers = []
        for mk in MARKER_RE.finditer(content):
            markers.append({"kind": mk.group(1).lower(), "note": (mk.group(2) or "").strip()[:200]})
        p.update(kind=kind, confidence=conf, summary=clip(summary, 900),
                 topics=[t.strip() for t in re.split(r"[;,]", fields.get("TOPICS", "")) if t.strip()][:12],
                 flags=flags, markers=markers, content=content)
        out[pid] = p
    extra = set(found) - set(expected_ids)
    if extra:
        problems.append(f"unexpected blocks: {sorted(extra)[:5]}")
    return out, problems


NO_IMAGE_RE = re.compile(r"(image|picture|page|scan)s?\b[^.]{0,40}\b(did not|didn't|was not|wasn't|were not|not)\s+"
                         r"(arrive|received|attached|included|provided|visible|shown|available)|no image (data|was|is|attached)"
                         r"|only the (page )?label", re.I)


def no_image_reply(p: dict) -> bool:
    """The model says it got no image (it sometimes claims this on dark scans).
    Treated as a failed call and retried at the same level, not escalated."""
    text = " ".join([p.get("summary", "")] + [f.get("note", "") for f in p.get("flags", [])])
    return bool(NO_IMAGE_RE.search(text)) and len((p.get("content") or "").strip()) < 200


CLAIM_KINDS = ("source-suspect", "illegible", "unsure", "cut-off", "typo")


def _norm_note(s: str) -> str:
    return re.sub(r"[^\w]+", " ", (s or "").lower()).strip()


def claims(r: dict) -> List[dict]:
    """One list of a result's flags and inline markers, without the duplicates
    that arise when a model both flags a problem and marks it inline."""
    out: List[dict] = []
    seen: List[Tuple[str, set]] = []
    for f in list(r.get("flags") or []) + list(r.get("markers") or []):
        kind = f.get("kind")
        if not kind:
            continue
        note = (f.get("note") or "").strip()
        words = set(_norm_note(note).split())
        dup = False
        for k, w in seen:
            if k != kind:
                continue
            if not words and not w:
                dup = True
            elif words and w and len(words & w) / max(1, min(len(words), len(w))) >= 0.6:
                dup = True
            if dup:
                break
        if dup:
            continue
        seen.append((kind, words))
        out.append({"kind": kind, "note": note})
    return out


def needs_escalation(p: dict) -> Optional[str]:
    """Why a cheap transcription should go up the ladder (None if it is fine)."""
    if p.get("kind") == "blank":
        return None
    kinds = {m["kind"] for m in p.get("markers", [])} | {f["kind"] for f in p.get("flags", [])}
    if "illegible" in kinds:
        return "illegible parts"
    if p.get("confidence") == "low":
        return "low confidence"
    if "cut-off" in kinds or "low-resolution" in kinds:
        return "cut off or low resolution"
    unsure = sum(1 for m in p.get("markers", []) if m["kind"] == "unsure")
    if unsure >= 3:
        return f"{unsure} unsure spots"
    return None


def render_result_md(page: dict, parsed: dict, model: str, level: str) -> str:
    """The stored per-page file: a small header, then the content."""
    lines = [
        f"<!-- page {page['page_id']} | model {model} | level {level} | kind {parsed.get('kind')} | "
        f"confidence {parsed.get('confidence')} -->",
        f"**Summary:** {parsed.get('summary', '')}",
    ]
    if parsed.get("topics"):
        lines.append(f"**Topics:** {', '.join(parsed['topics'])}")
    flags = claims(parsed)
    if flags:
        lines.append("**Flags:** " + "; ".join(f"{f['kind']}: {f['note']}" if f.get("note") else f["kind"] for f in flags))
    lines.append("")
    lines.append(parsed.get("content", ""))
    return "\n".join(lines).rstrip() + "\n"
