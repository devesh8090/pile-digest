"""Unit tests for the pure-Python parts (no external tools needed)."""

import unittest
import zipfile
from pathlib import Path

import fixtures
from helpers import TempEnv

from pdlib import config, inventory, parse, raster, registry, sniff, textutil
from pdlib.util import PileDigestError


class TestSniff(TempEnv):
    def test_kinds(self):
        d = self.tmp
        fixtures.write_pdf(d / "a.pdf", [fixtures.Page(["hello"])])
        fixtures.write_png(d / "b.png", 50, 60)
        fixtures.write_docx(d / "c.docx", [("", "x")])
        fixtures.write_pptx(d / "d.pptx", ["s"])
        fixtures.write_epub(d / "e.epub", ["x"])
        fixtures.write_zip(d / "f.zip", {"x.txt": b"x"})
        fixtures.write_eml(d / "g.eml", "body")
        fixtures.write_ipynb(d / "h.ipynb", "m", "c")
        (d / "i.txt").write_text("plain text ünïcode")
        (d / "j.html").write_text("<!doctype html><html><body>x</body></html>")
        (d / "k.rtf").write_text("{\\rtf1 hello}")
        (d / "l.bin").write_bytes(bytes(range(256)) * 4)
        (d / "m.txt").write_bytes(b"")
        (d / "n.mp3").write_bytes(b"ID3" + b"\0" * 50)
        (d / "renamed.dat").write_bytes((d / "a.pdf").read_bytes())
        want = {"a.pdf": "pdf", "b.png": "png", "c.docx": "docx", "d.pptx": "pptx", "e.epub": "epub",
                "f.zip": "zip", "g.eml": "eml", "h.ipynb": "ipynb", "i.txt": "text", "j.html": "html",
                "k.rtf": "rtf", "l.bin": "unknown", "m.txt": "empty", "n.mp3": "audio", "renamed.dat": "pdf"}
        for name, kind in want.items():
            self.assertEqual(sniff.sniff(d / name), kind, name)

    def test_text_message_not_email(self):
        p = self.tmp / "message.txt"
        p.write_text("From: a\nTo: b\nSubject: c\nDate: d\n\nbody")
        self.assertEqual(sniff.sniff(p), "text")


class TestInventory(TempEnv):
    def cfg(self):
        return config.load()

    def test_junk_and_duplicates_and_archive_safety(self):
        root = self.tmp / "pile"
        root.mkdir()
        (root / ".DS_Store").write_bytes(b"x")
        (root / "~$lock.docx").write_bytes(b"x")
        (root / ".git").mkdir()
        (root / ".git" / "obj").write_bytes(b"x")
        (root / "a.txt").write_text("same")
        (root / "b.txt").write_text("same")
        (root / "empty.txt").write_bytes(b"")
        fixtures.write_zip(root / "z.zip", {"../../escape.txt": b"bad", "/abs.txt": b"abs", "ok/c.txt": b"fine"})
        inv = inventory.build(root, mode="tree", cfg=self.cfg(), state=self.tmp / "st")
        files = inv["files"]
        self.assertNotIn(".DS_Store", files)
        self.assertNotIn("~$lock.docx", files)
        self.assertFalse(any(k.startswith(".git") for k in files))
        self.assertEqual(files["b.txt"]["status"], "duplicate-file")
        self.assertEqual(files["empty.txt"]["status"], "skipped")
        self.assertEqual(files["z.zip"]["status"], "container")
        members = [k for k in files if k.startswith("z.zip!/")]
        self.assertIn("z.zip!/ok/c.txt", members)
        for k in members:
            real = Path(files[k]["path"]).resolve()
            self.assertTrue(str(real).startswith(str((self.tmp / "st").resolve())), real)
        self.assertFalse((self.tmp / "escape.txt").exists())

    def test_encrypted_zip_and_depth(self):
        root = self.tmp / "pile"
        root.mkdir()
        # A zip whose member claims encryption.
        p = root / "enc.zip"
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("x.txt", b"x")
        data = bytearray(p.read_bytes())
        data[6] |= 0x1  # local header flag
        cd = data.rfind(b"PK\x01\x02")
        data[cd + 8] |= 0x1  # central directory flag
        p.write_bytes(bytes(data))
        inner = fixtures.write_zip(self.tmp / "l3.zip", {"deep.txt": b"deep"}).read_bytes()
        l2 = fixtures.write_zip(self.tmp / "l2.zip", {"l3.zip": inner}).read_bytes()
        l1 = fixtures.write_zip(self.tmp / "l1.zip", {"l2.zip": l2}).read_bytes()
        fixtures.write_zip(root / "nest.zip", {"l1.zip": l1})
        cfg = self.cfg()
        cfg["archive_max_depth"] = 2
        inv = inventory.build(root, mode="tree", cfg=cfg, state=self.tmp / "st")
        f = inv["files"]
        self.assertEqual(f["enc.zip"]["status"], "unreadable")
        self.assertIn("password", f["enc.zip"]["note"])
        deep = [k for k, v in f.items() if v.get("status") == "unreadable" and "nested" in v.get("note", "")]
        self.assertTrue(deep)

    def test_quick_diff(self):
        root = self.tmp / "pile"
        root.mkdir()
        (root / "a.txt").write_text("a")
        (root / "b.txt").write_text("b")
        inv = inventory.build(root, mode="tree", cfg=self.cfg(), state=self.tmp / "st")
        (root / "c.txt").write_text("c")
        (root / "a.txt").write_text("changed!")
        (root / "b.txt").unlink()
        d = inventory.quick_diff(root, mode="tree", cfg=self.cfg(), previous=inv)
        self.assertEqual(d["new"], ["c.txt"])
        self.assertEqual(d["removed"], ["b.txt"])
        self.assertEqual(d["changed"], ["a.txt"])

    def test_ignore_file_and_loose_mode(self):
        root = self.tmp / "pile"
        (root / "sub").mkdir(parents=True)
        (root / "keep.txt").write_text("k")
        (root / "skip.log").write_text("s")
        (root / "sub" / "inner.txt").write_text("i")
        (root / ".pile-digest-ignore").write_text("*.log\n")
        inv = inventory.build(root, mode="tree", cfg=self.cfg(), state=self.tmp / "st")
        self.assertEqual(sorted(inv["files"]), ["keep.txt", "sub/inner.txt"])
        inv2 = inventory.build(root, mode="loose", cfg=self.cfg(), state=self.tmp / "st2")
        self.assertEqual(sorted(inv2["files"]), ["keep.txt"])


class TestParse(unittest.TestCase):
    def block(self, pid, kind="transcription", conf="high", content="text", flags="- none"):
        return (f"=== PAGE {pid} ===\nKIND: {kind}\nCONFIDENCE: {conf}\nSUMMARY: s\nTOPICS: a, b\nFLAGS:\n{flags}\n"
                f"CONTENT:\n{content}\n=== END PAGE {pid} ===")

    def test_ok_and_missing(self):
        txt = self.block("p1") + "\n" + self.block("p2", content="$$x$$ [[ILLEGIBLE: y?]]")
        got, probs = parse.parse_blocks(txt, ["p1", "p2", "p3"])
        self.assertEqual(set(got), {"p1", "p2"})
        self.assertTrue(any("p3" in p for p in probs))
        self.assertEqual(got["p2"]["markers"][0]["kind"], "illegible")
        self.assertEqual(parse.needs_escalation(got["p2"]), "illegible parts")
        self.assertIsNone(parse.needs_escalation(got["p1"]))

    def test_content_with_delimiter_like_lines(self):
        content = "KIND: not a field\n=== PAGE other ===\nreal text"
        got, _ = parse.parse_blocks(self.block("p1", content=content), ["p1"])
        self.assertIn("real text", got["p1"]["content"])
        self.assertEqual(got["p1"]["kind"], "transcription")

    def test_bad_kind_and_low_conf(self):
        got, probs = parse.parse_blocks(self.block("p1", kind="poem"), ["p1"])
        self.assertFalse(got)
        got, _ = parse.parse_blocks(self.block("p1", conf="low"), ["p1"])
        self.assertEqual(parse.needs_escalation(got["p1"]), "low confidence")

    def test_spotcheck(self):
        txt = "=== PAGE p1 ===\nVERDICT: mismatch\nDIFFERENCES:\n- difference: sign\nCORRECTED:\nfixed\n=== END PAGE p1 ==="
        got, _ = parse.parse_blocks(txt, ["p1"], mode="spotcheck")
        self.assertEqual(got["p1"]["verdict"], "mismatch")
        self.assertEqual(got["p1"]["corrected"], "fixed")


class TestText(unittest.TestCase):
    def test_chunk(self):
        text = ("para one. " * 50 + "\n\n") * 10
        chunks = textutil.chunk_text(text, 800)
        self.assertTrue(all(len(c) <= 800 for c in chunks))
        self.assertGreater(len(chunks), 5)
        self.assertEqual(textutil.chunk_text("", 100), [])

    def test_quality_and_math(self):
        self.assertGreater(textutil.quality("A normal sentence with words."), 0.8)
        self.assertLess(textutil.quality("(cid:12)(cid:13)(cid:14)"), 0.3)
        self.assertGreater(textutil.math_density("∂u/∂t = α ∇²u\nx = y + z\n∫ f dx"), 0.12)

    def test_rtf_and_html(self):
        self.assertIn("Hello world", textutil.rtf_to_text(r"{\rtf1{\fonttbl{\f0 Arial;}}\f0 Hello \b world\b0\par}"))
        self.assertIn("# Title", textutil.html_to_text("<h1>Title</h1><script>bad()</script><p>x</p>"))
        self.assertNotIn("bad()", textutil.html_to_text("<script>bad()</script>"))


class TestRaster(TempEnv):
    def test_png_dims_and_pgm(self):
        p = fixtures.write_png(self.tmp / "x.png", 123, 77)
        self.assertEqual(raster.image_dims(p), (123, 77))
        g = raster.parse_pnm(b"P5\n3 2\n255\n" + bytes([0, 255, 255, 255, 255, 255]))
        self.assertEqual((g.w, g.h), (3, 2))
        st = raster.stats(g)
        self.assertGreater(st["ink"], 0)

    def test_target_dims(self):
        w, h = raster.target_dims(612, 792, 1568)
        self.assertLessEqual(max(w, h), 1568)
        self.assertLessEqual(w * h, raster.MAX_PIXELS)

    def test_mismatch(self):
        a = raster.Gray(10, 10, bytes([255] * 90 + [0] * 10))
        b = raster.Gray(10, 10, bytes([255] * 90 + [0] * 10))
        c = raster.Gray(10, 10, bytes([0] * 10 + [255] * 90))
        self.assertEqual(raster.pixel_mismatch(a, b), 0.0)
        self.assertGreater(raster.pixel_mismatch(a, c), 0.5)


class TestRegistry(TempEnv):
    def test_register_nested_move_forget(self):
        a = self.tmp / "piles" / "A"
        (a / "inner").mkdir(parents=True)
        (a / "x.txt").write_text("x")
        e = registry.register(a)
        self.assertEqual(registry.register(a)["id"], e["id"])  # idempotent
        with self.assertRaises(PileDigestError):
            registry.register(a / "inner")
        with self.assertRaises(PileDigestError):
            registry.register(self.tmp / "piles")
        moved = self.tmp / "elsewhere" / "A2"
        moved.parent.mkdir()
        a.rename(moved)
        found = registry.relocate_missing([str(moved.parent)])
        self.assertEqual(found[0][0], e["id"])
        self.assertEqual(registry.find(e["id"])["root"], str(moved.resolve()))
        registry.forget(e["id"])
        self.assertIsNone(registry.find(e["id"]))
        self.assertFalse((moved / registry.MARKER).exists())
        self.assertTrue((moved / "x.txt").exists())

    def test_refuse_home(self):
        import os

        with self.assertRaises(PileDigestError):
            registry.register(Path(os.path.expanduser("~")))


if __name__ == "__main__":
    unittest.main()
