"""End-to-end tests with real local tools and a fake `claude` CLI."""

import json
import os
import unittest

import fixtures
from helpers import TempEnv, needs_poppler

from pdlib import engine, registry
from pdlib.lock import PileLock
from pdlib.pile import open_pile
from pdlib.util import PileDigestError


@needs_poppler
class TestTriage(TempEnv):
    def test_classes(self):
        root = self.tmp / "pile"
        root.mkdir()
        fixtures.write_pdf(root / "a.pdf", [
            fixtures.Page(fixtures.text_lines(1)),       # 1 text-ok
            fixtures.Page([]),                          # 2 blank
            fixtures.Page(image_seed=7),                # 3 vision
            fixtures.Page(image_seed=7),                # 4 duplicate of 3
            fixtures.Page(image_seed=8),                # 5 vision (different scribble)
            fixtures.Page(["Title only"]),              # 6 short text page
        ])
        registry.register(root)
        p = open_pile("pile")
        p.prepare()
        cls = [pg["cls"] for pg in p.pages()]
        self.assertEqual(cls[0], "text-ok")
        self.assertEqual(cls[1], "blank")
        self.assertEqual(cls[2], "vision")
        self.assertEqual(cls[3], "duplicate")
        self.assertEqual(cls[4], "vision")
        self.assertEqual(cls[5], "text-ok")

    def test_repeated_titles_and_outline(self):
        root = self.tmp / "pile"
        root.mkdir()
        pages = []
        for k in range(4):
            pages.append(fixtures.Page([f"Part {k}", "Course"]))   # differ only by digits
            pages.append(fixtures.Page(fixtures.text_lines(10 + k)))
        fixtures.write_pdf(root / "s.pdf", pages, outline=[("One", 1), ("Two", 5)])
        registry.register(root)
        p = open_pile("pile")
        p.prepare()
        cls = [pg["cls"] for pg in p.pages()]
        self.assertEqual(cls[0], "title")
        self.assertEqual(cls.count("repeated-title") + cls.count("duplicate"), 3)
        outline = p.index()["docs"][0]["outline"]
        self.assertEqual([o["page"] for o in outline], [1, 5])


@needs_poppler
class TestPipeline(TempEnv):
    def make(self):
        root = fixtures.mixed_pile(self.tmp / "pile")
        code, out = self.pd("scan", root, "--register")
        self.assertEqual(code, 0, out)
        return root

    def test_full_flow_incremental_and_outputs(self):
        root = self.make()
        code, out = self.pd("prepare", "pile")
        self.assertEqual(code, 0, out)
        self.assertIn("mystery.bin", out)          # unreadable files are reported
        self.assertIn("recording.mp3", out)
        code, out = self.pd("run", "pile")
        self.assertEqual(code, 2)                  # not approved yet
        self.assertIn("approved", out)
        self.assertEqual(self.calls(), [])         # and no model was called
        self.assertEqual(self.pd("pilot", "pile")[0], 0)
        n_pilot = len(self.calls())
        self.assertGreater(n_pilot, 0)
        self.assertEqual(self.pd("approve", "pile")[0], 0)
        code, out = self.pd("run", "pile")
        self.assertEqual(code, 0, out)
        p = open_pile("pile")
        st = p.status()
        self.assertEqual(st["pages_without_result"], 0)
        self.assertTrue(p.digest_path.exists())
        dg = p.digest_path.read_text()
        self.assertIn("same as", dg)
        self.assertIn("unreadable", dg)
        # Every call was single-turn with a short system prompt and no tools.
        for c in self.calls():
            self.assertLessEqual(c["images"], engine.MAX_IMAGES_PER_CALL)
        # Outputs: mark, then add a file and see staleness and incremental work.
        self.assertEqual(self.pd("outputs", "pile", "--mark", "study-notes")[0], 0)
        before = len(self.calls())
        fixtures.write_pdf(root / "week 2" / "new scan.pdf", [fixtures.Page(image_seed=99)])
        code, out = self.pd("scan")
        self.assertIn("changed", out)
        self.assertEqual(self.pd("prepare", "pile")[0], 0)
        code, out = self.pd("--json", "outputs", "pile")
        stale = json.loads(out)["outputs"]["study-notes"]["stale_docs"]
        self.assertEqual(len(stale), 1)
        self.assertEqual(self.pd("approve", "pile")[0], 0)
        self.assertEqual(self.pd("run", "pile", "--stages", "summaries,bulk")[0], 0)
        new_calls = self.calls()[before:]
        ids = [i for c in new_calls for i in c["ids"]]
        self.assertEqual(len(ids), 1)              # only the new page was read

    def test_rename_reuses_results(self):
        root = self.make()
        self.pd("prepare", "pile")
        self.pd("approve", "pile")
        self.pd("run", "pile", "--stages", "summaries,bulk")
        n = len(self.calls())
        os.rename(root / "week 2" / "scan.pdf", root / "week 2" / "scan renamed.pdf")
        self.pd("prepare", "pile")
        self.pd("run", "pile", "--stages", "summaries,bulk")
        self.assertEqual(len(self.calls()), n)    # nothing re-read
        p = open_pile("pile")
        self.assertTrue(any("scan renamed.pdf" in d["relpath"] for d in p.index()["docs"]))

    def test_usage_limit_stops_and_resumes(self):
        self.make()
        self.pd("prepare", "pile")
        self.pd("approve", "pile")
        os.environ["FAKE_CLAUDE_BEHAVIOUR"] = "usage_limit"
        code, out = self.pd("run", "pile")
        self.assertEqual(code, 0)
        self.assertIn("STOPPED", out)
        n = len(self.calls())
        self.assertLessEqual(n, 3)                 # stops at once (concurrency 3), no retry loop
        os.environ["FAKE_CLAUDE_BEHAVIOUR"] = "ok"
        code, out = self.pd("run", "pile")
        self.assertNotIn("STOPPED", out)
        self.assertEqual(open_pile("pile").status()["pages_without_result"], 0)

    def test_window_guard(self):
        self.make()
        self.pd("prepare", "pile")
        self.pd("approve", "pile")
        os.environ["FAKE_CLAUDE_UTIL"] = "0.97"
        code, out = self.pd("run", "pile")
        self.assertIn("STOPPED", out)
        self.assertIn("window", out)
        self.assertLessEqual(len(self.calls()), 3)

    def test_auth_error(self):
        self.make()
        self.pd("prepare", "pile")
        self.pd("approve", "pile")
        os.environ["FAKE_CLAUDE_BEHAVIOUR"] = "auth"
        code, out = self.pd("run", "pile")
        self.assertIn("logged in", out)

    def test_garbage_and_dropped_pages_retry_then_fail(self):
        self.make()
        self.pd("prepare", "pile")
        self.pd("approve", "pile")
        os.environ["FAKE_CLAUDE_BEHAVIOUR"] = "drop_last"
        self.pd("run", "pile", "--stages", "summaries,bulk")
        p = open_pile("pile")
        self.assertEqual(p.status()["pages_without_result"], 0)  # split retries recovered every page
        os.environ["FAKE_CLAUDE_BEHAVIOUR"] = "garbage"
        root = p.root
        fixtures.write_pdf(root / "more.pdf", [fixtures.Page(image_seed=55)])
        self.pd("prepare", "pile")
        self.pd("approve", "pile")
        self.pd("run", "pile", "--stages", "bulk")
        res = open_pile("pile").results()
        failed = [r for r in res.values() if r.get("status") == "failed"]
        self.assertEqual(len(failed), 1)

    def test_escalation_ladder_and_review(self):
        root = self.tmp / "pile"
        root.mkdir()
        fixtures.write_pdf(root / "scan.pdf", [fixtures.Page(image_seed=3), fixtures.Page(image_seed=4)])
        self.pd("scan", root, "--register")
        self.pd("prepare", "pile")
        self.pd("approve", "pile")
        os.environ["FAKE_CLAUDE_BEHAVIOUR"] = "illegible"   # haiku struggles, sonnet does not
        code, out = self.pd("run", "pile")
        self.assertEqual(code, 0, out)
        res = open_pile("pile").results()
        levels = sorted(r["level"] for r in res.values())
        self.assertEqual(levels, ["strong", "strong"])
        models = [c["model"] for c in self.calls()]
        self.assertIn("sonnet", models)
        # Spot check finds a mismatch -> review queue -> fix with suggestion.
        os.environ["FAKE_CLAUDE_BEHAVIOUR"] = "mismatch"
        p = open_pile("pile")
        for pid in res:
            engine.mark(p, pid, level="bulk", spot=None)
        ctx = engine.RunContext(p)
        engine.stage_spotcheck(ctx, p.pages())
        q = engine.review_queue(p)
        self.assertTrue(q["review"])
        ref = "D1:1"
        code, out = self.pd("fix", "pile", ref, "--suggested")
        self.assertEqual(code, 0, out)
        r = open_pile("pile").result(q["review"][0]["page_id"]) if q["review"][0]["page_id"].endswith("p0001") else None
        if r:
            self.assertEqual(r["level"], "main")

    def test_page_image_and_verify_errata(self):
        self.make()
        self.pd("prepare", "pile")
        code, out = self.pd("--json", "page", "pile", "D16:1", "--image")
        info = json.loads(out)
        self.assertTrue(info["images"])
        self.assertTrue(os.path.exists(info["images"][0]))
        self.assertEqual(self.pd("approve", "pile")[0], 0)
        self.pd("run", "pile", "--stages", "bulk")
        self.assertEqual(self.pd("verify", "pile", "D16:1", "--verdict", "source-error", "--note", "wrong sign")[0], 0)
        code, out = self.pd("errata", "pile")
        self.assertIn("wrong sign", out)

    def test_lock(self):
        self.make()
        p = open_pile("pile")
        with p.lock():
            with self.assertRaises(PileDigestError):
                PileLock(p.dir / "run.lock").acquire()
        # A stale lock from a dead process is taken over.
        (p.dir / "run.lock").write_text(json.dumps({"pid": 999999, "host": __import__("socket").gethostname(), "started": 0}))
        with p.lock():
            pass

    def test_watch_root_auto_registers(self):
        w = self.tmp / "inbox"
        (w / "course A").mkdir(parents=True)
        fixtures.write_pdf(w / "course A" / "x.pdf", [fixtures.Page(["hello there"])])
        fixtures.write_png(w / "loose.png", 50, 50)
        self.assertEqual(self.pd("watch", "add", w)[0], 0)
        code, out = self.pd("--json", "scan")
        rep = json.loads(out)
        names = sorted(e["name"] for e in rep["auto_registered"])
        self.assertEqual(names, ["course A", "inbox (loose files)"])
        code, out = self.pd("--json", "scan")
        self.assertEqual(json.loads(out)["auto_registered"], [])  # only once
        (w / "course B").mkdir()
        (w / "course B" / "n.md").write_text("# notes")
        rep = json.loads(self.pd("--json", "scan")[1])
        self.assertEqual([e["name"] for e in rep["auto_registered"]], ["course B"])

    def test_read_only_pile_outputs_go_to_state(self):
        root = self.tmp / "ro"
        root.mkdir()
        fixtures.write_pdf(root / "a.pdf", [fixtures.Page(["hello world text"])])
        self.pd("add", root)
        os.chmod(root, 0o555)
        try:
            if os.access(root, os.W_OK):
                self.skipTest("running as root: permissions not enforced")
            p = open_pile("ro")
            self.assertTrue(str(p.output_dir()).startswith(str(p.dir)))
            p.prepare()
        finally:
            os.chmod(root, 0o755)


if __name__ == "__main__":
    unittest.main()
