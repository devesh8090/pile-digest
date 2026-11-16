"""Small shared helpers: atomic writes, hashing, subprocess, tool discovery."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

CHUNK = 1 << 20


# A top-level folder with this name inside a pile is never read as part of the
# pile, so derived work written there (notes, summaries) is not ingested.
DERIVED_DIRNAME = "pile-digest"


class PileDigestError(Exception):
    """An error with a message meant for the user (no traceback needed)."""


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def eprint(*args: Any) -> None:
    print(*args, file=sys.stderr)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(CHUNK)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8", "surrogatepass"))


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8", "surrogatepass"))


def write_json(path: Path, obj: Any) -> None:
    atomic_write_text(path, json.dumps(obj, indent=1, ensure_ascii=False, sort_keys=True) + "\n")


def read_json(path: Path, default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as exc:
        # A corrupt state file must never crash the tool silently or be
        # overwritten blindly: keep a copy and start from the default.
        backup = Path(str(path) + f".corrupt-{int(time.time())}")
        try:
            shutil.copy2(path, backup)
        except OSError:
            pass
        eprint(f"warning: {path} was unreadable ({exc}); a copy was saved to {backup}")
        return default


def append_jsonl(path: Path, obj: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(obj, ensure_ascii=False, sort_keys=True) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())


def read_jsonl(path: Path) -> list:
    out = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # a torn last line after a crash
    except FileNotFoundError:
        pass
    return out


_WHICH_CACHE: dict = {}


def which(name: str) -> Optional[str]:
    """shutil.which with a cache and an env override (PILE_DIGEST_<NAME>_BIN)."""
    key = "PILE_DIGEST_" + name.upper().replace("-", "_") + "_BIN"
    override = os.environ.get(key)
    if override:
        return override if (os.path.isfile(override) or shutil.which(override)) else None
    if name not in _WHICH_CACHE:
        _WHICH_CACHE[name] = shutil.which(name)
    return _WHICH_CACHE[name]


def first_tool(names: Iterable[str]) -> Optional[str]:
    for n in names:
        p = which(n)
        if p:
            return p
    return None


class RunResult:
    def __init__(self, returncode: int, stdout: bytes, stderr: bytes, timed_out: bool = False):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def out(self) -> str:
        return self.stdout.decode("utf-8", "replace")

    def err(self) -> str:
        return self.stderr.decode("utf-8", "replace")


def run(
    cmd: Sequence[str],
    *,
    input_bytes: Optional[bytes] = None,
    timeout: float = 300,
    cwd: Optional[Path] = None,
    env: Optional[dict] = None,
) -> RunResult:
    """Run a command (argument list, never a shell string) and capture output."""
    try:
        p = subprocess.run(
            list(cmd),
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            cwd=str(cwd) if cwd else None,
            env=env,
        )
        return RunResult(p.returncode, p.stdout, p.stderr)
    except subprocess.TimeoutExpired as exc:
        return RunResult(-1, exc.stdout or b"", exc.stderr or b"", timed_out=True)
    except (FileNotFoundError, PermissionError, OSError) as exc:
        return RunResult(-2, b"", str(exc).encode())


def free_bytes(path: Path) -> int:
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    try:
        return shutil.disk_usage(str(p)).free
    except OSError:
        return 1 << 62


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def human_tokens(n: float) -> str:
    n = float(n)
    if n >= 1e6:
        return f"{n / 1e6:.2f}M"
    if n >= 1e3:
        return f"{n / 1e3:.1f}k"
    return f"{n:.0f}"


def is_writable_dir(path: Path) -> bool:
    path = Path(path)
    if not path.is_dir():
        return False
    try:
        fd, tmp = tempfile.mkstemp(prefix=".pd-write-test-", dir=str(path))
        os.close(fd)
        os.unlink(tmp)
        return True
    except OSError:
        return False


def safe_name(text: str, limit: int = 60) -> str:
    keep = []
    for ch in text:
        if ch.isalnum() or ch in "-_.":
            keep.append(ch)
        elif ch in " /\\:":
            keep.append("-")
    s = "".join(keep).strip("-._") or "pile"
    while "--" in s:
        s = s.replace("--", "-")
    return s[:limit]
