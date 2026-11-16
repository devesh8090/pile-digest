"""Text helpers: decoding, HTML/RTF stripping, chunking, quality scores."""

from __future__ import annotations

import html
import re
import unicodedata
from html.parser import HTMLParser
from typing import List

MATH_CHARS = set(
    "∑∏∫∮∂∇√∞≈≠≡≤≥±∓×÷→←↔⇒⇔∈∉⊂⊃⊆⊇∪∩∀∃∅ℝℕℤℚℂ"
    "αβγδεζηθικλμνξοπρστυφχψωΓΔΘΛΞΠΣΦΨΩϕϵϑ"
    "′″‴⁰¹²³⁴⁵⁶⁷⁸⁹₀₁₂₃₄₅₆₇₈₉⟨⟩‖"
)


def decode_bytes(data: bytes) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", "replace")
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", "replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    for enc in ("cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


class _Stripper(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article",
             "blockquote", "pre", "table", "ul", "ol", "dt", "dd", "hr", "header", "footer"}
    SKIP = {"script", "style", "noscript", "template", "head"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: List[str] = []
        self.skip = 0
        self.images: List[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.out.append("\n")
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6") and not self.skip:
            self.out.append("#" * int(tag[1]) + " ")
        if tag == "li" and not self.skip:
            self.out.append("- ")
        if tag == "img":
            src = dict(attrs).get("src") or ""
            if src:
                self.images.append(src)

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def html_to_text(text: str) -> str:
    p = _Stripper()
    try:
        p.feed(text)
        p.close()
    except Exception:  # noqa: BLE001 - malformed html should still yield text
        return re.sub(r"<[^>]+>", " ", html.unescape(text))
    return tidy("".join(p.out))


def html_images(text: str) -> List[str]:
    p = _Stripper()
    try:
        p.feed(text)
    except Exception:  # noqa: BLE001
        pass
    return p.images


def rtf_to_text(rtf: str) -> str:
    """A tolerant RTF stripper (good enough for reading, not for layout)."""
    out = []
    i = 0
    depth = 0
    skip_depth = None
    n = len(rtf)
    destinations = {"fonttbl", "colortbl", "stylesheet", "info", "pict", "object", "header", "footer",
                    "headerl", "headerr", "footerl", "footerr", "listtable", "listoverridetable", "rsidtbl",
                    "generator", "xmlnstbl", "themedata", "colorschememapping", "latentstyles", "datastore"}
    while i < n:
        c = rtf[i]
        if c == "{":
            depth += 1
            i += 1
            continue
        if c == "}":
            if skip_depth is not None and depth == skip_depth:
                skip_depth = None
            depth -= 1
            i += 1
            continue
        if c == "\\":
            m = re.match(r"\\([a-zA-Z]+)(-?\d+)? ?|\\'([0-9a-fA-F]{2})|\\(.)", rtf[i:])
            if not m:
                i += 1
                continue
            word, num, hexc, sym = m.groups()
            i += m.end()
            if skip_depth is not None:
                continue
            if hexc:
                out.append(bytes([int(hexc, 16)]).decode("cp1252", "replace"))
            elif sym:
                if sym == "*":
                    skip_depth = depth
                elif sym in "\\{}":
                    out.append(sym)
                elif sym == "~":
                    out.append(" ")
            elif word in destinations:
                skip_depth = depth
            elif word in ("par", "line", "row", "sect", "page"):
                out.append("\n")
            elif word == "tab" or word == "cell":
                out.append("\t")
            elif word == "u" and num:
                try:
                    out.append(chr(int(num) % 0x110000 if int(num) >= 0 else int(num) + 65536))
                except ValueError:
                    pass
                if i < n and rtf[i] == "?":
                    i += 1
            continue
        if skip_depth is None and c not in "\r\n":
            out.append(c)
        i += 1
    return tidy("".join(out))


def tidy(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def chunk_text(text: str, size: int) -> List[str]:
    """Split text into pieces of about `size` chars, preferring paragraph breaks."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    chunks = []
    start = 0
    n = len(text)
    while start < n:
        end = min(n, start + size)
        if end < n:
            window = text[start:end]
            cut = max(window.rfind("\n\n"), window.rfind("\n# "))
            if cut < size * 0.5:
                cut = window.rfind("\n")
            if cut < size * 0.5:
                cut = window.rfind(". ")
            if cut >= size * 0.3:
                end = start + cut + 1
        chunks.append(text[start:end].strip())
        start = end
    return [c for c in chunks if c]


def quality(text: str) -> float:
    """0..1: how much an extracted text layer looks like real, readable text."""
    t = text.strip()
    if not t:
        return 0.0
    if "(cid:" in t:
        return 0.1
    nonspace = [c for c in t if not c.isspace()]
    if not nonspace:
        return 0.0
    total = len(nonspace)
    letters = sum(1 for c in nonspace if c.isalpha())
    digits = sum(1 for c in nonspace if c.isdigit())
    bad = sum(1 for c in nonspace if c == "\ufffd" or unicodedata.category(c) in ("Co", "Cn", "Cc"))
    words = t.split()
    avg = sum(len(w) for w in words) / max(1, len(words))
    score = (letters + 0.5 * digits) / total
    if bad / total > 0.02:
        score *= 0.3
    if avg > 25 or avg < 1.3:
        score *= 0.5
    return round(max(0.0, min(1.0, score + 0.15)), 3)


def math_density(text: str) -> float:
    """Rough share of a page's text that is mathematical notation."""
    t = text.strip()
    if not t:
        return 0.0
    sym = sum(1 for c in t if c in MATH_CHARS)
    ops = len(re.findall(r"(?<![a-zA-Z])[a-zA-Z](?![a-zA-Z])\s*[=+\-/^_]", t))
    lines = [ln for ln in t.splitlines() if ln.strip()]
    short_sym_lines = sum(
        1 for ln in lines if len(ln.strip()) < 40 and sum(1 for c in ln if c in "=+-/^()[]{}|<>" or c in MATH_CHARS) >= 3
    )
    score = (sym * 3 + ops * 2 + short_sym_lines * 6) / max(60, len(t))
    return round(min(1.0, score), 3)


def normalize_for_compare(text: str) -> str:
    t = unicodedata.normalize("NFKC", text).lower()
    t = re.sub(r"\d+", "", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def first_line_summary(text: str, limit: int = 140) -> str:
    for ln in text.splitlines():
        s = ln.strip().lstrip("#").strip()
        if len(s) >= 3:
            return (s[: limit - 1] + "…") if len(s) > limit else s
    return ""


_PAGE_NO_LINE = re.compile(r"^\s*(?:page|p\.|slide|seite|página|page n°)?\s*\d{1,4}\s*(?:/\s*\d{1,4}|of\s+\d{1,4})?\s*$", re.I)


def normalize_page_text(text: str) -> str:
    """Normalise for duplicate checks: keeps digits (they carry content, e.g. a
    filled-in table on a build-up slide) but drops lines that are only a page or
    slide number."""
    t = unicodedata.normalize("NFKC", text).lower()
    lines = [ln for ln in t.splitlines() if ln.strip() and not _PAGE_NO_LINE.match(ln)]
    return re.sub(r"\s+", " ", " ".join(lines)).strip()


_TOKEN_RE = re.compile(r"[^\W_]+(?:[.,/][0-9]+)*", re.U)


def content_tokens(text: str) -> set:
    """Words (3+ letters) and numbers, lowercased: a cheap fingerprint of what a
    page says, used to compare readings of the same page."""
    t = unicodedata.normalize("NFKC", text or "").lower()
    out = set()
    for tok in _TOKEN_RE.findall(t):
        if tok.isdigit() or any(c.isdigit() for c in tok):
            out.add(tok)
        elif len(tok) >= 3:
            out.add(tok)
    return out


def overlap(a: set, b: set) -> float:
    """Share of the smaller set found in the larger one (0..1)."""
    if not a or not b:
        return 0.0
    small, big = (a, b) if len(a) <= len(b) else (b, a)
    return len(small & big) / len(small)


def clip(text: str, limit: int) -> str:
    """Shorten at a sentence end, else at a word boundary, never mid-word."""
    s = re.sub(r"\s+", " ", (text or "").strip())
    if len(s) <= limit:
        return s
    cut = s[:limit]
    ends = [m.end() for m in re.finditer(r"[.!?;](?=\s|$)", cut)]
    if ends and ends[-1] >= limit * 0.5:
        return cut[:ends[-1]].rstrip()
    sp = cut.rfind(" ")
    if sp >= limit * 0.5:
        cut = cut[:sp]
    return cut.rstrip(" ,;:-") + "…"
