"""Generate test documents with nothing but the standard library.

Minimal but valid PDFs (text pages, "scanned" image pages, blank pages,
bookmarks), PNGs, Office files, EPUB, email and archives.
"""

from __future__ import annotations

import io
import json
import random
import struct
import zipfile
import zlib
from email.message import EmailMessage
from pathlib import Path
from typing import List, Optional, Sequence


# ----------------------------------------------------------------- rasters


def scribble(w: int, h: int, seed: int, strokes: int = 60, bg: int = 255, ink: int = 20) -> bytes:
    """A greyscale image with pseudo-handwriting lines (deterministic per seed)."""
    rng = random.Random(seed)
    buf = bytearray([bg]) * (w * h)
    line_h = max(12, h // 18)
    for row in range(2, 16):
        y = row * line_h
        x = rng.randint(w // 20, w // 8)
        while x < w - w // 10:
            seg = rng.randint(w // 30, w // 8)
            for k in range(seg):
                yy = y + int(4 * __import__("math").sin((x + k) / rng.uniform(3, 9)))
                for t in range(-1, 2):
                    py = yy + t
                    px = x + k
                    if 0 <= py < h and 0 <= px < w:
                        buf[py * w + px] = ink
            x += seg + rng.randint(w // 60, w // 25)
        if rng.random() < 0.15:
            break
    for _ in range(strokes // 6):
        x0, y0 = rng.randrange(w), rng.randrange(h)
        for k in range(rng.randint(10, 60)):
            px, py = x0 + k, y0 + rng.randint(-1, 1)
            if 0 <= py < h and 0 <= px < w:
                buf[py * w + px] = ink
    return bytes(buf)


def png_bytes(w: int, h: int, gray: bytes) -> bytes:
    raw = b"".join(b"\x00" + gray[y * w:(y + 1) * w] for y in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def write_png(path: Path, w: int = 600, h: int = 800, seed: int = 1, blank: bool = False) -> Path:
    gray = bytes([255]) * (w * h) if blank else scribble(w, h, seed)
    path.write_bytes(png_bytes(w, h, gray))
    return path


# ----------------------------------------------------------------- PDF


class Page:
    def __init__(self, lines: Sequence[str] = (), image_seed: Optional[int] = None, image_px=(850, 1100),
                 size=(612, 792), image_dpi: Optional[float] = None):
        self.lines = list(lines)
        self.image_seed = image_seed
        self.image_px = image_px
        self.size = size
        self.image_dpi = image_dpi


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def write_pdf(path: Path, pages: List[Page], outline: Optional[List[tuple]] = None) -> Path:
    objs: List[bytes] = []

    def add(b: bytes) -> int:
        objs.append(b)
        return len(objs)

    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    pages_id_placeholder = add(b"")  # filled later
    page_ids = []
    for pg in pages:
        res = f"<< /Font << /F1 {font} 0 R >>".encode()
        content = []
        if pg.image_seed is not None:
            iw, ih = pg.image_px
            data = zlib.compress(scribble(iw, ih, pg.image_seed))
            img = add(f"<< /Type /XObject /Subtype /Image /Width {iw} /Height {ih} /ColorSpace /DeviceGray "
                      f"/BitsPerComponent 8 /Filter /FlateDecode /Length {len(data)} >>\nstream\n".encode()
                      + data + b"\nendstream")
            res += f" /XObject << /Im1 {img} 0 R >>".encode()
            if pg.image_dpi:
                dw, dh = iw * 72.0 / pg.image_dpi, ih * 72.0 / pg.image_dpi
            else:
                dw, dh = pg.size
            content.append(f"q {dw:.2f} 0 0 {dh:.2f} 0 {pg.size[1] - dh:.2f} cm /Im1 Do Q")
        y = pg.size[1] - 72
        for ln in pg.lines:
            content.append(f"BT /F1 11 Tf 72 {y} Td ({_esc(ln)}) Tj ET")
            y -= 15
        stream = "\n".join(content).encode("latin-1", "replace")
        cid = add(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream")
        res += b" >>"
        pid = add(f"<< /Type /Page /Parent {pages_id_placeholder} 0 R /MediaBox [0 0 {pg.size[0]} {pg.size[1]}] "
                  f"/Resources ".encode() + res + f" /Contents {cid} 0 R >>".encode())
        page_ids.append(pid)
    kids = " ".join(f"{p} 0 R" for p in page_ids)
    objs[pages_id_placeholder - 1] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode()
    outline_ref = ""
    if outline:
        root_ol = len(objs) + 1
        objs.append(b"")
        item_ids = list(range(root_ol + 1, root_ol + 1 + len(outline)))
        for k, (title, pageno) in enumerate(outline):
            prev = f" /Prev {item_ids[k - 1]} 0 R" if k else ""
            nxt = f" /Next {item_ids[k + 1]} 0 R" if k + 1 < len(outline) else ""
            objs.append(f"<< /Title ({_esc(title)}) /Parent {root_ol} 0 R{prev}{nxt} "
                        f"/Dest [{page_ids[pageno - 1]} 0 R /Fit] >>".encode())
        objs[root_ol - 1] = (f"<< /Type /Outlines /First {item_ids[0]} 0 R /Last {item_ids[-1]} 0 R "
                             f"/Count {len(outline)} >>").encode()
        outline_ref = f" /Outlines {root_ol} 0 R"
    catalog = add(f"<< /Type /Catalog /Pages {pages_id_placeholder} 0 R{outline_ref} >>".encode())
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, b in enumerate(objs, 1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + b + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    for o in offsets:
        out.write(f"{o:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objs) + 1} /Root {catalog} 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    path.write_bytes(out.getvalue())
    return path


LOREM = [
    "The method of characteristics turns a first-order equation into ordinary equations along curves.",
    "Each curve carries a constant value of the solution, so the initial data travels with it.",
    "When two characteristic curves cross, the solution develops a discontinuity called a shock.",
    "The speed of the shock follows from conservation of the integrated quantity across the jump.",
    "In practice one sketches the curves first and then reads off where the solution is defined.",
]


def text_lines(seed: int, n: int = 14) -> List[str]:
    rng = random.Random(seed)
    return [f"{seed}.{k} " + rng.choice(LOREM) for k in range(n)]


# ----------------------------------------------------------------- office etc.


def write_docx(path: Path, paragraphs: List[tuple]) -> Path:
    body = []
    for style, text in paragraphs:
        ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
        body.append(f'<w:p>{ppr}<w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>')
    doc = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
           + "".join(body) + "</w:body></w:document>")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        z.writestr("_rels/.rels", '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        z.writestr("word/document.xml", doc)
    return path


def write_pptx(path: Path, slides: List[str], image: Optional[bytes] = None) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        for i, s in enumerate(slides, 1):
            z.writestr(f"ppt/slides/slide{i}.xml",
                       '<?xml version="1.0"?><p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
                       'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:cSld><p:spTree><p:sp><p:txBody>'
                       f'<a:p><a:r><a:t>{s}</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>')
        if image:
            z.writestr("ppt/media/image1.png", image)
    return path


def write_epub(path: Path, chapters: List[str]) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", '<?xml version="1.0"?><container version="1.0" '
                   'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                   '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        items = "".join(f'<item id="c{i}" href="c{i}.xhtml" media-type="application/xhtml+xml"/>' for i in range(len(chapters)))
        spine = "".join(f'<itemref idref="c{i}"/>' for i in range(len(chapters)))
        z.writestr("OEBPS/content.opf", f'<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
                   f'<manifest>{items}</manifest><spine>{spine}</spine></package>')
        for i, c in enumerate(chapters):
            z.writestr(f"OEBPS/c{i}.xhtml", f"<html><body><h1>Chapter {i + 1}</h1><p>{c}</p></body></html>")
    return path


def write_eml(path: Path, body: str, attachment: Optional[tuple] = None) -> Path:
    m = EmailMessage()
    m["From"] = "sender@example.org"
    m["To"] = "reader@example.org"
    m["Subject"] = "Course material"
    m["Date"] = "Mon, 02 Mar 2026 10:00:00 +0000"
    m.set_content(body)
    if attachment:
        name, data, maintype, subtype = attachment
        m.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    path.write_bytes(bytes(m))
    return path


def write_ipynb(path: Path, md: str, code: str, png: Optional[bytes] = None) -> Path:
    import base64

    outputs = []
    if png:
        outputs.append({"output_type": "display_data", "data": {"image/png": base64.b64encode(png).decode()}, "metadata": {}})
    nb = {"cells": [{"cell_type": "markdown", "source": [md], "metadata": {}},
                    {"cell_type": "code", "source": [code], "outputs": outputs, "metadata": {}, "execution_count": 1}],
          "metadata": {}, "nbformat": 4, "nbformat_minor": 5}
    path.write_text(json.dumps(nb), encoding="utf-8")
    return path


def write_zip(path: Path, members: dict) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return path


def mixed_pile(root: Path) -> Path:
    """A messy pile covering most input types and page situations."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "week 1").mkdir(exist_ok=True)
    (root / "week 10").mkdir(exist_ok=True)
    (root / "week 2").mkdir(exist_ok=True)
    typed = [Page(text_lines(1)), Page(text_lines(2)), Page([]), Page(text_lines(3))]
    write_pdf(root / "week 1" / "typed notes.pdf", typed, outline=[("Introduction", 1), ("Shocks", 3)])
    scans = [Page(image_seed=11), Page(image_seed=12), Page(image_seed=12), Page([]), Page(image_seed=13, image_dpi=80)]
    write_pdf(root / "week 2" / "scan.pdf", scans)
    titles = [Page(["Course title", "Lecture"]) if k % 2 == 0 else Page(text_lines(20 + k)) for k in range(6)]
    write_pdf(root / "week 10" / "slides.pdf", titles)
    write_png(root / "week 2" / "photo of board.png", seed=21)
    write_png(root / "blank.png", blank=True)
    write_docx(root / "summary.docx", [("Heading1", "Overview"), ("", "A short paragraph of typed text.")])
    write_pptx(root / "deck.pptx", ["First slide text", "Second slide text"], image=png_bytes(200, 200, scribble(200, 200, 5)))
    write_epub(root / "book.epub", ["Chapter one text. " * 30, "Chapter two text. " * 30])
    write_eml(root / "mail.eml", "Please find the notes attached.",
              ("attached.pdf", (root / "week 1" / "typed notes.pdf").read_bytes(), "application", "pdf"))
    write_ipynb(root / "analysis.ipynb", "# Analysis\nSome notes.", "print(1)", png_bytes(300, 200, scribble(300, 200, 9)))
    write_zip(root / "images.zip", {"a/one.png": png_bytes(400, 500, scribble(400, 500, 31)),
                                    "a/two.png": png_bytes(400, 500, scribble(400, 500, 32)),
                                    "../evil.txt": b"path traversal attempt"})
    (root / "notes.md").write_text("# Notes\n\nSome markdown text about the topic.\n\n## Part two\n\nMore.\n", encoding="utf-8")
    (root / "empty.txt").write_bytes(b"")
    (root / "mystery.bin").write_bytes(bytes(range(256)) * 20)
    (root / "recording.mp3").write_bytes(b"ID3" + b"\x00" * 200)
    (root / ".DS_Store").write_bytes(b"junk")
    (root / "~$lock.docx").write_bytes(b"junk")
    (root / "copy of typed notes.pdf").write_bytes((root / "week 1" / "typed notes.pdf").read_bytes())
    (root / "broken.pdf").write_bytes(b"%PDF-1.4\nthis is not really a pdf\n")
    return root
