"""Find new piles and changes in known piles. Cheap: no hashing, no models."""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional

from . import config, inventory, registry
from .util import PileDigestError, read_json

DOC_EXTS = {
    ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".tif", ".tiff", ".bmp", ".heic", ".heif", ".avif", ".jp2",
    ".doc", ".docx", ".ppt", ".pptx", ".pps", ".ppsx", ".xls", ".xlsx", ".odt", ".odp", ".ods", ".rtf", ".txt",
    ".md", ".tex", ".epub", ".djvu", ".ps", ".eps", ".xps", ".html", ".htm", ".ipynb", ".eml", ".mbox", ".zip",
    ".7z", ".rar", ".tar", ".gz", ".tgz", ".csv", ".svg",
}
CODE_MARKERS = {"package.json", "pyproject.toml", "setup.py", "Cargo.toml", "go.mod", "pom.xml", "build.gradle",
                "Makefile", "CMakeLists.txt"}


def looks_like_pile(path: Path, limit: int = 4000) -> dict:
    path = Path(path)
    docs = 0
    seen = 0
    code_markers = 0
    examples: List[str] = []
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        depth = Path(dirpath).relative_to(path).parts
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d.lower() not in inventory.SKIP_DIRS]
        if len(depth) >= 3:
            dirnames[:] = []
        for f in filenames:
            seen += 1
            if f in CODE_MARKERS:
                code_markers += 1
            if Path(f).suffix.lower() in DOC_EXTS and not f.startswith(".") and not inventory._ignored(f, f, []):
                docs += 1
                if len(examples) < 5:
                    examples.append(str(Path(dirpath, f).relative_to(path)))
            if seen >= limit:
                break
        if seen >= limit:
            break
    has_git = (path / ".git").exists()
    return {"documents": docs, "files_seen": seen, "code_repo": bool(has_git or code_markers), "examples": examples}


def _inside_known(path: Path, piles: dict) -> Optional[dict]:
    rp = Path(os.path.realpath(path))
    for p in piles.values():
        if p.get("mode", "tree") != "tree":
            continue
        root = Path(p["root"])
        try:
            rp.relative_to(root)
            return p
        except ValueError:
            continue
    return None


def scan(path: Optional[str] = None, *, cwd: Optional[str] = None, register_path: bool = False) -> dict:
    cfg = config.load()
    cwd = cwd or os.getcwd()
    watch = [str(Path(w).expanduser()) for w in cfg.get("watch_roots", [])]
    data = registry.load()
    missing_parents = [str(Path(p["root"]).parent) for p in data["piles"].values() if not Path(p["root"]).is_dir()]
    moved = registry.relocate_missing(watch + [cwd] + ([path] if path else []) + missing_parents)
    report = {"moved": moved, "auto_registered": [], "candidate": None, "focus": None, "piles": [],
              "watch_roots": watch, "warnings": []}
    # 1. Watch roots: every subfolder is a pile; loose files form one more.
    for w in watch:
        wp = Path(w)
        if not wp.is_dir():
            report["warnings"].append(f"watch folder missing: {w}")
            continue
        data = registry.load()
        try:
            children = sorted(wp.iterdir())
        except OSError as exc:
            report["warnings"].append(f"cannot list {w}: {exc}")
            continue
        loose = False
        for c in children:
            if c.name.startswith(".") or c.name.lower() in (cfg.get("output_dirname", "pile-digest"),
                                                            cfg.get("output_dirname", "pile-digest") + "-loose"):
                continue
            if c.is_dir() and not c.is_symlink():
                if _inside_known(c, data["piles"]) or registry.find(str(c), data):
                    continue
                info = looks_like_pile(c)
                if info["documents"] == 0:
                    continue
                try:
                    e = registry.register(c)
                    report["auto_registered"].append(e)
                except PileDigestError as exc:
                    report["warnings"].append(str(exc))
            elif c.is_file() and c.suffix.lower() in DOC_EXTS:
                loose = True
        if loose:
            known = any(p["root"] == os.path.realpath(wp) and p.get("mode") == "loose" for p in data["piles"].values())
            if not known:
                try:
                    report["auto_registered"].append(registry.register(wp, mode="loose"))
                except PileDigestError as exc:
                    report["warnings"].append(str(exc))
    # 2. The explicit path, or the current folder.
    data = registry.load()
    target = path or cwd
    tp = Path(target).expanduser()
    if path and not tp.is_dir():
        raise PileDigestError(f"{tp} is not a folder")
    known = registry.find(str(tp), data) or _inside_known(tp, data["piles"])
    if known:
        report["focus"] = known["id"]
    elif tp.is_dir():
        info = looks_like_pile(tp)
        home = Path(os.path.realpath(Path.home()))
        is_special = Path(os.path.realpath(tp)) in (home, Path(tp.anchor)) or \
            str(os.path.realpath(tp)).startswith(str(config.state_root()))
        if path and register_path and not is_special:
            e = registry.register(tp)
            report["auto_registered"].append(e)
            report["focus"] = e["id"]
        elif info["documents"] and not is_special:
            rp = Path(os.path.realpath(tp))
            inner = [p["name"] for p in data["piles"].values() if p.get("mode", "tree") == "tree"
                     and str(Path(p["root"])).startswith(str(rp) + os.sep)]
            if inner:
                report["warnings"].append(f"{rp} contains known pile(s): {', '.join(inner)}")
            else:
                report["candidate"] = {"path": str(rp), **info}
    # 3. Every known pile: what changed since it was last prepared?
    data = registry.load()
    for p in sorted(data["piles"].values(), key=lambda x: x.get("added") or ""):
        root = Path(p["root"])
        item = {"id": p["id"], "name": p["name"], "root": p["root"], "mode": p.get("mode", "tree")}
        if not root.is_dir():
            item["state"] = "missing"
            registry.update(p["id"], missing=True)
            report["piles"].append(item)
            continue
        pdir = registry.pile_dir(p["id"])
        inv = read_json(pdir / "inventory.json", None)
        pcfg = config.load(read_json(pdir / "config.json", {}) or {})
        diff = inventory.quick_diff(root, mode=p.get("mode", "tree"), cfg=pcfg, previous=inv)
        item.update(files=diff["files"], bytes=diff["bytes"])
        if inv is None:
            item["state"] = "new"
        elif diff["new"] or diff["changed"] or diff["removed"]:
            item["state"] = "changed"
            item.update(new=diff["new"][:50], changed=diff["changed"][:50], removed=diff["removed"][:50],
                        n_new=len(diff["new"]), n_changed=len(diff["changed"]), n_removed=len(diff["removed"]))
        else:
            st = read_json(pdir / "state.json", {}) or {}
            lr = st.get("last_run") or {}
            if lr.get("stopped"):
                item["state"] = "paused"
                item["stopped"] = lr.get("stopped")
                item["stop_detail"] = lr.get("stop_detail")
            elif not st.get("digest_built"):
                item["state"] = "prepared" if st.get("prepared") else "new"
            else:
                item["state"] = "up-to-date"
        report["piles"].append(item)
    return report
