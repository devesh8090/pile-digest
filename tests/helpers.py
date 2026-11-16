import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "pile-digest" / "scripts"
FAKE = Path(__file__).resolve().parent / "fake_claude.py"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

HAVE_POPPLER = bool(shutil.which("pdftoppm") and shutil.which("pdftotext") and shutil.which("pdfinfo"))
needs_poppler = unittest.skipUnless(HAVE_POPPLER, "poppler-utils not installed")


class TempEnv(unittest.TestCase):
    """Each test gets its own state folder, config and fake claude CLI."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pd-test-"))
        self._env = dict(os.environ)
        os.environ["PILE_DIGEST_HOME"] = str(self.tmp / "state")
        os.environ["PILE_DIGEST_CLAUDE_BIN"] = str(FAKE)
        os.environ["FAKE_CLAUDE_LOG"] = str(self.tmp / "calls.log")
        os.environ.pop("FAKE_CLAUDE_BEHAVIOUR", None)
        os.environ.pop("FAKE_CLAUDE_UTIL", None)
        os.environ.pop("FAKE_CLAUDE_EXTRA_TOKENS", None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def calls(self):
        import json

        try:
            return [json.loads(x) for x in (self.tmp / "calls.log").read_text().splitlines() if x.strip()]
        except FileNotFoundError:
            return []

    def pd(self, *args):
        """Run the CLI in-process; return (exit code, stdout text)."""
        import contextlib
        import io

        import pd  # noqa: WPS433

        buf = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            code = pd.main([str(a) for a in args])
        return code, buf.getvalue() + err.getvalue()
