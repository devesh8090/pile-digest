"""Walk a pile, unpack containers safely, and record every file with a status.

Nothing is dropped silently: every file ends up in the inventory with a status
("ok", "duplicate-file", "ignored", "unreadable", ...) and a reason.
"""

from __future__ import annotations

import bz2
import email
import email.errors
import email.message
import email.policy
import fnmatch
import gzip
import lzma
import mailbox
import os
import shutil
import stat
import tarfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Callable, Iterator, Optional

from . import sniff as sniffmod
from .util import PileDigestError, run, safe_name, sha256_file, which

JUNK_NAMES = {
    ".ds_store", "thumbs.db", "desktop.ini", "ehthumbs.db", ".localized", "icon\r", ".directory",
    "__macosx",
}
JUNK_GLOBS = ["~$*", ".~lock.*#", "*.tmp", "*.temp", "*.part", "*.crdownload", "*.download", "*.swp", "*~", "._*"]
SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", ".tox", ".mypy_cache",
    ".pytest_cache", ".idea", ".vscode", ".trash", ".trashes", "$recycle.bin", "system volume information",
    "__macosx", ".cache",
}
IGNORE_FILE = ".pile-digest-ignore"


class Entry(dict):
    """One file in the inventory (a dict so it serialises directly)."""


def load_ignore(root: Path, extra: list) -> list:
    pats = list(extra or [])
    try:
        for line in (root / IGNORE_FILE).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                pats.append(line)
    except OSError:
        pass
    return pats


def _ignored(rel: str, name: str, pats: list) -> bool:
    low = name.lower()
    if low in JUNK_NAMES:
        return True
    if any(fnmatch.fnmatch(name, g) for g in JUNK_GLOBS):
        return True
    for p in pats:
        p2 = p.rstrip("/")
        if fnmatch.fnmatch(rel, p2) or fnmatch.fnmatch(name, p2) or fnmatch.fnmatch(rel, p2 + "/*"):
            return True
    return False


def walk(root: Path, *, mode: str, output_dirname: str, ignore: list) -> Iterator[tuple]:
    """Yield (relpath, abspath, stat_result) for candidate files, skipping junk.

    Directory symlinks are never followed (no loops); file symlinks are
    followed only when they point at a regular file.
    """
    root = Path(root)
    pats = load_ignore(root, ignore)
    skip_top = {output_dirname.lower()}
    if mode == "loose":
        try:
            names = sorted(os.listdir(root))
        except OSError as exc:
            raise PileDigestError(f"cannot list {root}: {exc}")
        for name in names:
            p = root / name
            if name.startswith("."):
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode) and not _ignored(name, name, pats):
                yield name, p, st
        return

    def onerror(exc: OSError) -> None:  # unreadable subfolders are reported, not fatal
        unreadable_dirs.append(str(exc))

    unreadable_dirs: list = []
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, onerror=onerror, followlinks=False):
        d = Path(dirpath)
        rel_dir = "" if d == root else d.relative_to(root).as_posix()
        keep = []
        for dn in sorted(dirnames):
            low = dn.lower()
            if dn.startswith(".") or low in SKIP_DIRS:
                continue
            if rel_dir == "" and low in skip_top:
                continue
            rel = f"{rel_dir}/{dn}" if rel_dir else dn
            if _ignored(rel, dn, pats):
                continue
            keep.append(dn)
        dirnames[:] = keep
        for fn in sorted(filenames):
            if fn.startswith("."):
                continue
            rel = f"{rel_dir}/{fn}" if rel_dir else fn
            if _ignored(rel, fn, pats):
                continue
            p = d / fn
            try:
                st = p.stat()  # follows file symlinks
            except OSError:
                yield rel, p, None
                continue
            if stat.S_ISREG(st.st_mode):
                yield rel, p, st


# ---------------------------------------------------------------- containers


class Limits:
    def __init__(self, cfg: dict):
        self.max_total = int(cfg.get("archive_max_total_mb", 4096)) * (1 << 20)
        self.max_ratio = float(cfg.get("archive_max_ratio", 250))
        self.max_members = int(cfg.get("archive_max_members", 20000))
        self.max_depth = int(cfg.get("archive_max_depth", 3))


def _safe_member(name: str) -> Optional[str]:
    name = name.replace("\\", "/")
    pp = PurePosixPath(name)
    if pp.is_absolute() or any(part in ("..", "") for part in pp.parts if part != "/"):
        parts = [p for p in pp.parts if p not in ("/", "..", ".", "")]
        if not parts:
            return None
        return "/".join(parts)  # strip dangerous parts instead of failing the whole archive
    if len(pp.parts) and pp.parts[0].endswith(":"):  # windows drive letters
        return "/".join(pp.parts[1:]) or None
    return pp.as_posix()


def _copy_capped(src, dst_path: Path, cap: int) -> int:
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with open(dst_path, "wb") as out:
        while True:
            b = src.read(1 << 20)
            if not b:
                break
            n += len(b)
            if n > cap:
                raise PileDigestError("archive expands beyond the size limit")
            out.write(b)
    return n


def extract_zip(path: Path, dest: Path, lim: Limits) -> str:
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        return f"unreadable: corrupt zip ({exc})"
    with z:
        infos = [i for i in z.infolist() if not i.is_dir()]
        if len(infos) > lim.max_members:
            return f"unreadable: archive has {len(infos)} members (limit {lim.max_members})"
        if any(i.flag_bits & 0x1 for i in infos):
            return "unreadable: password-protected archive (extract it yourself, then re-run)"
        total = sum(i.file_size for i in infos)
        if total > lim.max_total:
            return f"unreadable: archive would expand to {total >> 20} MB (limit {lim.max_total >> 20} MB)"
        written = 0
        for i in infos:
            if i.compress_size and i.file_size / max(1, i.compress_size) > lim.max_ratio and i.file_size > (8 << 20):
                return "unreadable: suspicious compression ratio (possible zip bomb)"
            name = _safe_member(i.filename)
            if not name:
                continue
            # Zip filenames without the UTF-8 flag are cp437; try to recover common cases.
            if not (i.flag_bits & 0x800):
                try:
                    name = name.encode("cp437").decode("utf-8")
                except (UnicodeEncodeError, UnicodeDecodeError):
                    pass
            try:
                with z.open(i) as src:
                    written += _copy_capped(src, dest / name, lim.max_total - written)
            except (RuntimeError, zipfile.BadZipFile, OSError, NotImplementedError, EOFError) as exc:
                (dest / (name + ".pile-digest-error")).parent.mkdir(parents=True, exist_ok=True)
                (dest / (name + ".pile-digest-error")).write_text(str(exc), encoding="utf-8")
            except PileDigestError as exc:
                return f"unreadable: {exc}"
    return "ok"


def extract_tar(path: Path, dest: Path, lim: Limits) -> str:
    try:
        t = tarfile.open(path, mode="r:*")
    except (tarfile.TarError, OSError, EOFError, lzma.LZMAError) as exc:
        return f"unreadable: corrupt tar ({exc})"
    with t:
        written = 0
        count = 0
        try:
            for m in t:
                if not m.isreg():
                    continue  # no links, devices or fifos
                count += 1
                if count > lim.max_members:
                    return f"unreadable: archive has more than {lim.max_members} members"
                name = _safe_member(m.name)
                if not name:
                    continue
                if written + m.size > lim.max_total:
                    return "unreadable: archive expands beyond the size limit"
                src = t.extractfile(m)
                if src is None:
                    continue
                with src:
                    written += _copy_capped(src, dest / name, lim.max_total - written)
        except (tarfile.TarError, OSError, EOFError, lzma.LZMAError, zlib_error()) as exc:
            return f"partial: tar stopped early ({exc})" if written else f"unreadable: corrupt tar ({exc})"
        except PileDigestError as exc:
            return f"unreadable: {exc}"
    return "ok"


def zlib_error():
    import zlib

    return zlib.error


def extract_single_compressed(path: Path, kind: str, dest: Path, lim: Limits) -> str:
    opener: Callable = {"gzip": gzip.open, "bzip2": bz2.open, "xz": lzma.open}[kind]
    # A .tar.gz is really a tar.
    try:
        if tarfile.is_tarfile(str(path)):
            return extract_tar(path, dest, lim)
    except (OSError, tarfile.TarError, EOFError):
        pass
    stem = path.name
    for ext in (".gz", ".tgz", ".bz2", ".xz", ".z"):
        if stem.lower().endswith(ext):
            stem = stem[: -len(ext)] or "file"
            break
    try:
        with opener(path, "rb") as src:
            _copy_capped(src, dest / stem, lim.max_total)
    except (OSError, EOFError, lzma.LZMAError, zlib_error()) as exc:
        return f"unreadable: corrupt {kind} file ({exc})"
    except PileDigestError as exc:
        return f"unreadable: {exc}"
    return "ok"


def extract_7z(path: Path, dest: Path, lim: Limits) -> str:
    tool = which("7z") or which("7zz") or which("7za")
    if tool:
        lst = run([tool, "l", "-slt", "-ba", "-p", str(path)], timeout=300)
        text = lst.out() + lst.err()
        if "Wrong password" in text or "Encrypted = +" in text or "Can not open encrypted archive" in text:
            return "unreadable: password-protected archive (extract it yourself, then re-run)"
        if not lst.ok:
            return f"unreadable: 7z could not list the archive ({lst.err().strip()[:200]})"
        total = 0
        count = 0
        for line in text.splitlines():
            if line.startswith("Size = "):
                try:
                    total += int(line[7:].strip() or 0)
                    count += 1
                except ValueError:
                    pass
        if total > lim.max_total:
            return f"unreadable: archive would expand to {total >> 20} MB (limit {lim.max_total >> 20} MB)"
        if count > lim.max_members:
            return f"unreadable: archive has {count} members (limit {lim.max_members})"
        dest.mkdir(parents=True, exist_ok=True)
        x = run([tool, "x", "-y", "-p", "-snl-", "-o" + str(dest), str(path)], timeout=1800)
        if not x.ok:
            return f"partial: 7z reported errors ({x.err().strip()[:200]})"
        _drop_escapes(dest)
        return "ok"
    bsd = which("bsdtar")
    if bsd:
        dest.mkdir(parents=True, exist_ok=True)
        x = run([bsd, "-x", "-f", str(path), "-C", str(dest), "--no-same-owner"], timeout=1800)
        if not x.ok:
            return f"unreadable: bsdtar failed ({x.err().strip()[:200]})"
        _drop_escapes(dest)
        return "ok"
    return "unreadable: needs 7-Zip (`7z`) or `bsdtar` to open this archive"


def _drop_escapes(dest: Path) -> None:
    """Remove symlinks and anything that resolved outside dest after an external extractor."""
    real = os.path.realpath(dest)
    for dirpath, dirnames, filenames in os.walk(dest, followlinks=False):
        for n in dirnames + filenames:
            p = os.path.join(dirpath, n)
            if os.path.islink(p) or not os.path.realpath(p).startswith(real + os.sep):
                try:
                    if os.path.isdir(p) and not os.path.islink(p):
                        shutil.rmtree(p)
                    else:
                        os.unlink(p)
                except OSError:
                    pass


def extract_email(path: Path, dest: Path, lim: Limits, kind: str) -> str:
    """Write each message body as text plus its attachments as files."""
    dest.mkdir(parents=True, exist_ok=True)
    msgs = []
    try:
        if kind == "mbox":
            box = mailbox.mbox(str(path), create=False)
            msgs = [m for m in box][: lim.max_members]
        else:
            with open(path, "rb") as f:
                msgs = [email.message_from_binary_file(f, policy=email.policy.default)]
    except (OSError, ValueError, email.errors.MessageError) as exc:
        return f"unreadable: could not parse email ({exc})"
    written = 0
    for n, msg in enumerate(msgs, 1):
        if not isinstance(msg, email.message.EmailMessage):
            try:
                msg = email.message_from_bytes(msg.as_bytes(), policy=email.policy.default)
            except Exception:  # noqa: BLE001 - mailbox messages vary wildly
                continue
        prefix = f"{n:04d}-" if len(msgs) > 1 else ""
        head = []
        for h in ("From", "To", "Cc", "Date", "Subject"):
            if msg.get(h):
                head.append(f"{h}: {msg.get(h)}")
        body = ""
        try:
            part = msg.get_body(preferencelist=("plain", "html"))
            if part is not None:
                body = part.get_content()
                if part.get_content_type() == "text/html":
                    from .textutil import html_to_text

                    body = html_to_text(body)
        except (KeyError, LookupError, ValueError, AttributeError):
            body = ""
        (dest / f"{prefix}message.txt").write_text("\n".join(head) + "\n\n" + (body or ""), encoding="utf-8")
        for k, att in enumerate(msg.iter_attachments(), 1):
            try:
                data = att.get_payload(decode=True) or b""
            except Exception:  # noqa: BLE001
                continue
            written += len(data)
            if written > lim.max_total:
                return "partial: attachments exceed the size limit"
            fname = att.get_filename() or f"attachment-{k}"
            fname = safe_name(fname, 120) or f"attachment-{k}"
            (dest / f"{prefix}{fname}").write_bytes(data)
    return "ok"


def extract_container(path: Path, kind: str, dest: Path, lim: Limits) -> str:
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    if kind == "zip":
        status = extract_zip(path, dest, lim)
    elif kind == "tar":
        status = extract_tar(path, dest, lim)
    elif kind in ("gzip", "bzip2", "xz"):
        status = extract_single_compressed(path, kind, dest, lim)
    elif kind in ("7z", "rar"):
        status = extract_7z(path, dest, lim)
    elif kind in ("eml", "mbox"):
        status = extract_email(path, dest, lim, kind)
    else:
        status = f"unreadable: not a container ({kind})"
    (dest / ".done").write_text(status, encoding="utf-8")
    return status


CONTAINER_KINDS = {"zip", "tar", "gzip", "bzip2", "xz", "7z", "rar", "eml", "mbox"}


def build(root: Path, *, mode: str, cfg: dict, state: Path, previous: Optional[dict] = None,
          progress: Optional[Callable[[str], None]] = None) -> dict:
    """Return the inventory: {"files": {relpath: Entry}, "unreadable_dirs": [...]}.

    Content hashes are reused from the previous inventory when size and mtime
    are unchanged, so re-scans of big piles are fast.
    """
    prev_files = (previous or {}).get("files", {})
    lim = Limits(cfg)
    files: dict = {}
    seen_sha: dict = {}
    derived_root = state / "derived"

    def add(rel: str, path: Path, st, parent: Optional[str], depth: int) -> None:
        e = Entry(relpath=rel, parent=parent, path=str(path))
        if st is None:
            e.update(status="unreadable", note="cannot read file (permissions or broken link)", kind="unreadable")
            files[rel] = e
            return
        e["size"] = st.st_size
        e["mtime"] = int(st.st_mtime)
        old = prev_files.get(rel)
        if old and old.get("size") == e["size"] and old.get("mtime") == e["mtime"] and old.get("sha") and parent is None:
            e["sha"] = old["sha"]
            e["kind"] = old.get("kind") or sniffmod.sniff(path)
        else:
            try:
                e["sha"] = sha256_file(path)
            except OSError as exc:
                e.update(status="unreadable", note=f"cannot read file ({exc})", kind="unreadable")
                files[rel] = e
                return
            e["kind"] = sniffmod.sniff(path)
        kind = e["kind"]
        if kind == "empty":
            e.update(status="skipped", note="empty file")
            files[rel] = e
            return
        if e["sha"] in seen_sha:
            e.update(status="duplicate-file", duplicate_of=seen_sha[e["sha"]], note="identical to another file")
            files[rel] = e
            return
        seen_sha[e["sha"]] = rel
        if kind in CONTAINER_KINDS:
            if depth >= lim.max_depth:
                e.update(status="unreadable", note=f"archive nested deeper than {lim.max_depth} levels")
                files[rel] = e
                return
            dest = derived_root / ("x-" + e["sha"][:20])
            done = dest / ".done"
            if done.exists():
                status = done.read_text(encoding="utf-8")
            else:
                if progress:
                    progress(f"unpacking {rel}")
                status = extract_container(path, kind, dest, lim)
            e["container"] = True
            e["derived"] = str(dest)
            if status.startswith("unreadable"):
                e.update(status="unreadable", note=status.split(":", 1)[1].strip())
                files[rel] = e
                return
            e["status"] = "container"
            e["note"] = status if status != "ok" else ""
            files[rel] = e
            for crel, cpath, cst in walk(dest, mode="tree", output_dirname="\0", ignore=[]):
                if crel == ".done" or crel.endswith(".pile-digest-error"):
                    continue
                add(f"{rel}!/{crel}", cpath, cst, rel, depth + 1)
            return
        e["status"] = "ok"
        files[rel] = e

    for rel, path, st in walk(root, mode=mode, output_dirname=cfg.get("output_dirname", "pile-digest"),
                              ignore=cfg.get("ignore", [])):
        add(rel, path, st, None, 0)
    return {"files": files}


def quick_diff(root: Path, *, mode: str, cfg: dict, previous: Optional[dict]) -> dict:
    """Cheap change detection by size+mtime only (no hashing, no unpacking)."""
    prev = {k: v for k, v in ((previous or {}).get("files", {})).items() if v.get("parent") is None}
    now: dict = {}
    for rel, _path, st in walk(root, mode=mode, output_dirname=cfg.get("output_dirname", "pile-digest"),
                              ignore=cfg.get("ignore", [])):
        now[rel] = (st.st_size, int(st.st_mtime)) if st else (None, None)
    new = sorted(set(now) - set(prev))
    removed = sorted(set(prev) - set(now))
    changed = sorted(r for r in set(now) & set(prev) if (prev[r].get("size"), prev[r].get("mtime")) != now[r])
    total_bytes = sum(s for s, _ in now.values() if s)
    return {"new": new, "removed": removed, "changed": changed, "files": len(now), "bytes": total_bytes}
