"""Work out what a file really is, from its bytes first and its name second."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".rst", ".org", ".tex", ".bib", ".csv", ".tsv", ".log", ".ini", ".cfg",
    ".conf", ".yaml", ".yml", ".toml", ".json", ".xml", ".py", ".r", ".m", ".jl", ".c", ".h", ".cpp",
    ".hpp", ".cc", ".java", ".js", ".ts", ".go", ".rs", ".rb", ".php", ".sh", ".bat", ".ps1", ".sql",
    ".lua", ".pl", ".kt", ".swift", ".scala", ".hs", ".ml", ".f90", ".f", ".asm", ".s", ".vb", ".cs",
    ".sty", ".cls", ".srt", ".vtt", ".adoc", ".textile", ".wiki", ".mediawiki",
}
HTML_EXTS = {".html", ".htm", ".xhtml", ".mht", ".mhtml"}
IMAGE_KINDS = {"png", "jpeg", "gif", "webp", "tiff", "bmp", "heif", "avif", "jp2", "ico", "pnm", "svg"}
LEGACY_OFFICE = {".doc": "doc", ".xls": "xls", ".ppt": "ppt", ".msg": "msg", ".pps": "ppt", ".pot": "ppt", ".dot": "doc"}
AV_EXTS = {
    ".mp3", ".wav", ".flac", ".ogg", ".oga", ".m4a", ".aac", ".opus", ".wma", ".mp4", ".mkv", ".mov",
    ".avi", ".webm", ".wmv", ".m4v", ".3gp", ".mpg", ".mpeg",
}


def _zip_kind(path: Path, ext: str) -> str:
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist()[:5000])
            if "mimetype" in names:
                try:
                    mt = z.read("mimetype")[:100].decode("ascii", "ignore").strip()
                except (KeyError, RuntimeError, zipfile.BadZipFile, OSError):
                    mt = ""
                if mt == "application/epub+zip":
                    return "epub"
                if mt.startswith("application/vnd.oasis.opendocument"):
                    return "odf"
            if "[Content_Types].xml" in names:
                if any(n.startswith("word/") for n in names):
                    return "docx"
                if any(n.startswith("ppt/") for n in names):
                    return "pptx"
                if any(n.startswith("xl/") for n in names):
                    return "xlsx"
                if any(n.startswith("Documents/") or n.endswith(".fdseq") for n in names):
                    return "xps"
            if "FixedDocSeq.fdseq" in names or any(n.endswith(".fpage") for n in names):
                return "xps"
            if ext in (".cbz",):
                return "zip"
    except (zipfile.BadZipFile, OSError, RuntimeError, ValueError, NotImplementedError):
        return "corrupt-zip"
    return "zip"


def _looks_text(head: bytes) -> bool:
    if not head:
        return True
    if b"\x00" in head[:4096]:
        # UTF-16 with BOM is text; other NUL-bearing data is binary.
        return head[:2] in (b"\xff\xfe", b"\xfe\xff")
    try:
        head.decode("utf-8")
        return True
    except UnicodeDecodeError as exc:
        # A multi-byte character cut at the end of the sample is fine.
        if exc.start >= len(head) - 4:
            return True
    bad = sum(1 for b in head[:4096] if b < 9 or 13 < b < 32)
    return bad / max(1, min(len(head), 4096)) < 0.01


def sniff(path: Path) -> str:
    """Return a short kind label such as 'pdf', 'png', 'docx', 'zip', 'text', 'audio', 'unknown'."""
    path = Path(path)
    ext = path.suffix.lower()
    try:
        with open(path, "rb") as f:
            head = f.read(8192)
    except OSError:
        return "unreadable"
    if not head:
        return "empty"
    h = head
    # Strong magic numbers.
    if h[:1024].find(b"%PDF-") != -1:
        return "pdf"
    if h.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if h.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if h[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if h[:4] == b"RIFF" and h[8:12] == b"WEBP":
        return "webp"
    if h[:4] == b"RIFF" and h[8:12] in (b"WAVE", b"AVI "):
        return "audio" if h[8:12] == b"WAVE" else "video"
    if h[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if h[:2] == b"BM" and ext in (".bmp", ".dib"):
        return "bmp"
    if h[4:8] == b"ftyp":
        brand = h[8:12]
        if brand in (b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1"):
            return "heif"
        if brand in (b"avif", b"avis"):
            return "avif"
        if brand in (b"M4A ", b"M4B "):
            return "audio"
        return "video"
    if h.startswith(b"\x00\x00\x00\x0cjP  ") or h.startswith(b"\xff\x4f\xff\x51"):
        return "jp2"
    if h[:4] == b"\x00\x00\x01\x00" and ext == ".ico":
        return "ico"
    if h[:2] in (b"P1", b"P2", b"P3", b"P4", b"P5", b"P6") and ext in (".pbm", ".pgm", ".ppm", ".pnm"):
        return "pnm"
    if h.startswith(b"AT&TFORM"):
        return "djvu"
    if h.startswith(b"%!PS"):
        return "eps" if b"EPSF" in h[:40] else "ps"
    if h.startswith(b"\xc5\xd0\xd3\xc6"):
        return "eps"
    if h.startswith(b"{\\rtf"):
        return "rtf"
    if h.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return LEGACY_OFFICE.get(ext, "ole")
    if h.startswith(b"PK\x03\x04") or h.startswith(b"PK\x05\x06"):
        return _zip_kind(path, ext)
    if h.startswith(b"7z\xbc\xaf\x27\x1c"):
        return "7z"
    if h.startswith(b"Rar!\x1a\x07"):
        return "rar"
    if h.startswith(b"\x1f\x8b"):
        return "gzip"
    if h.startswith(b"BZh"):
        return "bzip2"
    if h.startswith(b"\xfd7zXZ\x00"):
        return "xz"
    if len(h) > 262 and h[257:262] == b"ustar":
        return "tar"
    if h[:3] == b"ID3" or h[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2") or h[:4] in (b"OggS", b"fLaC"):
        return "audio"
    if h[:4] == b"\x1a\x45\xdf\xa3":
        return "video"
    if h[:4] == b"\x00\x00\x01\xba" or h[:4] == b"\x00\x00\x01\xb3":
        return "video"
    if ext in AV_EXTS:
        return "audio" if ext in {".mp3", ".wav", ".flac", ".ogg", ".oga", ".m4a", ".aac", ".opus", ".wma"} else "video"
    if _looks_text(h):
        low = h[:2048].lstrip().lower()
        if ext == ".svg" or low.startswith(b"<svg") or (low.startswith(b"<?xml") and b"<svg" in h[:4096].lower()):
            return "svg"
        if ext in HTML_EXTS or low.startswith(b"<!doctype html") or low.startswith(b"<html"):
            return "html"
        if ext == ".ipynb":
            try:
                with open(path, "r", encoding="utf-8") as f:
                    obj = json.load(f)
                if isinstance(obj, dict) and "cells" in obj:
                    return "ipynb"
            except (OSError, ValueError):
                pass
            return "text"
        if ext == ".eml" or (ext == "" and _looks_like_email(h)):
            return "eml"
        if ext == ".mbox":
            return "mbox"
        if ext in (".xml",) and b"<fixeddocument" in h[:4096].lower():
            return "xps"
        return "text"
    return "unknown"


def _looks_like_email(h: bytes) -> bool:
    head = h[:2048].decode("latin-1", "ignore")
    keys = ("From:", "To:", "Subject:", "Date:", "Message-ID:", "MIME-Version:")
    return sum(1 for k in keys if ("\n" + k) in ("\n" + head)) >= 3


def is_image(kind: str) -> bool:
    return kind in IMAGE_KINDS


def is_archive(kind: str) -> bool:
    return kind in ("zip", "tar", "gzip", "bzip2", "xz", "7z", "rar")
