"""A registered pile: its state on disk, preparation (local stages) and status."""

from __future__ import annotations

import re
import shutil
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from . import config, converters, inventory, registry, triage
from .lock import PileLock
from .util import (PileDigestError, eprint, free_bytes, now_iso, read_json, sha256_text,
                   write_json)

TRIAGE_VERSION = 5
SKILL_DIR = Path(__file__).resolve().parents[2]
PROMPTS_DIR = SKILL_DIR / "prompts"


def natural_key(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def all_prompts() -> Dict[str, str]:
    return {n: load_prompt(n) for n in ("transcribe", "summarize", "spotcheck", "section")}


class Pile:
    def __init__(self, entry: dict):
        self.entry = entry
        self.id = entry["id"]
        self.name = entry["name"]
        self.root = Path(entry["root"])
        self.mode = entry.get("mode", "tree")
        self.dir = registry.pile_dir(self.id)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.cfg = config.load(read_json(self.dir / "config.json", {}) or {})

    # ------------------------------------------------------------ paths
    @property
    def inventory_path(self) -> Path:
        return self.dir / "inventory.json"

    @property
    def index_path(self) -> Path:
        return self.dir / "index.json"

    @property
    def classes_path(self) -> Path:
        return self.dir / "classes.json"

    @property
    def overrides_path(self) -> Path:
        return self.dir / "overrides.json"

    @property
    def ledger_path(self) -> Path:
        return self.dir / "ledger.jsonl"

    @property
    def state_path(self) -> Path:
        return self.dir / "state.json"

    @property
    def results_dir(self) -> Path:
        return self.dir / "results"

    @property
    def renders_dir(self) -> Path:
        return self.dir / "renders"

    @property
    def digest_path(self) -> Path:
        return self.dir / "digest.md"

    def doc_path(self, doc_id: str) -> Path:
        return self.dir / "docs" / f"{doc_id}.json"

    def work_dir(self, doc_id: str) -> Path:
        return self.dir / "work" / doc_id

    def lock(self) -> PileLock:
        return PileLock(self.dir / "run.lock", f"pile '{self.name}'")

    # ------------------------------------------------------------ state
    def state(self) -> dict:
        return read_json(self.state_path, {}) or {}

    def set_state(self, **kw) -> dict:
        st = self.state()
        st.update(kw)
        write_json(self.state_path, st)
        return st

    def index(self) -> dict:
        return read_json(self.index_path, {"docs": []}) or {"docs": []}

    def overrides(self) -> dict:
        return read_json(self.overrides_path, {}) or {}

    def set_override(self, page_id: str, cls: str, why: str, auto: bool = False) -> None:
        """Pin a page's class. auto=True for the pipeline's own decisions (e.g. a
        garbled text layer): those still give way to duplicate detection, while a
        class set by hand always wins."""
        ov = self.overrides()
        ov[page_id] = {"cls": cls, "why": why, **({"auto": True} if auto else {})}
        write_json(self.overrides_path, ov)

    def pages(self, include_docs: Optional[set] = None, raw: bool = False) -> List[dict]:
        """All pages in reading order, with pile-wide classes and overrides applied.

        raw=True skips the pile-wide (cross-page) classes, for recomputing them.
        """
        classes = {} if raw else (read_json(self.classes_path, {}) or {})
        ov = self.overrides()
        out = []
        for d in self.index().get("docs", []):
            if d.get("status") != "ok":
                continue
            if include_docs and d["doc_id"] not in include_docs:
                continue
            data = read_json(self.doc_path(d["doc_id"]), None)
            if not data:
                continue
            for p in data.get("pages", []):
                c = classes.get(p["page_id"])
                if c:
                    p.update(c)
                o = ov.get(p["page_id"])
                if o:
                    auto = bool(o.get("auto")) or o.get("why") == "text layer looked garbled to the summariser"
                    if not (auto and c and c.get("cls") == "duplicate"):
                        p["cls"] = o["cls"]
                        p["why"] = o["why"]
                        p.pop("dup_of", None)
                    p["pinned"] = "auto" if auto else "hand"
                p["relpath"] = d["relpath"]
                out.append(p)
        return out

    def result(self, page_id: str) -> Optional[dict]:
        return read_json(self.results_dir / f"{page_id}.json", None)

    def result_text(self, page_id: str) -> str:
        try:
            return (self.results_dir / f"{page_id}.md").read_text(encoding="utf-8")
        except OSError:
            return ""

    def results(self) -> Dict[str, dict]:
        out = {}
        if self.results_dir.is_dir():
            for f in self.results_dir.glob("*.json"):
                r = read_json(f, None)
                if r:
                    out[f.stem] = r
        return out

    # ------------------------------------------------------------ prepare
    def prepare(self, progress: Optional[Callable[[str], None]] = None) -> dict:
        """Inventory, convert and triage: all local, no model calls. Incremental."""
        progress = progress or (lambda m: None)
        if not self.root.is_dir():
            raise PileDigestError(f"the folder for pile '{self.name}' is missing: {self.root}")
        need = int(self.cfg.get("min_free_disk_mb", 500)) << 20
        if free_bytes(self.dir) < need:
            raise PileDigestError(f"less than {need >> 20} MB free in {self.dir}; free some space and retry")
        prev = read_json(self.inventory_path, None)
        progress("inventory: walking the pile")
        inv = inventory.build(self.root, mode=self.mode, cfg=self.cfg, state=self.dir, previous=prev,
                              progress=progress)
        write_json(self.inventory_path, inv)
        old_index = {d["doc_id"]: d for d in self.index().get("docs", [])}
        docs_meta: List[dict] = []
        by_doc: Dict[str, dict] = {}
        tmp = self.dir / "tmp"
        files = sorted(inv["files"].values(), key=lambda e: natural_key(e["relpath"]))
        n_new = 0
        dup_entries: List[dict] = []
        for e in files:
            st = e.get("status")
            if st in ("container", "skipped"):
                continue
            if st == "duplicate-file":
                dup_entries.append(e)
                continue
            if st != "ok":
                did = (e.get("sha") or sha256_text(e["relpath"]))[:16]
                meta = {"doc_id": did, "relpath": e["relpath"], "kind": e.get("kind"), "status": "unreadable",
                        "note": e.get("note", ""), "n_pages": 0}
                docs_meta.append(meta)
                by_doc[did] = meta
                continue
            did = e["sha"][:16]
            cached = read_json(self.doc_path(did), None)
            if cached and cached.get("triage_version") == TRIAGE_VERSION and cached.get("status") in ("ok", "skipped",
                                                                                                          "unreadable"):
                if cached.get("source_path") != e["path"]:
                    self._repoint(cached, cached.get("source_path"), e["path"])
                    write_json(self.doc_path(did), cached)
                doc = cached
            elif cached and cached.get("status") == "needs-password" and not (self.work_dir(did) / "unlocked.pdf").exists():
                doc = cached
            else:
                n_new += 1
                progress(f"reading {e['relpath']}")
                d = converters.convert(e, self.work_dir(did), self.cfg)
                pages: List[dict] = []
                if d["status"] == "ok":
                    tmp.mkdir(parents=True, exist_ok=True)
                    try:
                        pages = triage.triage_doc(d, self.cfg, tmp)
                    except Exception as exc:  # noqa: BLE001 - never lose the whole pile to one file
                        d.update(status="unreadable", note=f"triage failed: {type(exc).__name__}: {exc}"[:300])
                doc = {k: v for k, v in d.items() if k != "units"}
                doc.update(pages=pages, triage_version=TRIAGE_VERSION, source_path=e["path"], prepared=now_iso())
                write_json(self.doc_path(did), doc)
            meta = {"doc_id": did, "relpath": e["relpath"], "kind": doc.get("kind"), "status": doc["status"],
                    "note": doc.get("note", ""), "title": doc.get("title"), "outline": doc.get("outline", []),
                    "n_pages": len(doc.get("pages", [])), "converter": doc.get("converter"), "aliases": []}
            docs_meta.append(meta)
            by_doc[did] = meta
        for e in dup_entries:
            did = (e.get("sha") or "")[:16]
            if did in by_doc:
                by_doc[did].setdefault("aliases", []).append(e["relpath"])
        shutil.rmtree(tmp, ignore_errors=True)
        new_ids = {d["doc_id"] for d in docs_meta}
        removed = [d for k, d in old_index.items() if k not in new_ids]
        added = [d for d in docs_meta if d["doc_id"] not in old_index]
        write_json(self.index_path, {"docs": docs_meta, "updated": now_iso(),
                                     "removed": [{"relpath": d["relpath"], "doc_id": d["doc_id"]} for d in removed]})
        progress("triage: checking duplicates across the pile")
        pages = self.pages(raw=True)
        tmp.mkdir(parents=True, exist_ok=True)
        triage.cross_page(pages, self.cfg, tmp)
        shutil.rmtree(tmp, ignore_errors=True)
        write_json(self.classes_path, {p["page_id"]: {"cls": p["cls"], "why": p.get("why", ""),
                                                      **({"dup_of": p["dup_of"]} if p.get("dup_of") else {})}
                                       for p in pages})
        registry.update(self.id, last_scan=now_iso())
        self.set_state(prepared=now_iso())
        return {
            "docs": len(docs_meta),
            "docs_added": [d["relpath"] for d in added],
            "docs_removed": [d["relpath"] for d in removed],
            "converted_now": n_new,
            "unreadable": [{"relpath": d["relpath"], "note": d.get("note", "")} for d in docs_meta
                           if d["status"] not in ("ok", "skipped")],
            "skipped_files": [{"relpath": e["relpath"], "note": e.get("note", "")} for e in files
                              if e.get("status") == "skipped"],
            "duplicate_files": [{"relpath": e["relpath"], "of": e.get("duplicate_of")} for e in files
                                if e.get("status") == "duplicate-file"],
            "classes": triage.counts(pages),
        }

    @staticmethod
    def _repoint(doc: dict, old: Optional[str], new: str) -> None:
        if not old:
            return
        for p in doc.get("pages", []):
            r = p.get("render")
            if r and r.get("path") == old:
                r["path"] = new
        if doc.get("pdf") == old:
            doc["pdf"] = new

    # ------------------------------------------------------------ status
    def status(self) -> dict:
        idx = self.index()
        pages = self.pages() if idx.get("docs") else []
        res = self.results()
        by_status: Dict[str, int] = {}
        by_level: Dict[str, int] = {}
        need_work = 0
        for p in pages:
            if p["cls"] in triage.SKIP_CLASSES:
                continue
            r = res.get(p["page_id"])
            if not r:
                need_work += 1
                continue
            by_status[r["status"]] = by_status.get(r["status"], 0) + 1
            by_level[r.get("level", "?")] = by_level.get(r.get("level", "?"), 0) + 1
        from . import ledger

        st = self.state()
        return {
            "pile": self.entry,
            "docs": len(idx.get("docs", [])),
            "pages": len(pages),
            "classes": triage.counts(pages),
            "results": by_status,
            "levels": by_level,
            "pages_without_result": need_work,
            "approved": st.get("approved"),
            "last_run": st.get("last_run"),
            "prepared": st.get("prepared"),
            "digest": str(self.digest_path) if self.digest_path.exists() else None,
            "spend": ledger.summarize(self.ledger_path),
            "unreadable": [d for d in idx.get("docs", []) if d.get("status") not in ("ok", "skipped")],
        }


def open_pile(ref: str) -> Pile:
    return Pile(registry.require(ref))


def wait_note(resets_at) -> str:
    try:
        ts = float(resets_at)
    except (TypeError, ValueError):
        return ""
    mins = max(0, int((ts - time.time()) / 60))
    return f"the usage window resets in about {mins // 60} h {mins % 60} min ({time.strftime('%H:%M', time.localtime(ts))})"


def log(msg: str) -> None:
    eprint(msg)
