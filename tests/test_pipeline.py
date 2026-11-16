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
            pages.append(fixtures.Page(["Course Title", f"{2 * k + 1}"]))   # same title, only the page number differs
            pages.append(fixtures.Page(fixtures.text_lines(10 + k)))
        for k in range(3):
            pages.append(fixtures.Page([f"Part {k + 1}", "Course"]))         # different section markers: all kept
        fixtures.write_pdf(root / "s.pdf", pages, outline=[("One", 1), ("Two", 5)])
        registry.register(root)
        p = open_pile("pile")
        p.prepare()
        cls = [pg["cls"] for pg in p.pages()]
        self.assertEqual(cls[0], "title")
        self.assertEqual(cls[:8].count("repeated-title") + cls[:8].count("duplicate"), 3)
        self.assertNotIn("repeated-title", cls[8:])
        self.assertNotIn("duplicate", cls[8:])
        outline = p.index()["docs"][0]["outline"]
        self.assertEqual([o["page"] for o in outline], [1, 5])


@needs_poppler
class TestPipeline(TempEnv):
    def make(self):
        root = fixtures.mixed_pile(self.tmp / "pile")
        code, out = self.pd("scan", root, "--register")
        self.assertEqual(code, 0, out)
        return root

    def test_full_flow_and_incremental(self):
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
        # Add a file: only the new page is read.
        before = len(self.calls())
        fixtures.write_pdf(root / "week 2" / "new scan.pdf", [fixtures.Page(image_seed=99)])
        code, out = self.pd("scan")
        self.assertIn("changed", out)
        self.assertEqual(self.pd("prepare", "pile")[0], 0)
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

    def test_read_only_pile_works(self):
        root = self.tmp / "ro"
        root.mkdir()
        fixtures.write_pdf(root / "a.pdf", [fixtures.Page(["hello world text"])])
        self.pd("add", root)
        os.chmod(root, 0o555)
        try:
            if os.access(root, os.W_OK):
                self.skipTest("running as root: permissions not enforced")
            p = open_pile("ro")
            p.prepare()
            self.assertEqual(len(p.pages()), 1)
        finally:
            os.chmod(root, 0o755)



@needs_poppler
class TestSpendAndQuality(TempEnv):
    """Behaviour added after the first field report."""

    def scan_pile(self, n=4, seed=10):
        root = self.tmp / "pile"
        root.mkdir()
        fixtures.write_pdf(root / "scan.pdf", [fixtures.Page(image_seed=seed + k) for k in range(n)])
        self.pd("scan", root, "--register")
        self.pd("prepare", "pile")
        return root

    def test_run_stops_when_spend_passes_the_approval(self):
        self.scan_pile(n=12)
        self.assertEqual(self.pd("approve", "pile")[0], 0)
        os.environ["FAKE_CLAUDE_EXTRA_TOKENS"] = "200000"     # every call costs far more than estimated
        code, out = self.pd("run", "pile")
        self.assertEqual(code, 0, out)
        self.assertIn("STOPPED", out)
        self.assertIn("approved", out)
        self.assertIn("ASK THE USER", out)
        n = len(self.calls())
        self.assertLessEqual(n, 3)
        # Running again without a new approval stops again at once.
        self.pd("run", "pile")
        self.assertEqual(len(self.calls()), n)
        # Approving more lets it finish.
        os.environ["FAKE_CLAUDE_EXTRA_TOKENS"] = "0"
        self.assertEqual(self.pd("approve", "pile", "--add", "10000000")[0], 0)
        code, out = self.pd("run", "pile")
        self.assertNotIn("STOPPED", out)
        self.assertEqual(open_pile("pile").status()["pages_without_result"], 0)

    def test_workers_run_without_prompt_caching(self):
        self.scan_pile(n=1)
        self.pd("approve", "pile")
        self.pd("run", "pile", "--stages", "bulk")
        self.assertTrue(self.calls())
        self.assertTrue(all(c["env_no_cache"] == "1" for c in self.calls()))

    def test_no_image_reply_is_retried_at_the_same_level(self):
        self.scan_pile(n=3)
        self.pd("approve", "pile")
        os.environ["FAKE_CLAUDE_BEHAVIOUR"] = "noimage"      # multi-page calls claim no image arrived
        self.pd("run", "pile", "--stages", "bulk")
        res = open_pile("pile").results()
        self.assertEqual(len(res), 3)
        for r in res.values():
            self.assertEqual(r["level"], "bulk")              # retried one page per call, not escalated
            self.assertFalse(r.get("no_image"))
            self.assertNotIn("did not arrive", r["summary"])
        singles = [c for c in self.calls() if len(c["ids"]) == 1]
        self.assertEqual(len(singles), 3)

    def test_escalation_cannot_swap_in_another_pages_content(self):
        self.scan_pile(n=1)
        self.pd("approve", "pile")
        os.environ["FAKE_CLAUDE_BEHAVIOUR"] = "swap"
        self.pd("run", "pile", "--stages", "bulk,escalate")
        p = open_pile("pile")
        (pid, r), = p.results().items()
        self.assertEqual(r["level"], "bulk")                  # the earlier reading is kept
        self.assertEqual(r["status"], "needs-main")
        self.assertIn("shares almost nothing", r["review_reason"])
        self.assertTrue((p.results_dir / f"{pid}.rejected-strong.md").exists())
        self.assertIn("Zopt = 12.5", p.result_text(pid))
        strong = [c for c in self.calls() if c["model"] == "sonnet"]
        self.assertTrue(strong and all(c["earlier"] for c in strong))   # strong saw the earlier reading
        self.assertTrue(all(c["images"] == 1 for c in strong))          # full page, not tiles

    def test_default_ladder_skips_tiles(self):
        self.scan_pile(n=2)
        self.pd("approve", "pile")
        os.environ["FAKE_CLAUDE_BEHAVIOUR"] = "illegible"
        self.pd("run", "pile", "--stages", "bulk,escalate")
        models = [c["model"] for c in self.calls()]
        self.assertEqual(models.count("haiku"), 1)             # bulk only: no cheap tiles re-read
        levels = {r["level"] for r in open_pile("pile").results().values()}
        self.assertEqual(levels, {"strong"})

    def test_verdict_survives_fix_and_digest_shows_it(self):
        self.scan_pile(n=2)
        self.pd("approve", "pile")
        self.pd("run", "pile", "--stages", "bulk,digest")
        before = len(self.calls())
        self.assertEqual(self.pd("verify", "pile", "D1:1", "--verdict", "source-error", "--note", "wrong constant")[0], 0)
        fix = self.tmp / "fix.md"
        fix.write_text("# Fixed page\nCorrected reading.")
        self.assertEqual(self.pd("fix", "pile", "D1:1", "--file", fix)[0], 0)
        p = open_pile("pile")
        r = p.result(p.pages()[0]["page_id"])
        self.assertTrue(r["verified"])
        self.assertEqual(r["verifications"][0]["verdict"], "source-error")
        dg = p.digest_path.read_text()                       # refreshed for free after verify and fix
        self.assertIn("Confirmed source errors", dg)
        self.assertIn("wrong constant", dg)
        self.assertIn("corrected by main model", dg)
        self.assertEqual(len(self.calls()), before)

    def test_digest_rebuild_is_free_and_splits_when_large(self):
        from pdlib import digest

        self.scan_pile(n=3)
        self.pd("approve", "pile")
        self.pd("run", "pile")
        n = len(self.calls())
        old = digest.SPLIT_TOKENS
        digest.SPLIT_TOKENS = 50
        try:
            code, out = self.pd("--json", "digest", "pile")
        finally:
            digest.SPLIT_TOKENS = old
        self.assertEqual(code, 0, out)
        info = json.loads(out)
        self.assertTrue(info["split"])
        self.assertTrue(os.path.exists(os.path.join(info["pages_dir"], "D1.md")))
        self.assertEqual(len(self.calls()), n)               # no model calls
        code, out = self.pd("digest", "pile", "--sections")  # approved: allowed, but nothing to do for a tiny pile
        self.assertEqual(code, 0, out)

    def test_spot_checks_per_level_and_advice(self):
        self.scan_pile(n=8)
        self.pd("approve", "pile")
        self.pd("run", "pile", "--stages", "bulk")
        p = open_pile("pile")
        os.environ["FAKE_CLAUDE_BEHAVIOUR"] = "mismatch"
        engine.stage_spotcheck(engine.RunContext(p), p.pages())
        s = engine.spot_summary(p)
        self.assertIn("bulk", s["levels"])
        self.assertGreaterEqual(s["levels"]["bulk"]["checked"], 3)
        self.assertIn("--level strong", s["advice"])
        self.assertNotIn("tiles", s["advice"])
        # A fix does not erase the original verdict.
        checked = [pid for pid, r in p.results().items() if r.get("spots")]
        f = self.tmp / "f.md"
        f.write_text("fixed")
        from pdlib import digest as dg

        labels = dg.doc_labels(p)
        pg = next(x for x in p.pages() if x["page_id"] == checked[0])
        self.pd("fix", "pile", f"{labels[pg['doc_id']]}:{pg['index']}", "--file", f)
        s2 = engine.spot_summary(open_pile("pile"))
        self.assertEqual(s2["checked"], s["checked"])
        self.assertEqual(s2["corrected_since"], 1)

    def test_pilot_samples_every_document(self):
        root = self.tmp / "pile"
        root.mkdir()
        for d in range(3):
            fixtures.write_pdf(root / f"doc{d}.pdf", [fixtures.Page(image_seed=100 * d + k) for k in range(8)])
        self.pd("scan", root, "--register")
        self.pd("prepare", "pile")
        p = open_pile("pile")
        pick = engine.select_pilot(p, p.pages(), 2)
        self.assertEqual(len({x["doc_id"] for x in pick}), 3)
        self.assertGreaterEqual(len(pick), 6)

    def test_removed_output_commands(self):
        for cmd in ("profiles", "outputs", "postbuild"):
            with self.assertRaises(SystemExit):
                self.pd(cmd, "pile")

if __name__ == "__main__":
    unittest.main()
