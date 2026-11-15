"""Rendering and pixel statistics, with no hard dependency beyond poppler.

Pillow and ImageMagick are used when present; otherwise pure-Python PGM/PNG
header parsing keeps triage working for PDFs.
"""

from __future__ import annotations

import math
import os
import shutil
import struct
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

from .util import run, which

MAX_PIXELS = 1_150_000      # the API downsizes images above about this many pixels
MAX_IMAGE_BYTES = 3_700_000  # stays under the 5 MB request limit after base64

try:  # optional
    from PIL import Image, ImageOps  # type: ignore

    HAVE_PIL = True
except Exception:  # noqa: BLE001
    HAVE_PIL = False


def magick() -> Optional[str]:
    return which("magick") or which("convert")


def magick_cmd(*args: str) -> List[str]:
    m = magick()
    if not m:
        return []
    return [m, *args]


# ------------------------------------------------------------------ PGM / dims


class Gray:
    """A small greyscale raster (row-major bytes)."""

    def __init__(self, w: int, h: int, data: bytes):
        self.w, self.h, self.data = w, h, data


def read_pgm(path: Path) -> Optional[Gray]:
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return None
    return parse_pnm(raw)


def parse_pnm(raw: bytes) -> Optional[Gray]:
    if len(raw) < 8 or raw[:1] != b"P":
        return None
    magic = raw[:2]
    tokens: list = []
    i = 2
    n = len(raw)
    need = 2 if magic in (b"P4", b"P1") else 3
    while len(tokens) < need and i < n:
        c = raw[i:i + 1]
        if c == b"#":
            while i < n and raw[i:i + 1] not in (b"\n", b"\r"):
                i += 1
        elif c.isspace():
            i += 1
        else:
            j = i
            while j < n and not raw[j:j + 1].isspace():
                j += 1
            tokens.append(int(raw[i:j]))
            i = j
    i += 1
    w, h = tokens[0], tokens[1]
    maxv = tokens[2] if need == 3 else 1
    body = raw[i:]
    if magic == b"P5" and maxv < 256:
        return Gray(w, h, bytes(body[: w * h]))
    if magic == b"P6" and maxv < 256:
        px = body[: w * h * 3]
        return Gray(w, h, bytes((px[k] * 3 + px[k + 1] * 6 + px[k + 2]) // 10 for k in range(0, len(px) - 2, 3)))
    if magic == b"P4":
        row = (w + 7) // 8
        out = bytearray()
        for y in range(h):
            for x in range(w):
                b = body[y * row + x // 8]
                out.append(0 if (b >> (7 - x % 8)) & 1 else 255)
        return Gray(w, h, bytes(out))
    if magic == b"P2":
        vals = [int(v) for v in body.split()[: w * h]]
        scale = 255 / max(1, maxv)
        return Gray(w, h, bytes(int(v * scale) for v in vals))
    return None


def image_dims(path: Path) -> Optional[Tuple[int, int]]:
    """Width/height from the file header for PNG, JPEG, GIF, WEBP (pure Python)."""
    try:
        with open(path, "rb") as f:
            head = f.read(64)
            if head.startswith(b"\x89PNG\r\n\x1a\n") and head[12:16] == b"IHDR":
                return struct.unpack(">II", head[16:24])
            if head[:6] in (b"GIF87a", b"GIF89a"):
                return struct.unpack("<HH", head[6:10])
            if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
                chunk = head[12:16]
                if chunk == b"VP8X":
                    w = 1 + int.from_bytes(head[24:27], "little")
                    h = 1 + int.from_bytes(head[27:30], "little")
                    return w, h
                if chunk == b"VP8 ":
                    w, h = struct.unpack("<HH", head[26:30])
                    return w & 0x3FFF, h & 0x3FFF
                if chunk == b"VP8L":
                    b = head[21:25]
                    w = 1 + (((b[1] & 0x3F) << 8) | b[0])
                    h = 1 + (((b[3] & 0xF) << 10) | (b[2] << 2) | ((b[1] & 0xC0) >> 6))
                    return w, h
            if head[:2] == b"\xff\xd8":
                f.seek(2)
                while True:
                    m = f.read(2)
                    if len(m) < 2 or m[0] != 0xFF:
                        return None
                    marker = m[1]
                    if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                        continue
                    seglen = struct.unpack(">H", f.read(2))[0]
                    if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                        f.read(1)
                        h, w = struct.unpack(">HH", f.read(4))
                        return w, h
                    f.seek(seglen - 2, 1)
    except (OSError, struct.error, IndexError):
        return None
    if HAVE_PIL:
        try:
            with Image.open(path) as im:
                return im.size
        except Exception:  # noqa: BLE001
            return None
    return None


def jpeg_orientation(path: Path) -> int:
    """EXIF orientation tag of a JPEG (1 if absent), pure Python."""
    try:
        with open(path, "rb") as f:
            if f.read(2) != b"\xff\xd8":
                return 1
            while True:
                m = f.read(2)
                if len(m) < 2 or m[0] != 0xFF:
                    return 1
                if m[1] == 0xDA:
                    return 1
                seglen = struct.unpack(">H", f.read(2))[0]
                seg = f.read(seglen - 2)
                if m[1] == 0xE1 and seg[:6] == b"Exif\x00\x00":
                    t = seg[6:]
                    le = t[:2] == b"II"
                    fmt = "<" if le else ">"
                    off = struct.unpack(fmt + "I", t[4:8])[0]
                    count = struct.unpack(fmt + "H", t[off:off + 2])[0]
                    for k in range(count):
                        e = t[off + 2 + 12 * k: off + 14 + 12 * k]
                        tag = struct.unpack(fmt + "H", e[:2])[0]
                        if tag == 0x0112:
                            return struct.unpack(fmt + "H", e[8:10])[0]
                    return 1
    except (OSError, struct.error, IndexError):
        return 1


# ------------------------------------------------------------------ statistics


def stats(g: Gray) -> dict:
    """Ink fraction (pixels that differ from the background), dHash, sample."""
    data = g.data
    if not data:
        return {"ink": 0.0, "dhash": "0" * 16, "w": g.w, "h": g.h}
    hist = [0] * 256
    step = max(1, len(data) // 60000)
    for k in range(0, len(data), step):
        hist[data[k]] += 1
    # Background = the most common brightness (works for dark slides too).
    bg = max(range(256), key=lambda v: sum(hist[max(0, v - 4):v + 5]))
    thr = 48
    ink = 0
    total = 0
    for k in range(0, len(data), step):
        total += 1
        if abs(data[k] - bg) > thr:
            ink += 1
    return {"ink": round(ink / max(1, total), 5), "bg": bg, "dhash": dhash(g), "w": g.w, "h": g.h}


def downsample(g: Gray, w: int, h: int) -> List[int]:
    out = []
    for y in range(h):
        y0 = y * g.h // h
        y1 = max(y0 + 1, (y + 1) * g.h // h)
        for x in range(w):
            x0 = x * g.w // w
            x1 = max(x0 + 1, (x + 1) * g.w // w)
            s = 0
            c = 0
            for yy in range(y0, y1, max(1, (y1 - y0) // 4)):
                row = yy * g.w
                for xx in range(x0, x1, max(1, (x1 - x0) // 4)):
                    s += g.data[row + xx]
                    c += 1
            out.append(s // max(1, c))
    return out


def dhash(g: Gray) -> str:
    px = downsample(g, 9, 8)
    bits = 0
    for y in range(8):
        for x in range(8):
            bits = (bits << 1) | (1 if px[y * 9 + x] > px[y * 9 + x + 1] else 0)
    return f"{bits:016x}"


def hamming(a: str, b: str) -> int:
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except ValueError:
        return 64


def pixel_mismatch(a: Gray, b: Gray, bg_a: int = 255, bg_b: int = 255) -> float:
    """Share of 'ink' pixels that differ between two same-size rasters (0 = identical)."""
    if a.w != b.w or a.h != b.h or not a.data:
        return 1.0
    thr = 48
    diff = 0
    ink = 0
    da, db = a.data, b.data
    for k in range(len(da)):
        ia = abs(da[k] - bg_a) > thr
        ib = abs(db[k] - bg_b) > thr
        if ia or ib:
            ink += 1
            if ia != ib or abs(da[k] - db[k]) > thr:
                diff += 1
    if ink == 0:
        return 0.0
    return diff / ink


# ------------------------------------------------------------------ rendering


def target_dims(w: float, h: float, long_side: int, max_pixels: int = MAX_PIXELS) -> Tuple[int, int]:
    if w <= 0 or h <= 0:
        return long_side, long_side
    s = min(1.0 * long_side / max(w, h), math.sqrt(max_pixels / (w * h)))
    s = min(s, 1e9)
    return max(16, int(w * s)), max(16, int(h * s))


def image_tokens(w: int, h: int) -> int:
    tw, th = target_dims(w, h, 1568)
    return max(1, (tw * th + 749) // 750)


def pdf_lowres(pdf: Path, dpi: int, outdir: Path, first: int = 1, last: Optional[int] = None,
               no_text: bool = False) -> dict:
    """Render pages to greyscale PGM at low DPI. Returns {page_number: Gray}."""
    outdir.mkdir(parents=True, exist_ok=True)
    pages: dict = {}
    if no_text:
        gs = which("gs") or which("gswin64c")
        if not gs:
            return pages
        cmd = [gs, "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE", "-dFILTERTEXT", "-sDEVICE=pgmraw", f"-r{dpi}",
               f"-dFirstPage={first}"]
        if last:
            cmd.append(f"-dLastPage={last}")
        cmd += ["-o", str(outdir / "nt-%05d.pgm"), str(pdf)]
        r = run(cmd, timeout=1800)
        if not r.ok:
            return pages
        for f in sorted(outdir.glob("nt-*.pgm")):
            n = int(f.stem.split("-")[1]) + first - 1
            g = read_pgm(f)
            if g:
                pages[n] = g
            f.unlink()
        return pages
    tool = which("pdftoppm")
    if tool:
        cmd = [tool, "-gray", "-r", str(dpi), "-f", str(first)]
        if last:
            cmd += ["-l", str(last)]
        cmd += [str(pdf), str(outdir / "lr")]
        run(cmd, timeout=1800)
        for f in sorted(outdir.glob("lr-*.pgm")):
            try:
                n = int(f.stem.rsplit("-", 1)[1])
            except ValueError:
                continue
            g = read_pgm(f)
            if g:
                pages[n] = g
            f.unlink()
        return pages
    mutool = which("mutool")
    if mutool:
        rng = f"{first}-{last}" if last else f"{first}-N"
        run([mutool, "draw", "-q", "-r", str(dpi), "-c", "gray", "-o", str(outdir / "lr-%d.pgm"), str(pdf), rng],
            timeout=1800)
        for f in sorted(outdir.glob("lr-*.pgm")):
            n = int(f.stem.rsplit("-", 1)[1])
            g = read_pgm(f)
            if g:
                pages[n] = g
            f.unlink()
    return pages


def image_lowres(path: Path, size: int = 300) -> Optional[Gray]:
    if HAVE_PIL:
        try:
            with Image.open(path) as im:
                im = ImageOps.exif_transpose(im)
                im = im.convert("L")
                im.thumbnail((size, size))
                return Gray(im.width, im.height, im.tobytes())
        except Exception:  # noqa: BLE001
            pass
    cmd = magick_cmd(str(path) + "[0]", "-auto-orient", "-colorspace", "Gray", "-resize", f"{size}x{size}",
                     "-depth", "8", "pgm:-")
    if cmd:
        r = run(cmd, timeout=120)
        if r.ok:
            return parse_pnm(r.stdout)
    return None


def _shrink_if_big(path: Path) -> Path:
    """Re-encode as JPEG if a render is too big to send."""
    try:
        if path.stat().st_size <= MAX_IMAGE_BYTES:
            return path
    except OSError:
        return path
    out = path.with_suffix(".jpg")
    if HAVE_PIL:
        try:
            with Image.open(path) as im:
                im = im.convert("RGB")
                for q in (85, 70, 55):
                    im.save(out, "JPEG", quality=q, optimize=True)
                    if out.stat().st_size <= MAX_IMAGE_BYTES:
                        path.unlink()
                        return out
        except Exception:  # noqa: BLE001
            pass
    cmd = magick_cmd(str(path), "-quality", "75", str(out))
    if cmd and run(cmd, timeout=120).ok and out.exists():
        path.unlink()
        return out
    return path


def render_pdf_page(pdf: Path, page: int, out: Path, w_pt: float, h_pt: float, long_side: int) -> Optional[Path]:
    tw, th = target_dims(w_pt, h_pt, long_side)
    out.parent.mkdir(parents=True, exist_ok=True)
    stem = out.with_suffix("")
    tool = which("pdftoppm")
    if tool:
        r = run([tool, "-png", "-f", str(page), "-l", str(page), "-scale-to-x", str(tw), "-scale-to-y", str(th),
                 "-singlefile", str(pdf), str(stem)], timeout=300)
        png = stem.with_suffix(".png")
        if r.ok and png.exists():
            return _shrink_if_big(png)
    mutool = which("mutool")
    if mutool:
        png = stem.with_suffix(".png")
        r = run([mutool, "draw", "-q", "-w", str(tw), "-h", str(th), "-o", str(png), str(pdf), str(page)], timeout=300)
        if r.ok and png.exists():
            return _shrink_if_big(png)
    return None


def render_pdf_tile(pdf: Path, page: int, out: Path, w_pt: float, h_pt: float, rows: int, idx: int,
                    long_side: int, overlap: float = 0.08) -> Optional[Path]:
    """Render horizontal band `idx` of `rows` (with overlap) at higher resolution."""
    tool = which("pdftoppm")
    band_h = h_pt / rows * (1 + overlap)
    tw, th = target_dims(w_pt, band_h, long_side)
    dpi = 72.0 * tw / w_pt
    y0 = max(0.0, h_pt * idx / rows - h_pt / rows * overlap / 2)
    y0 = min(y0, h_pt - band_h) if band_h < h_pt else 0.0
    out.parent.mkdir(parents=True, exist_ok=True)
    stem = out.with_suffix("")
    if tool:
        r = run([tool, "-png", "-f", str(page), "-l", str(page), "-r", f"{dpi:.2f}", "-x", "0",
                 "-y", str(int(y0 * dpi / 72)), "-W", str(tw), "-H", str(th), "-singlefile", str(pdf), str(stem)],
                timeout=300)
        png = stem.with_suffix(".png")
        if r.ok and png.exists():
            return _shrink_if_big(png)
    # Fallback: render big and crop.
    full = render_pdf_page(pdf, page, out.with_name(out.stem + "-full.png"), w_pt, h_pt, long_side * rows)
    if full:
        res = crop_band(full, out, rows, idx, long_side, overlap)
        try:
            full.unlink()
        except OSError:
            pass
        return res
    return None


def prepare_image(src: Path, out: Path, long_side: int) -> Optional[Path]:
    """Orient, scale and re-encode an image file so it can be sent to a model."""
    out.parent.mkdir(parents=True, exist_ok=True)
    if HAVE_PIL:
        try:
            with Image.open(src) as im:
                im = ImageOps.exif_transpose(im)
                if im.mode not in ("RGB", "L"):
                    bg = Image.new("RGB", im.size, (255, 255, 255))
                    rgba = im.convert("RGBA")
                    bg.paste(rgba, mask=rgba.split()[-1])
                    im = bg
                tw, th = target_dims(im.width, im.height, long_side)
                if tw < im.width:
                    im = im.resize((tw, th), Image.LANCZOS)
                png = out.with_suffix(".png")
                im.save(png, "PNG", optimize=True)
                return _shrink_if_big(png)
        except Exception:  # noqa: BLE001
            pass
    if magick():
        dims = image_dims(src) or (long_side, long_side)
        tw, th = target_dims(dims[0], dims[1], long_side)
        png = out.with_suffix(".png")
        cmd = magick_cmd(str(src) + "[0]", "-auto-orient", "-background", "white", "-flatten",
                         "-resize", f"{tw}x{th}>", str(png))
        if run(cmd, timeout=300).ok and png.exists():
            return _shrink_if_big(png)
    # No image library: send as-is if the API accepts the format and size.
    head = Path(src).read_bytes()[:16] if Path(src).exists() else b""
    ok_format = head.startswith(b"\x89PNG") or head.startswith(b"\xff\xd8") or head[:3] == b"GIF" or head[8:12] == b"WEBP"
    if ok_format and Path(src).stat().st_size <= MAX_IMAGE_BYTES:
        ext = {b"\x89P": ".png", b"\xff\xd8": ".jpg", b"GI": ".gif"}.get(head[:2], ".webp")
        dst = out.with_suffix(ext)
        shutil.copyfile(src, dst)
        return dst
    return None


def crop_band(src: Path, out: Path, rows: int, idx: int, long_side: int, overlap: float = 0.08) -> Optional[Path]:
    dims = image_dims(src)
    if not dims:
        return None
    w, h = dims
    band = int(h / rows * (1 + overlap))
    y0 = max(0, min(h - band, int(h * idx / rows - h / rows * overlap / 2)))
    out.parent.mkdir(parents=True, exist_ok=True)
    png = out.with_suffix(".png")
    if HAVE_PIL:
        try:
            with Image.open(src) as im:
                im = ImageOps.exif_transpose(im).convert("RGB")
                piece = im.crop((0, y0, w, y0 + band))
                tw, th = target_dims(piece.width, piece.height, long_side)
                if tw < piece.width:
                    piece = piece.resize((tw, th), Image.LANCZOS)
                piece.save(png, "PNG", optimize=True)
                return _shrink_if_big(png)
        except Exception:  # noqa: BLE001
            pass
    if magick():
        tw, th = target_dims(w, band, long_side)
        cmd = magick_cmd(str(src) + "[0]", "-auto-orient", "-crop", f"{w}x{band}+0+{y0}", "+repage",
                         "-resize", f"{tw}x{th}>", str(png))
        if run(cmd, timeout=300).ok and png.exists():
            return _shrink_if_big(png)
    return None


def split_multipage_image(src: Path, outdir: Path) -> List[Path]:
    """Split a multi-page TIFF (or similar) into PNG pages."""
    outdir.mkdir(parents=True, exist_ok=True)
    if HAVE_PIL:
        try:
            from PIL import ImageSequence  # type: ignore

            paths = []
            with Image.open(src) as im:
                for k, frame in enumerate(ImageSequence.Iterator(im), 1):
                    p = outdir / f"page-{k:04d}.png"
                    fr = frame.convert("RGB") if frame.mode not in ("RGB", "L") else frame.copy()
                    fr.save(p, "PNG")
                    paths.append(p)
            if paths:
                return paths
        except Exception:  # noqa: BLE001
            pass
    cmd = magick_cmd(str(src), "-auto-orient", str(outdir / "page-%04d.png"))
    if cmd and run(cmd, timeout=900).ok:
        return sorted(outdir.glob("page-*.png"))
    return []


def convert_image_to_png(src: Path, out: Path) -> Optional[Path]:
    """Convert odd formats (HEIC, JP2, BMP, SVG, ICO...) to PNG if any tool can."""
    out.parent.mkdir(parents=True, exist_ok=True)
    png = out.with_suffix(".png")
    if HAVE_PIL:
        try:
            with Image.open(src) as im:
                im = ImageOps.exif_transpose(im)
                im.convert("RGB").save(png, "PNG")
                return png
        except Exception:  # noqa: BLE001
            pass
    if str(src).lower().endswith(".svg") and which("rsvg-convert"):
        if run([which("rsvg-convert"), "-o", str(png), str(src)], timeout=120).ok:
            return png
    heif = which("heif-convert") or which("heif-dec")
    if heif and str(src).lower().endswith((".heic", ".heif", ".avif")):
        if run([heif, str(src), str(png)], timeout=300).ok and png.exists():
            return png
    cmd = magick_cmd(str(src) + "[0]", "-auto-orient", str(png))
    if cmd and run(cmd, timeout=300).ok and png.exists():
        return png
    return None


def tempdir(base: Path, prefix: str) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=str(base)))


def cleanup(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def file_size(p: Path) -> int:
    try:
        return os.path.getsize(p)
    except OSError:
        return 0
