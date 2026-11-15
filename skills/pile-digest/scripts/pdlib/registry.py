"""The registry of known piles, and how piles are found again after moves."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional

from . import config
from .lock import PileLock
from .util import PileDigestError, now_iso, read_json, safe_name, sha256_text, write_json

MARKER = ".pile-digest-id"
LOOSE_MARKER = ".pile-digest-loose-id"
REGISTRY_VERSION = 1


def registry_path() -> Path:
    return config.state_root() / "registry.json"


def pile_dir(pile_id: str) -> Path:
    return config.state_root() / "piles" / pile_id


def _empty() -> dict:
    return {"version": REGISTRY_VERSION, "piles": {}}


def load() -> dict:
    data = read_json(registry_path(), None)
    if not isinstance(data, dict) or "piles" not in data:
        data = _empty()
    return data


def save(data: dict) -> None:
    write_json(registry_path(), data)


class _RegistryLock(PileLock):
    def __init__(self) -> None:
        super().__init__(config.state_root() / "registry.lock", "the pile registry")


def _norm(path: Path) -> Path:
    return Path(os.path.realpath(os.path.expanduser(str(path))))


def marker_name(mode: str) -> str:
    return LOOSE_MARKER if mode == "loose" else MARKER


def read_marker(root: Path, mode: str = "tree") -> Optional[str]:
    try:
        text = (Path(root) / marker_name(mode)).read_text(encoding="utf-8").strip()
        return text.split()[0] if text else None
    except (OSError, IndexError):
        return None


def write_marker(root: Path, pile_id: str, mode: str) -> bool:
    try:
        (Path(root) / marker_name(mode)).write_text(
            pile_id + "\n# pile-digest uses this file to recognise this folder after it is moved. Safe to delete.\n",
            encoding="utf-8",
        )
        return True
    except OSError:
        return False


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def find(ref: str, data: Optional[dict] = None) -> Optional[dict]:
    """Find a pile by id, unique name, or path."""
    data = data or load()
    piles = data["piles"]
    if ref in piles:
        return piles[ref]
    by_name = [p for p in piles.values() if p.get("name") == ref]
    if len(by_name) == 1:
        return by_name[0]
    try:
        root = _norm(Path(ref))
    except (OSError, ValueError):
        root = None
    if root is not None:
        for p in piles.values():
            if Path(p["root"]) == root and p.get("mode", "tree") == "tree":
                return p
        mid = read_marker(root) if root.is_dir() else None
        if mid and mid in piles:
            return piles[mid]
    return None


def require(ref: str) -> dict:
    p = find(ref)
    if not p:
        raise PileDigestError(f"no known pile matches '{ref}'. Run `pd.py list` to see registered piles.")
    return p


def register(path: Path, *, mode: str = "tree", name: Optional[str] = None, force: bool = False) -> dict:
    root = _norm(path)
    if not root.is_dir():
        raise PileDigestError(f"{root} is not a folder")
    if root == Path(root.anchor) or root == _norm(Path.home()):
        if not force:
            raise PileDigestError(
                f"refusing to treat {root} as a pile (it is a filesystem root or your home folder). "
                "Point at the folder that holds the documents, or pass --force."
            )
    if _is_within(root, _norm(config.state_root())):
        raise PileDigestError("that folder is inside pile-digest's own state folder")
    with _RegistryLock():
        data = load()
        piles = data["piles"]
        # Already known (same root), or moved (marker matches a known id).
        for p in piles.values():
            if Path(p["root"]) == root and p.get("mode", "tree") == mode:
                return p
        mid = read_marker(root, mode)
        if mid and mid in piles:
            p = piles[mid]
            p["root"] = str(root)
            p["missing"] = False
            p["moved_at"] = now_iso()
            save(data)
            return p
        if mode == "tree" and not force:
            for p in piles.values():
                if p.get("mode", "tree") != "tree":
                    continue
                other = Path(p["root"])
                if _is_within(root, other):
                    raise PileDigestError(
                        f"{root} is inside the known pile '{p['name']}' ({other}). Its files are already "
                        "tracked there. Pass --force to track it separately as well."
                    )
                if _is_within(other, root):
                    raise PileDigestError(
                        f"{root} contains the known pile '{p['name']}' ({other}). Pass --force to track "
                        f"the bigger folder too, or `pd.py forget {p['id']}` first."
                    )
        base = name or (root.name + (" (loose files)" if mode == "loose" else ""))
        pid = safe_name(base, 40).lower() + "-" + sha256_text(str(root) + mode + now_iso())[:6]
        display = base
        taken = {p.get("name") for p in piles.values()}
        n = 2
        while display in taken:
            display = f"{base} ({n})"
            n += 1
        entry = {
            "id": pid,
            "name": display,
            "root": str(root),
            "mode": mode,
            "added": now_iso(),
            "last_scan": None,
            "missing": False,
            "marker": write_marker(root, pid, mode),
        }
        piles[pid] = entry
        save(data)
        pile_dir(pid).mkdir(parents=True, exist_ok=True)
        return entry


def update(pile_id: str, **fields) -> dict:
    with _RegistryLock():
        data = load()
        if pile_id not in data["piles"]:
            raise PileDigestError(f"unknown pile {pile_id}")
        data["piles"][pile_id].update(fields)
        save(data)
        return data["piles"][pile_id]


def relocate_missing(search_roots: list) -> list:
    """For piles whose folder vanished, look for their marker under search roots."""
    data = load()
    moved = []
    missing = {p["id"]: p for p in data["piles"].values() if not Path(p["root"]).is_dir()}
    if not missing:
        return moved
    candidates = []
    for sr in search_roots:
        sr = Path(sr)
        if not sr.is_dir():
            continue
        candidates.append(sr)
        try:
            for child in sr.iterdir():
                if child.is_dir() and not child.name.startswith("."):
                    candidates.append(child)
        except OSError:
            continue
    for c in candidates:
        for mode in ("tree", "loose"):
            mid = read_marker(c, mode)
            if mid in missing:
                moved.append((mid, str(_norm(c))))
    if moved:
        with _RegistryLock():
            data = load()
            for mid, new_root in moved:
                data["piles"][mid]["root"] = new_root
                data["piles"][mid]["missing"] = False
                data["piles"][mid]["moved_at"] = now_iso()
            save(data)
    return moved


def forget(pile_id: str) -> dict:
    with _RegistryLock():
        data = load()
        p = data["piles"].pop(pile_id, None)
        if not p:
            raise PileDigestError(f"unknown pile {pile_id}")
        save(data)
    shutil.rmtree(pile_dir(pile_id), ignore_errors=True)
    marker = Path(p["root"]) / marker_name(p.get("mode", "tree"))
    try:
        if read_marker(Path(p["root"]), p.get("mode", "tree")) == pile_id:
            marker.unlink()
    except OSError:
        pass
    return p
