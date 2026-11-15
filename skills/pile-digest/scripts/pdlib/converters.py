"""Turn any inventoried file into a Document made of page units.

A page unit is the thing a model may later read. It has an optional text
layer and, when vision may be needed, a render spec (a PDF page or an image
file). Every converter prefers the best installed tool and falls back to
pure-Python extraction, so a missing optional tool degrades quality instead of
failing the file.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import List, Optional

from . import raster
from .textutil import chunk_text, decode_bytes, html_images, html_to_text, rtf_to_text, tidy
from .util import run, sha256_bytes, which

MIN_EMBEDDED_IMAGE_BYTES = 6000  # skip logos, bullets and spacer images


def new_doc(entry: dict, **kw) -> dict:
    d = {
        "doc_id": entry["sha"][:16],
        "relpath": entry["relpath"],
        "kind": entry.get("kind"),
        "status": "ok",
        "note": "",
        "title": None,
        "outline": [],
        "units": [],
        "converter": None,
    }
    d.update(kw)
    return d


def text_unit(text: str, source: str = "file") -> dict:
    return {"text": text, "text_source": source, "render": None}


def image_unit(path: Path, text: str = "", note: str = "") -> dict:
    dims = raster.image_dims(path)
    u = {"text": text, "text_source": "embedded" if text else "none",
         "render": {"type": "image", "path": str(path)}}
    if dims:
        u["w_px"], u["h_px"] = dims
    if note:
        u["note"] = note
    return u


# ----------------------------------------------------------------------- PDF


def pdfinfo(pdf: Path) -> dict:
    tool = which("pdfinfo")
    info: dict = {"ok": False}
    if tool:
        r = run([tool, "-f", "1", "-l", "100000", str(pdf)], timeout=300)
        text = r.out() + "\n" + r.err()
        if "Incorrect password" in text or "Command Line Error: Incorrect password" in text:
            info["needs_password"] = True
            return info
        if not r.ok:
            info["error"] = r.err().strip()[:300]
            return info
        info["ok"] = True
        sizes: dict = {}
        rots: dict = {}
        for line in r.out().splitlines():
            m = re.match(r"Page\s+(\d+)\s+size:\s+([\d.]+)\s+x\s+([\d.]+)", line)
            if m:
                sizes[int(m.group(1))] = (float(m.group(2)), float(m.group(3)))
                continue
            m = re.match(r"Page\s+(\d+)\s+rot:\s+(\d+)", line)
            if m:
                rots[int(m.group(1))] = int(m.group(2)) % 360
                continue
            if line.startswith("Pages:"):
                info["pages"] = int(line.split()[1])
            elif line.startswith("Title:"):
                info["title"] = line.split(":", 1)[1].strip() or None
            elif line.startswith("Encrypted:"):
                info["encrypted"] = line.split(":", 1)[1].strip().startswith("yes")
            elif line.startswith("Page size:") and 1 not in sizes:
                m = re.search(r"([\d.]+)\s+x\s+([\d.]+)", line)
                if m:
                    sizes.setdefault(0, (float(m.group(1)), float(m.group(2))))
        info["sizes"] = sizes
        info["rots"] = rots
        return info
    mutool = which("mutool")
    if mutool:
        r = run([mutool, "info", str(pdf)], timeout=300)
        if not r.ok:
            if "password" in r.err().lower():
                info["needs_password"] = True
            else:
                info["error"] = r.err().strip()[:300]
            return info
        m = re.search(r"Pages:\s*(\d+)", r.out())
        if m:
            info.update(ok=True, pages=int(m.group(1)), sizes={}, rots={})
    return info


def pdf_texts(pdf: Path, n: int) -> List[str]:
    tool = which("pdftotext")
    texts: List[str] = []
    if tool:
        r = run([tool, "-layout", "-enc", "UTF-8", str(pdf), "-"], timeout=1800)
        if r.ok:
            texts = r.out().split("\f")
    elif which("mutool"):
        r = run([which("mutool"), "draw", "-q", "-F", "txt", "-o", "-", str(pdf)], timeout=1800)
        if r.ok:
            texts = r.out().split("\f")
    texts = [tidy(t) for t in texts[:n]]
    while len(texts) < n:
        texts.append("")
    return texts


def pdf_images(pdf: Path) -> dict:
    """{page: [(w_px, h_px, x_ppi, y_ppi)]} from `pdfimages -list`."""
    tool = which("pdfimages")
    out: dict = {}
    if not tool:
        return out
    r = run([tool, "-list", str(pdf)], timeout=900)
    if not r.ok:
        return out
    for line in r.out().splitlines()[2:]:
        parts = line.split()
        if len(parts) < 14:
            continue
        try:
            page, w, h = int(parts[0]), int(parts[3]), int(parts[4])
            xp, yp = float(parts[12]), float(parts[13])
        except ValueError:
            continue
        if parts[2] in ("smask", "stencil"):
            continue
        out.setdefault(page, []).append((w, h, xp, yp))
    return out


def pdf_outline(pdf: Path) -> List[dict]:
    q = which("qpdf")
    res: List[dict] = []
    if q:
        for flag in ("--json=2", "--json"):
            r = run([q, flag, "--json-key=outlines", str(pdf)], timeout=300)
            if not r.ok and r.returncode != 3:
                continue
            try:
                data = json.loads(r.out())
            except ValueError:
                continue

            def walk(items, level):
                for it in items or []:
                    title = (it.get("title") or "").strip()
                    page = it.get("destpageposfrom1")
                    if title:
                        res.append({"title": title, "page": page, "level": level})
                    walk(it.get("kids"), level + 1)

            walk(data.get("outlines"), 1)
            return res
    ph = which("pdftohtml")
    if ph:
        # Part of poppler-utils; -l 1 keeps the page output tiny, the outline is always complete.
        r = run([ph, "-xml", "-stdout", "-i", "-q", "-nodrm", "-l", "1", str(pdf)], timeout=300)
        if r.ok and "<outline" in r.out():
            try:
                xml = r.out()
                start = xml.index("<outline")
                end = xml.rindex("</outline>") + len("</outline>")
                root = ET.fromstring(xml[start:end])

                def walk_x(el, level):
                    for it in el:
                        if it.tag == "item":
                            title = "".join(it.itertext()).strip()
                            try:
                                page = int(it.get("page")) if it.get("page") else None
                            except ValueError:
                                page = None
                            if title:
                                res.append({"title": title, "page": page, "level": level})
                        elif it.tag == "outline":
                            walk_x(it, level + 1)

                walk_x(root, 1)
                return res
            except (ValueError, ET.ParseError):
                res.clear()
    m = which("mutool")
    if m:
        r = run([m, "show", str(pdf), "outline"], timeout=300)
        if r.ok:
            for line in r.out().splitlines():
                mt = re.match(r"^([|+\-\s]*)\"?(.*?)\"?\s+#(?:page=)?(\d+)", line.strip("\n"))
                if mt:
                    level = 1 + mt.group(1).count("\t") + mt.group(1).count("|")
                    res.append({"title": mt.group(2).strip(), "page": int(mt.group(3)), "level": level})
    return res


def repair_pdf(pdf: Path, workdir: Path) -> Optional[Path]:
    workdir.mkdir(parents=True, exist_ok=True)
    out = workdir / "repaired.pdf"
    q = which("qpdf")
    if q:
        r = run([q, str(pdf), str(out)], timeout=900)
        if (r.ok or r.returncode == 3) and out.exists() and pdfinfo(out).get("ok"):
            return out
    m = which("mutool")
    if m:
        r = run([m, "clean", "-gg", str(pdf), str(out)], timeout=900)
        if r.ok and out.exists() and pdfinfo(out).get("ok"):
            return out
    gs = which("gs")
    if gs:
        r = run([gs, "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE", "-sDEVICE=pdfwrite", "-o", str(out), str(pdf)],
                timeout=1800)
        if r.ok and out.exists() and pdfinfo(out).get("ok"):
            return out
    return None


def unlock_pdf(pdf: Path, password: str, workdir: Path) -> Optional[Path]:
    q = which("qpdf")
    workdir.mkdir(parents=True, exist_ok=True)
    out = workdir / "unlocked.pdf"
    if not q:
        return None
    pwfile = workdir / ".pw"
    try:
        fd = os.open(str(pwfile), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(password)
        r = run([q, f"--password-file={pwfile}", "--decrypt", str(pdf), str(out)], timeout=900)
    finally:
        try:
            pwfile.unlink()
        except OSError:
            pass
    if (r.ok or r.returncode == 3) and out.exists():
        return out
    return None


def convert_pdf(entry: dict, work: Path, *, source_path: Optional[Path] = None, kind: Optional[str] = None) -> dict:
    pdf = Path(source_path or entry["path"])
    doc = new_doc(entry, converter="pdf")
    if kind:
        doc["kind"] = kind
    unlocked = work / "unlocked.pdf"
    if unlocked.exists():
        pdf = unlocked
    info = pdfinfo(pdf)
    if info.get("needs_password"):
        doc.update(status="needs-password",
                   note="PDF is password-protected. Run `pd.py unlock <pile> <file>` in your own terminal.")
        return doc
    if not info.get("ok"):
        fixed = repair_pdf(pdf, work)
        if not fixed:
            doc.update(status="unreadable", note=f"damaged PDF that no installed tool could repair ({info.get('error', '')})")
            return doc
        pdf = fixed
        info = pdfinfo(pdf)
        doc["note"] = "repaired a damaged PDF"
    n = int(info.get("pages") or 0)
    if n == 0:
        doc.update(status="skipped", note="PDF has no pages")
        return doc
    doc["title"] = info.get("title")
    doc["pdf"] = str(pdf)
    texts = pdf_texts(pdf, n)
    imgs = pdf_images(pdf)
    sizes = info.get("sizes", {})
    rots = info.get("rots", {})
    default = sizes.get(1) or sizes.get(0) or (612.0, 792.0)
    for p in range(1, n + 1):
        w, h = sizes.get(p, default)
        if rots.get(p, 0) in (90, 270):
            w, h = h, w
        area_in2 = max(1e-6, (w / 72.0) * (h / 72.0))
        cov = 0.0
        ppis = []
        for (iw, ih, xp, yp) in imgs.get(p, []):
            if xp > 0 and yp > 0:
                cov += (iw / xp) * (ih / yp) / area_in2
                if iw * ih > 40000:
                    ppis.append(min(xp, yp))
        u = {
            "text": texts[p - 1],
            "text_source": "layer" if texts[p - 1].strip() else "none",
            "render": {"type": "pdf", "path": str(pdf), "page": p},
            "w_pt": round(w, 2),
            "h_pt": round(h, 2),
            "image_cov": round(min(1.0, cov), 3),
        }
        if ppis:
            u["source_ppi"] = round(min(ppis), 1)
        doc["units"].append(u)
    doc["outline"] = pdf_outline(pdf)
    return doc


# ------------------------------------------------------------------- office


def soffice_to_pdf(src: Path, work: Path) -> Optional[Path]:
    so = which("soffice") or which("libreoffice")
    if not so:
        return None
    work.mkdir(parents=True, exist_ok=True)
    out = work / (src.stem + ".pdf")
    if out.exists() and out.stat().st_size > 0:
        return out
    profile = work / "lo-profile"
    # Copy to a plain name: LibreOffice is fussy with some characters.
    tmp_src = work / ("input" + src.suffix.lower())
    shutil.copyfile(src, tmp_src)
    r = run([so, "--headless", "--norestore", f"-env:UserInstallation={profile.resolve().as_uri()}",
             "--convert-to", "pdf", "--outdir", str(work), str(tmp_src)], timeout=600)
    produced = work / "input.pdf"
    shutil.rmtree(profile, ignore_errors=True)
    try:
        tmp_src.unlink()
    except OSError:
        pass
    if r.ok and produced.exists() and produced.stat().st_size > 0:
        produced.replace(out)
        return out
    return None


NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
}


def _xml_text(data: bytes, para_tags=("p",)) -> str:
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return ""
    out: List[str] = []

    def rec(el):
        tag = el.tag.rsplit("}", 1)[-1]
        if tag in ("t", "instrText") and el.text:
            out.append(el.text)
        elif tag == "tab":
            out.append("\t")
        elif tag in ("br", "cr"):
            out.append("\n")
        for c in el:
            rec(c)
        if tag in para_tags:
            out.append("\n")

    rec(root)
    return tidy("".join(out))


def _docx_text(z: zipfile.ZipFile) -> str:
    try:
        data = z.read("word/document.xml")
    except KeyError:
        return ""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return ""
    lines = []
    for p in root.iter("{%s}p" % NS["w"]):
        style = p.find(".//{%s}pStyle" % NS["w"])
        sval = style.get("{%s}val" % NS["w"]) if style is not None else ""
        txt = "".join(t.text or "" for t in p.iter() if t.tag in ("{%s}t" % NS["w"],))
        m = re.match(r"(?i)heading\s*(\d)|title", sval or "")
        if m and txt.strip():
            level = int(m.group(1)) if m.group(1) else 1
            txt = "#" * level + " " + txt
        lines.append(txt)
    return tidy("\n".join(lines))


def _media(z: zipfile.ZipFile, prefixes, outdir: Path) -> List[Path]:
    paths = []
    seen = set()
    for name in sorted(z.namelist()):
        if not any(name.startswith(p) for p in prefixes):
            continue
        if not name.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".emf", ".wmf")):
            continue
        try:
            info = z.getinfo(name)
            if info.file_size < MIN_EMBEDDED_IMAGE_BYTES or info.file_size > (60 << 20):
                continue
            data = z.read(name)
        except (KeyError, RuntimeError, zipfile.BadZipFile, OSError):
            continue
        h = sha256_bytes(data)
        if h in seen:
            continue
        seen.add(h)
        outdir.mkdir(parents=True, exist_ok=True)
        p = outdir / (h[:12] + Path(name).suffix.lower())
        p.write_bytes(data)
        if p.suffix in (".emf", ".wmf", ".bmp", ".tif", ".tiff"):
            conv = raster.convert_image_to_png(p, p.with_suffix(".conv.png"))
            if not conv:
                continue
            p = conv
        paths.append(p)
    return paths


def convert_office(entry: dict, work: Path, cfg: dict) -> dict:
    src = Path(entry["path"])
    kind = entry.get("kind")
    spreadsheet = kind in ("xlsx", "xls") or src.suffix.lower() in (".xlsx", ".xlsm", ".xls", ".ods")
    if not spreadsheet:
        pdf = soffice_to_pdf(src, work)
        if pdf:
            doc = convert_pdf(entry, work / "pdf", source_path=pdf, kind=kind)
            doc["converter"] = "libreoffice→pdf"
            return doc
    doc = new_doc(entry, converter="office-xml")
    if kind in ("doc", "ppt", "xls", "ole", "msg"):
        if spreadsheet and (which("soffice") or which("libreoffice")):
            pdf = soffice_to_pdf(src, work)
            if pdf:
                d2 = convert_pdf(entry, work / "pdf", source_path=pdf, kind=kind)
                d2["converter"] = "libreoffice→pdf"
                return d2
        doc.update(status="unreadable", note="legacy Office file; install LibreOffice to read it")
        return doc
    try:
        z = zipfile.ZipFile(src)
    except (zipfile.BadZipFile, OSError) as exc:
        doc.update(status="unreadable", note=f"damaged Office file ({exc})")
        return doc
    size = int(cfg.get("text_page_chars", 3500))
    with z:
        names = z.namelist()
        if kind == "docx":
            for c in chunk_text(_docx_text(z), size):
                doc["units"].append(text_unit(c))
            for p in _media(z, ("word/media/",), work / "media"):
                doc["units"].append(image_unit(p, note="embedded image"))
        elif kind == "pptx":
            slides = sorted(
                (n for n in names if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                key=lambda n: int(re.search(r"(\d+)", n.rsplit("/", 1)[1]).group(1)),
            )
            for s in slides:
                txt = _xml_text(z.read(s), para_tags=("p",))
                notes_name = s.replace("slides/slide", "notesSlides/notesSlide")
                if notes_name in names:
                    nt = _xml_text(z.read(notes_name), para_tags=("p",))
                    if nt.strip():
                        txt += "\n\n[Speaker notes]\n" + nt
                doc["units"].append(text_unit(txt or ""))
            for p in _media(z, ("ppt/media/",), work / "media"):
                doc["units"].append(image_unit(p, note="embedded image"))
            doc["note"] = "read without LibreOffice: slide layout and drawn shapes are lost"
        elif kind == "xlsx":
            shared: List[str] = []
            if "xl/sharedStrings.xml" in names:
                try:
                    root = ET.fromstring(z.read("xl/sharedStrings.xml"))
                    for si in root.iter("{%s}si" % NS["s"]):
                        shared.append("".join(t.text or "" for t in si.iter("{%s}t" % NS["s"])))
                except ET.ParseError:
                    pass
            sheets = sorted(n for n in names if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
            for sh in sheets:
                try:
                    root = ET.fromstring(z.read(sh))
                except ET.ParseError:
                    continue
                rows = []
                for row in root.iter("{%s}row" % NS["s"]):
                    cells = []
                    for c in row.iter("{%s}c" % NS["s"]):
                        v = c.find("{%s}v" % NS["s"])
                        val = v.text if v is not None and v.text else ""
                        if c.get("t") == "s" and val.isdigit() and int(val) < len(shared):
                            val = shared[int(val)]
                        elif c.get("t") == "inlineStr":
                            val = "".join(t.text or "" for t in c.iter("{%s}t" % NS["s"]))
                        cells.append(val)
                    if any(cells):
                        rows.append("\t".join(cells))
                text = f"[Sheet {sh.rsplit('/', 1)[1][:-4]}]\n" + "\n".join(rows)
                for ch in chunk_text(text, size):
                    doc["units"].append(text_unit(ch))
        elif kind == "odf":
            try:
                txt = _xml_text(z.read("content.xml"), para_tags=("p", "h", "table-row"))
            except KeyError:
                txt = ""
            for c in chunk_text(txt, size):
                doc["units"].append(text_unit(c))
            for p in _media(z, ("Pictures/",), work / "media"):
                doc["units"].append(image_unit(p, note="embedded image"))
            doc["note"] = "read without LibreOffice: layout is lost"
    if not doc["units"]:
        doc.update(status="skipped", note="no text or images found in the file")
    return doc


# --------------------------------------------------------------- text-likes


def convert_text(entry: dict, work: Path, cfg: dict) -> dict:
    doc = new_doc(entry, converter="text")
    src = Path(entry["path"])
    kind = entry.get("kind")
    try:
        raw = src.read_bytes()
    except OSError as exc:
        doc.update(status="unreadable", note=str(exc))
        return doc
    text = decode_bytes(raw)
    images: List[Path] = []
    if kind == "html":
        for k, s in enumerate(html_images(text)):
            if s.startswith("data:image/") and ";base64," in s:
                try:
                    data = base64.b64decode(s.split(",", 1)[1], validate=False)
                except (ValueError, base64.binascii.Error):
                    continue
                if len(data) >= MIN_EMBEDDED_IMAGE_BYTES:
                    p = work / "media" / f"inline-{k}.{s[11:s.index(';')] or 'png'}"
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(data)
                    images.append(p)
            elif not re.match(r"^[a-z]+://", s):
                cand = (src.parent / s.split("?")[0]).resolve()
                if cand.is_file() and cand.stat().st_size >= MIN_EMBEDDED_IMAGE_BYTES:
                    images.append(cand)
        text = html_to_text(text)
    elif kind == "rtf":
        text = rtf_to_text(text)
    elif kind == "ipynb":
        text, images = _ipynb(text, work)
    elif kind == "svg":
        png = raster.convert_image_to_png(src, work / "svg.png")
        if png:
            doc["units"].append(image_unit(png))
            return doc
        text = " ".join(re.findall(r"<text[^>]*>(.*?)</text>", text, flags=re.S))
    size = int(cfg.get("text_page_chars", 3500))
    chunks = chunk_text(text, size)
    cap = int(cfg.get("max_text_pages_per_file", 200))
    if len(chunks) > cap:
        doc["note"] = f"very long text file: only the first {cap} pieces (about {cap * size // 1000}k characters) are read"
        chunks = chunks[:cap]
    for c in chunks:
        doc["units"].append(text_unit(c))
    for p in images:
        doc["units"].append(image_unit(p, note="embedded image"))
    if not doc["units"]:
        doc.update(status="skipped", note="no readable text")
    # Headings in markdown/html become the outline.
    for i, u in enumerate(doc["units"], 1):
        for line in (u.get("text") or "").splitlines():
            m = re.match(r"^(#{1,3})\s+(.+)", line)
            if m:
                doc["outline"].append({"title": m.group(2).strip()[:120], "page": i, "level": len(m.group(1))})
    return doc


def _ipynb(text: str, work: Path):
    images: List[Path] = []
    try:
        nb = json.loads(text)
    except ValueError:
        return text, images
    parts = []
    for k, cell in enumerate(nb.get("cells", [])):
        src = cell.get("source", "")
        src = "".join(src) if isinstance(src, list) else str(src)
        if cell.get("cell_type") == "code":
            parts.append("```\n" + src + "\n```")
            for o in cell.get("outputs", []) or []:
                data = o.get("data", {}) if isinstance(o, dict) else {}
                if "image/png" in data:
                    try:
                        raw = base64.b64decode("".join(data["image/png"]) if isinstance(data["image/png"], list) else data["image/png"])
                    except (ValueError, TypeError):
                        continue
                    if len(raw) >= MIN_EMBEDDED_IMAGE_BYTES:
                        p = work / "media" / f"cell-{k}.png"
                        p.parent.mkdir(parents=True, exist_ok=True)
                        p.write_bytes(raw)
                        images.append(p)
                txt = data.get("text/plain") or o.get("text") if isinstance(o, dict) else None
                if txt:
                    txt = "".join(txt) if isinstance(txt, list) else str(txt)
                    parts.append("Output:\n" + txt[:2000])
        else:
            parts.append(src)
    return tidy("\n\n".join(parts)), images


def convert_epub(entry: dict, work: Path, cfg: dict) -> dict:
    doc = new_doc(entry, converter="epub")
    try:
        z = zipfile.ZipFile(entry["path"])
    except (zipfile.BadZipFile, OSError) as exc:
        doc.update(status="unreadable", note=f"damaged EPUB ({exc})")
        return doc
    size = int(cfg.get("text_page_chars", 3500))
    with z:
        names = z.namelist()
        order: List[str] = []
        try:
            container = ET.fromstring(z.read("META-INF/container.xml"))
            opf_path = next(e.get("full-path") for e in container.iter() if e.tag.endswith("rootfile"))
            opf = ET.fromstring(z.read(opf_path))
            base = opf_path.rsplit("/", 1)[0] + "/" if "/" in opf_path else ""
            manifest = {e.get("id"): e.get("href") for e in opf.iter() if e.tag.endswith("item")}
            for e in opf.iter():
                if e.tag.endswith("itemref") and e.get("idref") in manifest:
                    order.append(base + manifest[e.get("idref")])
        except (KeyError, ET.ParseError, StopIteration, TypeError):
            order = sorted(n for n in names if n.lower().endswith((".xhtml", ".html", ".htm")))
        for n in order:
            try:
                txt = html_to_text(decode_bytes(z.read(n)))
            except KeyError:
                continue
            for c in chunk_text(txt, size):
                doc["units"].append(text_unit(c))
        for p in _media(z, ("",), work / "media"):
            doc["units"].append(image_unit(p, note="embedded image"))
    if not doc["units"]:
        doc.update(status="skipped", note="no readable content in EPUB")
    return doc


# ------------------------------------------------------------------- images


def convert_image(entry: dict, work: Path) -> dict:
    doc = new_doc(entry, converter="image")
    src = Path(entry["path"])
    kind = entry.get("kind")
    if kind == "tiff" or (kind == "gif" and src.stat().st_size > 200_000):
        pages = raster.split_multipage_image(src, work / "pages")
        if len(pages) > 1:
            for p in pages:
                doc["units"].append(image_unit(p))
            return doc
    if kind in ("png", "jpeg", "gif", "webp"):
        doc["units"].append(image_unit(src))
        return doc
    png = raster.convert_image_to_png(src, work / "converted.png")
    if png:
        doc["units"].append(image_unit(png))
        doc["converter"] = "image→png"
        return doc
    hint = {"heif": "install Pillow with pillow-heif, ImageMagick, or libheif's heif-convert",
            "avif": "install ImageMagick or libheif"}.get(kind, "install Pillow or ImageMagick")
    doc.update(status="unreadable", note=f"{kind} image needs a converter: {hint}")
    return doc


# ----------------------------------------------------------------- dispatch


def to_pdf_with(entry: dict, work: Path, label: str) -> dict:
    src = Path(entry["path"])
    kind = entry.get("kind")
    work.mkdir(parents=True, exist_ok=True)
    out = work / "converted.pdf"
    ok = out.exists() and out.stat().st_size > 0
    hint = ""
    if not ok and kind == "djvu":
        t = which("ddjvu")
        hint = "install DjVuLibre (ddjvu)"
        if t:
            ok = run([t, "-format=pdf", str(src), str(out)], timeout=1800).ok
    elif not ok and kind in ("ps", "eps"):
        t = which("ps2pdf")
        hint = "install Ghostscript (ps2pdf)"
        if t:
            args = [t, "-dEPSCrop", str(src), str(out)] if kind == "eps" else [t, str(src), str(out)]
            ok = run(args, timeout=1800).ok
    elif not ok and kind == "xps":
        t = which("mutool")
        hint = "install MuPDF (mutool)"
        if t:
            ok = run([t, "convert", "-o", str(out), str(src)], timeout=1800).ok
    if ok and out.exists():
        d = convert_pdf(entry, work / "pdf", source_path=out, kind=kind)
        d["converter"] = label
        return d
    d = new_doc(entry)
    d.update(status="unreadable", note=f"{kind} file needs a converter: {hint}")
    return d


AV_NOTE = "audio/video is not processed in this version (a transcript or slides of it can be added to the pile instead)"


def convert(entry: dict, work: Path, cfg: dict) -> dict:
    kind = entry.get("kind") or "unknown"
    work.mkdir(parents=True, exist_ok=True)
    try:
        if kind == "pdf":
            return convert_pdf(entry, work)
        if kind in ("docx", "pptx", "xlsx", "odf", "doc", "ppt", "xls", "ole", "msg"):
            return convert_office(entry, work, cfg)
        if kind == "epub":
            return convert_epub(entry, work, cfg)
        if kind in ("text", "html", "rtf", "ipynb", "svg"):
            return convert_text(entry, work, cfg)
        if raster_kind(kind):
            return convert_image(entry, work)
        if kind in ("djvu", "ps", "eps", "xps"):
            return to_pdf_with(entry, work, f"{kind}→pdf")
        d = new_doc(entry)
        if kind in ("audio", "video"):
            d.update(status="unreadable", note=AV_NOTE)
        elif kind == "corrupt-zip":
            d.update(status="unreadable", note="damaged zip-based file")
        else:
            d.update(status="unreadable", note=f"unrecognised file type ({kind})")
        return d
    except Exception as exc:  # noqa: BLE001 - one bad file must never stop the pile
        d = new_doc(entry)
        d.update(status="unreadable", note=f"converter crashed: {type(exc).__name__}: {exc}"[:300])
        return d


def raster_kind(kind: str) -> bool:
    return kind in ("png", "jpeg", "gif", "webp", "tiff", "bmp", "heif", "avif", "jp2", "ico", "pnm")
