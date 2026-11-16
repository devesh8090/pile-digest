"""Local, free page triage: what each page is and whether a model must look at it.

Classes:
  blank           nothing on it: skipped
  duplicate       same as an earlier page: skipped, points at the original
  repeated-title  a short title page seen before: skipped
  title           first occurrence of a short title page: kept as a section marker, free
  text-ok         the text layer is good: no vision needed, only a cheap summary
  hybrid          text layer present, plus images/drawings/formulas: vision with the text as a hint
  vision          no usable text layer: vision needed
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional

from . import raster
import re

from .textutil import content_tokens, math_density, normalize_page_text, quality
from .util import sha256_bytes, sha256_text

SKIP_CLASSES = {"blank", "duplicate", "repeated-title"}
FREE_CLASSES = {"title", "text-ok"}
CLOSING_RE = re.compile(r"^\W*(thank\s*you|thanks|any\s+questions|questions|q\s*&\s*a|the\s+end|end)\W*$", re.I)
VISION_CLASSES = {"hybrid", "vision"}


def page_id(doc_id: str, index: int) -> str:
    return f"{doc_id}-p{index:04d}"


def _lowres_for_unit(u: dict, cfg: dict, tmp: Path, cache: dict) -> Optional[raster.Gray]:
    r = u.get("render")
    if not r:
        return None
    if r["type"] == "pdf":
        key = (r["path"], r["page"])
        if key not in cache:
            got = raster.pdf_lowres(Path(r["path"]), int(cfg["triage_dpi"]), tmp, r["page"], r["page"])
            cache[key] = got.get(r["page"])
        return cache[key]
    key = (r["path"], 0)
    if key not in cache:
        cache[key] = raster.image_lowres(Path(r["path"]))
    return cache[key]


def triage_doc(doc: dict, cfg: dict, tmp: Path) -> List[dict]:
    """Per-page features and a first class (before cross-page duplicate checks)."""
    units = doc.get("units", [])
    pages: List[dict] = []
    lowres: Dict[int, raster.Gray] = {}
    nontext: Dict[int, raster.Gray] = {}
    pdf_path = None
    if units and units[0].get("render", {}) and (units[0].get("render") or {}).get("type") == "pdf":
        pdf_path = Path(units[0]["render"]["path"])
        lowres = raster.pdf_lowres(pdf_path, int(cfg["triage_dpi"]), tmp)
        wants_nt = any(
            (u.get("text") or "").strip() and u.get("image_cov", 0) < 0.12 for u in units
        )
        if wants_nt:
            nontext = raster.pdf_lowres(pdf_path, int(cfg["triage_dpi"]), tmp, no_text=True)
    for i, u in enumerate(units, 1):
        text = u.get("text") or ""
        rec = {
            "page_id": page_id(doc["doc_id"], i),
            "doc_id": doc["doc_id"],
            "index": i,
            "text": text,
            "text_source": u.get("text_source", "none"),
            "render": u.get("render"),
            "chars": len(text.strip()),
            "text_quality": quality(text),
            "math": math_density(text),
        }
        for k in ("w_pt", "h_pt", "w_px", "h_px", "image_cov", "source_ppi", "note"):
            if k in u:
                rec[k] = u[k]
        g = None
        if u.get("render"):
            if u["render"]["type"] == "pdf":
                g = lowres.get(u["render"]["page"])
            else:
                g = raster.image_lowres(Path(u["render"]["path"]))
        if g is not None:
            st = raster.stats(g)
            rec["ink"] = st["ink"]
            rec["bg"] = st["bg"]
            rec["dhash"] = st["dhash"]
            rec["raster_sha"] = sha256_bytes(g.data)[:24]
            rec["lr_w"], rec["lr_h"] = g.w, g.h
            if "w_px" not in rec and "w_pt" not in rec:
                rec["w_px"], rec["h_px"] = g.w * 10, g.h * 10
        if u.get("render") and u["render"]["type"] == "pdf" and u["render"]["page"] in nontext:
            rec["nontext_ink"] = raster.stats(nontext[u["render"]["page"]])["ink"]
        rec["text_sha"] = sha256_text(normalize_page_text(text))[:24] if text.strip() else None
        if i > len(units) - 2 and len(units) > 2 and rec["chars"] <= 40 and CLOSING_RE.match(normalize_page_text(text)):
            rec["closing"] = True
        rec["cls"], rec["why"] = classify(rec, cfg)
        w, h = page_dims(rec)
        if w and h and h / w > float(cfg.get("tall_page_ratio", 2.2)):
            rec["tall"] = True
        if rec.get("source_ppi") and rec["source_ppi"] < 110:
            rec["lowres_source"] = True
        pages.append(rec)
    return pages


def page_dims(rec: dict):
    if rec.get("w_pt"):
        return rec["w_pt"], rec["h_pt"]
    if rec.get("w_px"):
        return rec["w_px"], rec["h_px"]
    return None, None


def classify(rec: dict, cfg: dict):
    text = rec["text"].strip()
    chars = rec["chars"]
    if not rec.get("render"):
        return ("text-ok", "text file") if text else ("blank", "empty text")
    if rec.get("closing"):
        return "title", "closing slide"
    ink = rec.get("ink")
    if ink is not None:
        tiny_text = chars <= 3 or (chars <= 6 and text.replace(" ", "").isdigit())
        if tiny_text and ink < 0.0025:
            return "blank", f"no ink ({ink:.4f})"
        if tiny_text and ink < 0.006 and rec.get("image_cov", 0) < 0.02:
            return "blank", f"only a page number or speck ({ink:.4f})"
    if rec["render"]["type"] == "image":
        return "vision", "image file"
    tq = rec["text_quality"]
    cov = rec.get("image_cov", 0.0)
    if chars >= 40 and tq >= 0.65:
        nt = rec.get("nontext_ink")
        if cov >= 0.12:
            return "hybrid", f"text layer plus images ({cov:.0%} of page)"
        if nt is not None and nt > 0.015:
            return "hybrid", f"text layer plus drawings ({nt:.3f} non-text ink)"
        if rec["math"] >= 0.12 and cfg.get("math_pages_use_vision", True):
            return "hybrid", f"formula-heavy text ({rec['math']:.2f})"
        return "text-ok", f"good text layer (quality {tq:.2f})"
    if chars >= 40:
        return "vision", f"text layer unusable (quality {tq:.2f})"
    if chars > 0 and tq >= 0.65 and cov < 0.05:
        nt = rec.get("nontext_ink")
        quiet = (nt is not None and nt < 0.005) or (nt is None and ink is not None and ink < chars * 1e-4 + 0.003)
        if quiet:
            return "text-ok", "short text page with nothing else on it"
    if chars > 0:
        return "vision", "little text, rest must be read from the image"
    return "vision", "no text layer"


def cross_page(pages: List[dict], cfg: dict, tmp: Path) -> None:
    """Pile-wide duplicate and repeated-title detection (pages in reading order)."""
    cache: dict = {}
    by_raster: Dict[str, str] = {}
    by_text: Dict[str, str] = {}
    hashes: List[dict] = []
    _mark_builds(pages)
    for p in pages:
        if p.get("cls") == "blank":
            continue
        if p.get("pinned") == "hand":
            # A class set by hand stays, but the page still counts as the original
            # of later copies, so duplicate decisions don't change between runs.
            _register(p, by_raster, by_text, hashes)
            continue
        p.pop("dup_of", None)
        if p["cls"] in ("duplicate", "repeated-title", "title"):
            p["cls"], p["why"] = classify(p, cfg)
        # Exact duplicate: identical low-res raster and identical text.
        key = None
        if p.get("build"):
            _register(p, by_raster, by_text, hashes)
            continue
        if p.get("raster_sha"):
            key = p["raster_sha"] + ":" + (p.get("text_sha") or "")
            if key in by_raster:
                p["cls"], p["dup_of"], p["why"] = "duplicate", by_raster[key], "identical page"
                continue
        elif p.get("text_sha") and p["chars"] >= 50:
            if p["text_sha"] in by_text:
                p["cls"], p["dup_of"], p["why"] = "duplicate", by_text[p["text_sha"]], "identical text"
                continue
        # Near duplicate: close perceptual hash, then a strict pixel comparison.
        if p.get("dhash"):
            for q in hashes:
                if raster.hamming(p["dhash"], q["dhash"]) > 5:
                    continue
                if (p.get("lr_w"), p.get("lr_h")) != (q.get("lr_w"), q.get("lr_h")):
                    continue
                if (p.get("text_sha") or "") != (q.get("text_sha") or ""):
                    continue  # different text (page numbers aside) is different content, however small
                ga = _lowres_for_unit(p, cfg, tmp, cache)
                gb = _lowres_for_unit(q, cfg, tmp, cache)
                if ga is None or gb is None:
                    continue
                mm = raster.pixel_mismatch(ga, gb, p.get("bg", 255), q.get("bg", 255))
                if mm < 0.03:
                    p["cls"], p["dup_of"], p["why"] = "duplicate", q["page_id"], f"near-identical page ({mm:.1%} differs)"
                    break
            if p.get("cls") == "duplicate":
                continue
            hashes.append(p)
        if key:
            by_raster[key] = p["page_id"]
        if p.get("text_sha") and p["chars"] >= 50:
            by_text.setdefault(p["text_sha"], p["page_id"])
    # Repeated short title pages.
    groups: Dict[str, List[dict]] = defaultdict(list)
    for p in pages:
        if p["cls"] in ("text-ok", "duplicate") and p.get("render") and 3 < p["chars"] < 160 and not p.get("build") \
                and p.get("image_cov", 0) < 0.15 and (p.get("nontext_ink") or 0) < 0.015 and not p.get("pinned"):
            groups[normalize_page_text(p["text"])].append(p)
    for norm, group in groups.items():
        if len(group) >= 3 and norm:
            first = next((q for q in group if q["cls"] == "text-ok"), None)
            if first is None:
                continue
            first["cls"], first["why"] = "title", f"title page (repeats {len(group)} times)"
            for q in group:
                if q is not first and q["cls"] == "text-ok":
                    q["cls"], q["dup_of"], q["why"] = "repeated-title", first["page_id"], "repeated title page"


def _register(p: dict, by_raster: dict, by_text: dict, hashes: list) -> None:
    if p.get("raster_sha"):
        by_raster.setdefault(p["raster_sha"] + ":" + (p.get("text_sha") or ""), p["page_id"])
    if p.get("text_sha") and p["chars"] >= 50:
        by_text.setdefault(p["text_sha"], p["page_id"])
    if p.get("dhash"):
        hashes.append(p)


def _mark_builds(pages: List[dict]) -> None:
    """Progressive build-up slides: each page adds to the one before (a table
    filled in step by step). They differ in content, so none is a repeat."""
    for p in pages:
        p.pop("build", None)
    prev = None
    for p in pages:
        if prev is not None and prev["doc_id"] == p["doc_id"] and p["index"] == prev["index"] + 1:
            a, b = prev.get("_tok"), None
            if a is None:
                a = prev["_tok"] = content_tokens(prev.get("text", ""))
            b = p["_tok"] = content_tokens(p.get("text", ""))
            if a and a < b:
                prev["build"] = p["build"] = True
        else:
            p["_tok"] = content_tokens(p.get("text", ""))
        prev = p
    for p in pages:
        p.pop("_tok", None)


def counts(pages: List[dict]) -> dict:
    c: Dict[str, int] = defaultdict(int)
    for p in pages:
        c[p["cls"]] += 1
    return dict(c)
