"""A portable per-pile lock so two runs never work on the same pile at once."""

from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path

from .util import PileDigestError

STALE_AFTER_S = 12 * 3600


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class PileLock:
    def __init__(self, path: Path, what: str = "this pile"):
        self.path = Path(path)
        self.what = what
        self.held = False

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _is_stale(self, info: dict) -> bool:
        started = float(info.get("started", 0) or 0)
        if info.get("host") == socket.gethostname():
            return not _pid_alive(int(info.get("pid", -1) or -1))
        return time.time() - started > STALE_AFTER_S

    def acquire(self) -> "PileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"pid": os.getpid(), "host": socket.gethostname(), "started": time.time()})
        for _ in range(2):
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                info = self._read()
                if self._is_stale(info):
                    try:
                        os.unlink(self.path)
                    except FileNotFoundError:
                        pass
                    continue
                raise PileDigestError(
                    f"{self.what} is busy: another pile-digest run (pid {info.get('pid')}) holds "
                    f"{self.path}. Wait for it to finish, or delete that file if you are sure it is not running."
                )
            with os.fdopen(fd, "w") as f:
                f.write(payload)
            self.held = True
            return self
        raise PileDigestError(f"could not take the lock {self.path}")

    def release(self) -> None:
        if self.held:
            try:
                os.unlink(self.path)
            except FileNotFoundError:
                pass
            self.held = False

    def __enter__(self) -> "PileLock":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()
