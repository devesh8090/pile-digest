#!/usr/bin/env python3
"""pile-digest command line. Run `pd.py --help` or `pd.py <command> --help`.

Local stages are free. Commands that call models say so in their help, and
`run` refuses to start until the current estimate has been approved.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import platform
import shutil
import sys
from pathlib import Path

if sys.version_info < (3, 9):
    sys.exit("pile-digest needs Python 3.9 or newer")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pdlib import (config, converters, digest, engine, estimate, ledger, registry,  # noqa: E402
                   scan, worker)
from pdlib.pile import Pile, all_prompts, open_pile, wait_note  # noqa: E402
from pdlib.util import PileDigestError, human_bytes, human_tokens, now_iso, read_json, run, which, write_json  # noqa: E402

VERSION = "1.0.0"
SKILL_DIR = Path(__file__).resolve().parents[1]
JSON = False


def out(obj, text: str = "") -> None:
    if JSON:
        print(json.dumps(obj, indent=1, ensure_ascii=False, default=str))
    else:
        print(text if text else json.dumps(obj, indent=1, ensure_ascii=False, default=str))


def progress(msg: str) -> None:
    print(f"  {msg}", file=sys.stderr, flush=True)


# ------------------------------------------------------------------ doctor


TOOLS = [
    ("pdftoppm", "poppler-utils", True, "render PDF pages"),
    ("pdftotext", "poppler-utils", True, "read PDF text layers"),
    ("pdfinfo", "poppler-utils", True, "page counts and sizes"),
    ("pdfimages", "poppler-utils", False, "detect scanned pages"),
    ("qpdf", "qpdf", False, "repair/decrypt PDFs, read bookmarks"),
    ("mutool", "mupdf-tools", False, "fallback PDF renderer, XPS"),
    ("gs", "ghostscript", False, "spot drawings on typed pages, PostScript"),
    ("soffice", "libreoffice", False, "Office files with full layout, legacy .doc/.ppt"),
    ("magick", "imagemagick", False, "odd image formats, tiling without Pillow"),
    ("7z", "p7zip / 7zip", False, "7z and rar archives"),
    ("ddjvu", "djvulibre", False, "DjVu files"),
    ("heif-convert", "libheif", False, "HEIC photos without Pillow"),
]


def cmd_doctor(a) -> int:
    rows = []
    for name, pkg, required, why in TOOLS:
        path = which(name) or (which("convert") if name == "magick" else None) or \
            (which("libreoffice") if name == "soffice" else None) or (which("7zz") if name == "7z" else None)
        rows.append({"tool": name, "found": bool(path), "required": required, "package": pkg, "for": why})
    py_ok = sys.version_info >= (3, 9)
    try:
        import PIL  # type: ignore  # noqa: F401

        pil = True
    except Exception:  # noqa: BLE001
        pil = False
    try:
        import sympy  # type: ignore  # noqa: F401

        sym = True
    except Exception:  # noqa: BLE001
        sym = False
    cli = worker.claude_bin()
    cli_version = run([cli, "--version"], timeout=30).out().strip() if cli else None
    live = None
    if a.live and cli:
        r = worker.selftest(config.load()["models"]["bulk"])
        live = {"ok": r.ok, "error": r.error_kind, "detail": r.error, "model": r.model_used, "rate": r.rate}
    missing_required = [r["tool"] for r in rows if r["required"] and not r["found"]]
    # One renderer is enough: mutool can stand in for poppler.
    if missing_required and which("mutool"):
        missing_required = [t for t in missing_required if t not in ("pdftoppm", "pdftotext", "pdfinfo")]
    res = {"version": VERSION, "python": platform.python_version(), "python_ok": py_ok, "pillow": pil, "sympy": sym,
           "claude_cli": cli, "claude_version": cli_version, "tools": rows, "missing_required": missing_required,
           "state_dir": str(config.state_root()), "config": str(config.config_path()), "live_check": live,
           "ok": py_ok and bool(cli) and not missing_required and (live is None or live["ok"])}
    if JSON:
        out(res)
        return 0 if res["ok"] else 1
    print(f"pile-digest {VERSION} · Python {res['python']} · Pillow {'yes' if pil else 'no'} · sympy {'yes' if sym else 'no'}")
    print(f"claude CLI: {cli_version or 'NOT FOUND (required)'}")
    for r in rows:
        mark = "ok " if r["found"] else ("MISSING" if r["required"] else "-  ")
        print(f"  [{mark}] {r['tool']:<13} {r['for']}" + ("" if r["found"] else f"  (install: {r['package']})"))
    if live:
        print("live check: " + ("ok" if live["ok"] else f"FAILED ({live['error']}: {live['detail']})"))
    print(f"state: {res['state_dir']}\nconfig: {res['config']}")
    print("READY" if res["ok"] else "NOT READY: " + ", ".join(
        (["claude CLI"] if not cli else []) + missing_required + (["live check"] if live and not live["ok"] else [])))
    return 0 if res["ok"] else 1


# ------------------------------------------------------------------ piles


def fmt_pile_line(p: dict) -> str:
    st = p.get("state", "")
    extra = ""
    if st == "changed":
        extra = f" (+{p.get('n_new', 0)} new, {p.get('n_changed', 0)} changed, -{p.get('n_removed', 0)} removed)"
    elif st == "paused":
        extra = f" ({p.get('stop_detail') or p.get('stopped')})"
    size = f", {p.get('files', 0)} files, {human_bytes(p.get('bytes', 0))}" if "files" in p else ""
    return f"- [{st}] {p['name']}  id={p['id']}{size}{extra}\n    {p['root']}"


def cmd_scan(a) -> int:
    rep = scan.scan(a.path, register_path=a.register)
    if JSON:
        out(rep)
        return 0
    if rep["moved"]:
        for pid, root in rep["moved"]:
            print(f"Found moved pile {pid} at {root}")
    for e in rep["auto_registered"]:
        print(f"New pile registered: {e['name']} ({e['root']})")
    if rep["candidate"]:
        c = rep["candidate"]
        warn = " — looks like a code repository" if c["code_repo"] else ""
        print(f"Unregistered folder with {c['documents']} document-like files{warn}: {c['path']}")
        print(f"  e.g. {', '.join(c['examples'][:3])}")
        print(f"  Register it with: pd.py add \"{c['path']}\"")
    if rep["focus"]:
        print(f"Current pile: {rep['focus']}")
    if rep["piles"]:
        print("Known piles:")
        for p in rep["piles"]:
            print(fmt_pile_line(p))
    else:
        print("No piles registered yet.")
    if not rep["watch_roots"]:
        print("No watch folders set (new piles there are picked up automatically): pd.py watch add <folder>")
    for w in rep["warnings"]:
        print(f"warning: {w}")
    return 0


def cmd_list(a) -> int:
    data = registry.load()
    piles = sorted(data["piles"].values(), key=lambda x: x.get("added") or "")
    if JSON:
        out(piles)
        return 0
    if not piles:
        print("No piles registered. Add one: pd.py add <folder>")
    for p in piles:
        miss = "  (folder missing)" if not Path(p["root"]).is_dir() else ""
        print(f"- {p['name']}  id={p['id']}  {p['root']}{miss}")
    return 0


def cmd_add(a) -> int:
    e = registry.register(Path(a.path), mode="loose" if a.loose else "tree", name=a.name, force=a.force)
    out(e, f"Registered pile '{e['name']}' (id {e['id']}) at {e['root']}")
    return 0


def cmd_watch(a) -> int:
    cfg = config.load_user_config()
    roots = list(cfg.get("watch_roots", []))
    if a.action == "list":
        out(roots, "\n".join(roots) if roots else "No watch folders.")
        return 0
    p = os.path.realpath(os.path.expanduser(a.folder or ""))
    if a.action == "add":
        if not Path(p).is_dir():
            raise PileDigestError(f"{p} is not a folder")
        if p not in roots:
            roots.append(p)
    elif a.action == "remove":
        roots = [r for r in roots if os.path.realpath(r) != p]
    config.set_user_value("watch_roots", roots)
    out(roots, "Watch folders:\n" + ("\n".join(roots) if roots else "(none)"))
    return 0


def _prepare_report(p: Pile, r: dict) -> str:
    lines = [f"Pile '{p.name}': {r['docs']} files read ({r['converted_now']} new this time)."]
    if r["docs_added"]:
        lines.append(f"Added since last time: {len(r['docs_added'])}: " + ", ".join(r["docs_added"][:10]) +
                     (" …" if len(r["docs_added"]) > 10 else ""))
    if r["docs_removed"]:
        lines.append(f"Gone since last time: {len(r['docs_removed'])}: " + ", ".join(r["docs_removed"][:10]))
    c = r["classes"]
    lines.append("Pages: " + ", ".join(f"{k} {v}" for k, v in sorted(c.items(), key=lambda kv: -kv[1])))
    if r["duplicate_files"]:
        lines.append(f"Identical files skipped: {len(r['duplicate_files'])}")
    if r["skipped_files"]:
        lines.append(f"Empty files skipped: {len(r['skipped_files'])}")
    for u in r["unreadable"]:
        lines.append(f"  not readable: {u['relpath']} — {u['note']}")
    return "\n".join(lines)


def cmd_prepare(a) -> int:
    p = open_pile(a.pile)
    with p.lock():
        r = p.prepare(progress)
    est = _estimate(p)
    if JSON:
        out({"prepare": r, "estimate": est})
    else:
        print(_prepare_report(p, r))
        print()
        print(estimate.format_estimate(est))
    return 0


def _estimate(p: Pile) -> dict:
    pages = p.pages()
    done = set(p.results().keys())
    return estimate.estimate(pages, p.cfg, prompts=all_prompts(), ledger_path=p.ledger_path, done_ids=done)


def cmd_estimate(a) -> int:
    p = open_pile(a.pile)
    est = _estimate(p)
    out(est, estimate.format_estimate(est))
    return 0


def _run_summary(ctx: engine.RunContext, stats: dict) -> str:
    lines = [f"Spent this run: {human_tokens(ctx.spent)} tokens (input + cache writes + output)."]
    if ctx.last_rate and ctx.last_rate.get("utilization") is not None:
        lines.append(f"5-hour window: {float(ctx.last_rate['utilization']):.0%} used; "
                     + wait_note(ctx.last_rate.get("resets_at")))
    if ctx.stop_reason:
        lines.append(f"STOPPED: {ctx.stop_detail}. Nothing is lost: run the same command again to resume.")
    for k, v in stats.items():
        if v and not isinstance(v, (list, dict)):
            lines.append(f"  {k}: {v}")
    return "\n".join(lines)


def cmd_pilot(a) -> int:
    p = open_pile(a.pile)
    with p.lock():
        if not p.index().get("docs"):
            p.prepare(progress)
        ctx = engine.RunContext(p, budget=a.budget)
        stats = engine.stage_pilot(ctx, p.pages())
    est = _estimate(p)
    res = p.results()
    samples = []
    for pid in stats.get("pilot_pages", []):
        r = res.get(pid) or {}
        samples.append({"page_id": pid, "relpath": r.get("relpath"), "page": r.get("index"), "level": r.get("level"),
                        "confidence": r.get("confidence"), "escalate": r.get("escalate"),
                        "flags": len(r.get("flags") or []) + len(r.get("markers") or []),
                        "file": str(p.results_dir / f"{pid}.md")})
    p.set_state(pilot={"at": now_iso(), "pages": stats.get("pilot_pages", []), "stopped": ctx.stop_reason})
    if JSON:
        out({"stats": stats, "samples": samples, "estimate": est, "stopped": ctx.stop_reason,
             "stop_detail": ctx.stop_detail})
        return 0
    print(_run_summary(ctx, {k: v for k, v in stats.items() if k != "pilot_pages"}))
    print("\nPilot pages (open a file to see the transcription):")
    for s in samples:
        flag = f" ⚑ {s['escalate']}" if s["escalate"] else ""
        print(f"  {s['relpath']} p{s['page']} [{s['confidence']}]{flag}\n    {s['file']}")
    print()
    print(estimate.format_estimate(est))
    print("\nIf the user agrees, record it with: pd.py approve " + p.id)
    return 0


def cmd_approve(a) -> int:
    p = open_pile(a.pile)
    est = _estimate(p)
    appr = {"at": now_iso(), "bulk_tokens": est["tokens"]["bulk_model"], "strong_tokens": est["tokens"]["strong_model"],
            "pages_todo": est["pages"]["vision"] + est["pages"]["text_summaries"], "note": a.note or ""}
    p.set_state(approved=appr)
    out(appr, f"Approved: ~{human_tokens(appr['bulk_tokens'])} cheap-model tokens and "
              f"~{human_tokens(appr['strong_tokens'])} stronger-model tokens for {appr['pages_todo']} pages.")
    return 0


def _check_approval(p: Pile) -> None:
    appr = p.state().get("approved")
    est = _estimate(p)
    todo = est["tokens"]["bulk_model"] + est["tokens"]["strong_model"]
    if todo == 0:
        return
    if not appr:
        raise PileDigestError("this pile's estimate has not been approved yet. Show the estimate to the user "
                              f"(pd.py estimate {p.id}); if they agree, run pd.py approve {p.id}")
    allowed = (appr.get("bulk_tokens", 0) + appr.get("strong_tokens", 0)) * 1.3 + 20000
    if todo > allowed:
        raise PileDigestError(f"the remaining work (~{human_tokens(todo)} tokens) is more than was approved "
                              f"(~{human_tokens(allowed / 1.3)}), probably because files were added. Show the new "
                              f"estimate to the user and re-approve: pd.py approve {p.id}")


STAGES = ["summaries", "bulk", "escalate", "spotcheck", "digest"]


def cmd_run(a) -> int:
    p = open_pile(a.pile)
    stages = [s.strip() for s in (a.stages or ",".join(STAGES)).split(",") if s.strip()]
    bad = [s for s in stages if s not in STAGES]
    if bad:
        raise PileDigestError(f"unknown stage(s): {bad}; choose from {STAGES}")
    with p.lock():
        if a.prepare or not p.index().get("docs"):
            p.prepare(progress)
        _check_approval(p)
        ctx = engine.RunContext(p, budget=a.budget)
        all_stats: dict = {}
        for s in stages:
            if ctx.stop_reason:
                break
            pages = p.pages()  # reload: summaries may reclassify garbled pages
            progress(f"stage: {s}")
            if s == "summaries":
                st = engine.stage_summaries(ctx, pages)
            elif s == "bulk":
                st = engine.stage_bulk(ctx, pages)
            elif s == "escalate":
                st = engine.stage_escalate(ctx, pages)
            elif s == "spotcheck":
                st = engine.stage_spotcheck(ctx, pages)
            else:
                path = digest.build(p, ctx, with_sections=not a.no_sections)
                st = {"digest": str(path)}
            all_stats.update({f"{s}.{k}": v for k, v in (st or {}).items()})
        if "bulk" in stages and not ctx.stop_reason:
            # Pages reclassified by the summariser are read now, in the same run.
            again = engine.stage_bulk(ctx, p.pages())
            if again:
                all_stats.update({f"bulk2.{k}": v for k, v in again.items()})
                if "digest" in stages and not ctx.stop_reason:
                    digest.build(p, ctx, with_sections=not a.no_sections)
        ctx.save_progress("done" if not ctx.stop_reason else "stopped", 0, 0)
        if not ctx.stop_reason:
            st = p.state()
            lr = st.get("last_run") or {}
            lr["stopped"] = None
            lr["finished"] = now_iso()
            p.set_state(last_run=lr)
    spot = engine.spot_summary(p)
    q = engine.review_queue(p)
    res = {"stats": all_stats, "spent": ctx.spent, "stopped": ctx.stop_reason, "stop_detail": ctx.stop_detail,
           "rate": ctx.last_rate, "spotcheck": spot, "review": len(q["review"]),
           "source_suspect": len(q["source_suspect"]), "digest": str(p.digest_path) if p.digest_path.exists() else None}
    if JSON:
        out(res)
        return 0
    print(_run_summary(ctx, all_stats))
    if spot["checked"]:
        mr = spot["mismatch_rate"]
        print(f"Spot checks: {spot['checked']} pages, verdicts {spot['verdicts']}" +
              (f", mismatch rate {mr:.0%}" if mr is not None else ""))
        if spot["advice"]:
            print("ADVICE: " + spot["advice"].replace("<pile>", p.id))
    print(f"For main-model review: {len(q['review'])} pages; source claims to verify: {len(q['source_suspect'])}")
    if res["digest"]:
        print(f"Digest: {res['digest']}")
    return 0


def cmd_rerun(a) -> int:
    p = open_pile(a.pile)
    with p.lock():
        ctx = engine.RunContext(p, budget=a.budget)
        st = engine.rerun(ctx, p.pages(), a.level, a.doc)
    out({"stats": st, "stopped": ctx.stop_reason}, _run_summary(ctx, st))
    return 0


def cmd_status(a) -> int:
    if not a.pile:
        rep = scan.scan(None)
        if JSON:
            out(rep["piles"])
            return 0
        if not rep["piles"]:
            print("No piles registered.")
        for item in rep["piles"]:
            print(fmt_pile_line(item))
        return 0
    p = open_pile(a.pile)
    s = p.status()
    q = engine.review_queue(p)
    s["review"] = len(q["review"])
    s["source_suspect"] = len(q["source_suspect"])
    s["spotcheck"] = engine.spot_summary(p)
    s["outputs"] = _outputs_status(p)
    if JSON:
        out(s)
        return 0
    sp = s["spend"]
    print(f"Pile '{p.name}' ({p.id}) at {p.root}")
    print(f"  files {s['docs']}, pages {s['pages']}: " + ", ".join(f"{k} {v}" for k, v in sorted(s["classes"].items())))
    print("  results: " + (", ".join(f"{k} {v}" for k, v in s["results"].items()) or "none") +
          f"; not read yet: {s['pages_without_result']}")
    if s["levels"]:
        print("  read at level: " + ", ".join(f"{k} {v}" for k, v in s["levels"].items()))
    print(f"  spent so far: {human_tokens(sp['total_tokens'])} tokens in {sp['calls']} calls "
          f"(API-price equivalent ${sp['cost_usd']:.2f})")
    lr = s.get("last_run") or {}
    if lr.get("stopped"):
        print(f"  last run stopped: {lr.get('stop_detail') or lr['stopped']} — re-run to resume")
    print(f"  review queue: {s['review']} pages; source claims to verify: {s['source_suspect']}")
    if s["spotcheck"]["checked"]:
        print(f"  spot checks: {s['spotcheck']['verdicts']}")
    for name, o in s["outputs"].items():
        stale = o.get("stale_docs") or []
        print(f"  output '{name}': built {o.get('built')}" + (f", STALE for {len(stale)} file(s)" if stale else ", up to date"))
    for d in s["unreadable"]:
        print(f"  not readable: {d['relpath']} — {d.get('note', '')}")
    print(f"  digest: {s['digest'] or 'not built yet'}")
    print(f"  outputs folder: {s['output_dir']}")
    return 0


def cmd_review(a) -> int:
    p = open_pile(a.pile)
    q = engine.review_queue(p)
    labels = digest.doc_labels(p)
    for item in q["review"] + q["source_suspect"]:
        did = item["page_id"].rsplit("-p", 1)[0]
        item["ref"] = f"{labels.get(did, '?')}:{item['page']}"
    if JSON:
        out(q)
        return 0
    if not q["review"] and not q["source_suspect"]:
        print("Nothing waiting for review.")
    if q["review"]:
        print("Pages the main model should read from the image (pd.py page <pile> <ref> --image):")
        for it in q["review"]:
            sug = "  (a suggested correction exists: --suggested)" if it.get("suggested") else ""
            print(f"  {it['ref']}  {it['relpath']} p{it['page']}: {it['reason']}{sug}")
    if q["source_suspect"]:
        print("Claims flagged as possibly wrong in the source (verify, then pd.py verify):")
        for it in q["source_suspect"]:
            print(f"  {it['ref']}  {it['relpath']} p{it['page']}: {it['note']}")
    return 0


def _page_lookup(p: Pile, ref: str) -> dict:
    pid = digest.resolve_ref(p, ref)
    for pg in p.pages():
        if pg["page_id"] == pid:
            return pg
    raise PileDigestError(f"no page '{ref}' in pile {p.id}")


def cmd_page(a) -> int:
    p = open_pile(a.pile)
    pg = _page_lookup(p, a.ref)
    info = {"page_id": pg["page_id"], "relpath": pg.get("relpath"), "page": pg["index"], "cls": pg["cls"],
            "result": p.result(pg["page_id"])}
    if a.image:
        outdir = p.dir / "review"
        imgs = engine.render_images(p, pg, outdir, tiles=a.tiles)
        info["images"] = [str(i) for i in imgs]
        if not imgs:
            info["images_error"] = "this page has no image (text-only source) or could not be rendered"
    if a.suggested:
        sp = p.results_dir / f"{pg['page_id']}.suggested.md"
        info["suggested"] = sp.read_text(encoding="utf-8") if sp.exists() else None
    if JSON:
        info["text"] = p.result_text(pg["page_id"]) or pg.get("text", "")
        out(info)
        return 0
    print(f"# {pg.get('relpath')} page {pg['index']}  ({pg['page_id']}, {pg['cls']})")
    body = p.result_text(pg["page_id"])
    print(body if body else (pg.get("text") or "(no text; not read yet)"))
    if info.get("suggested"):
        print("\n--- suggested correction from the spot check ---\n" + info["suggested"])
    for i in info.get("images", []):
        print(f"[image] {i}")
    if info.get("images_error"):
        print(info["images_error"])
    return 0


def cmd_fix(a) -> int:
    p = open_pile(a.pile)
    pg = _page_lookup(p, a.ref)
    if a.suggested:
        src = p.results_dir / f"{pg['page_id']}.suggested.md"
        if not src.exists():
            raise PileDigestError("no suggested correction for this page")
        content = src.read_text(encoding="utf-8")
    else:
        content = Path(a.file).read_text(encoding="utf-8")
    old = p.result(pg["page_id"]) or {}
    parsed = {"kind": old.get("kind") or "transcription", "confidence": "high",
              "summary": a.summary or old.get("summary") or "", "topics": old.get("topics") or [],
              "flags": [f for f in (old.get("flags") or []) if f.get("kind") == "source-suspect"], "markers": []}
    from pdlib.parse import MARKER_RE

    parsed["markers"] = [{"kind": m.group(1).lower(), "note": (m.group(2) or "")} for m in MARKER_RE.finditer(content)]
    meta = engine.save_result(p, pg, parsed, model="main", level="main", stage="review", content=content)
    engine.mark(p, pg["page_id"], reviewed=True, fixed_by_main=True, fix_note=a.note or "")
    out(meta, f"Saved the main-model version of {a.ref}.")
    return 0


def cmd_reclass(a) -> int:
    p = open_pile(a.pile)
    pg = _page_lookup(p, a.ref)
    if a.cls in ("hybrid", "vision") and not pg.get("render"):
        raise PileDigestError("this page comes from a text-only source and has no image to read")
    p.set_override(pg["page_id"], a.cls, a.note or f"set by hand to {a.cls}")
    if a.cls in ("hybrid", "vision", "text-ok") and p.result(pg["page_id"]):
        res = p.results_dir / f"{pg['page_id']}.json"
        old = p.result(pg["page_id"]) or {}
        if old.get("level") not in ("main",):
            res.unlink()  # re-read on the next run
    out({"page_id": pg["page_id"], "cls": a.cls},
        f"{a.ref} is now '{a.cls}'. It is read on the next `pd.py run` (approval may be needed).")
    return 0


def cmd_accept(a) -> int:
    p = open_pile(a.pile)
    pg = _page_lookup(p, a.ref)
    r = p.result(pg["page_id"])
    if not r:
        raise PileDigestError("this page has no result to accept")
    engine.mark(p, pg["page_id"], reviewed=True, status="done", review_note=a.note or "accepted as is")
    out({"ok": True}, f"Marked {a.ref} as reviewed.")
    return 0


def cmd_verify(a) -> int:
    p = open_pile(a.pile)
    pg = _page_lookup(p, a.ref)
    r = p.result(pg["page_id"]) or {}
    ver = r.get("verifications") or []
    ver.append({"verdict": a.verdict, "note": a.note, "at": now_iso()})
    engine.mark(p, pg["page_id"], verified=True, verifications=ver)
    out({"ok": True}, f"Recorded: {a.ref} is {a.verdict}.")
    return 0


def cmd_errata(a) -> int:
    p = open_pile(a.pile)
    labels = digest.doc_labels(p)
    items = []
    for pid, r in sorted(p.results().items()):
        did = pid.rsplit("-p", 1)[0]
        ref = f"{labels.get(did, '?')}:{r.get('index')}"
        for v in r.get("verifications") or []:
            if v["verdict"] == "source-error":
                items.append({"ref": ref, "relpath": r.get("relpath"), "page": r.get("index"), "type": "source error",
                              "note": v["note"]})
        if r.get("fixed_by_main"):
            items.append({"ref": ref, "relpath": r.get("relpath"), "page": r.get("index"),
                          "type": "transcription corrected", "note": r.get("fix_note", "")})
    if JSON:
        out(items)
        return 0
    if not items:
        print("No verified source errors or corrected transcriptions yet.")
    for i in items:
        print(f"- {i['ref']} ({i['relpath']} p{i['page']}) [{i['type']}] {i['note']}")
    return 0


def cmd_digest(a) -> int:
    p = open_pile(a.pile)
    with p.lock():
        ctx = engine.RunContext(p, budget=a.budget) if not a.no_sections else None
        if ctx is not None:
            appr = p.state().get("approved")
            if not appr:
                ctx = None  # section digests call a model: only after approval
        path = digest.build(p, ctx, with_sections=ctx is not None)
    out({"digest": str(path)}, f"Digest: {path}")
    return 0


# ------------------------------------------------------------------ outputs


def profiles() -> dict:
    out_ = {}
    for f in sorted((SKILL_DIR / "profiles").glob("*.md")):
        out_[f.stem] = {"instructions": str(f), "builtin": True}
    for name, spec in (config.load().get("profiles") or {}).items():
        if not isinstance(spec, dict):
            continue
        item = dict(out_.get(name, {}))
        item.update({k: v for k, v in spec.items()})
        if item.get("instructions"):
            item["instructions"] = os.path.expanduser(item["instructions"])
        item["builtin"] = name in out_ and not spec.get("instructions")
        out_[name] = item
    return out_


def _outputs_status(p: Pile) -> dict:
    data = read_json(p.dir / "outputs.json", {}) or {}
    fps = digest.doc_fingerprints(p)
    res = {}
    for name, o in data.items():
        old = o.get("fingerprints", {})
        stale = [d for d, h in fps.items() if old.get(d) != h]
        res[name] = {"built": o.get("built"), "dir": o.get("dir"), "stale_docs": stale}
    return res


def cmd_profiles(a) -> int:
    pr = profiles()
    out(pr, "\n".join(f"- {k}: {v.get('instructions')}" + (f"  post_build: {v['post_build']}" if v.get("post_build") else "")
                      for k, v in pr.items()))
    return 0


def cmd_outputs(a) -> int:
    p = open_pile(a.pile)
    if a.mark:
        data = read_json(p.dir / "outputs.json", {}) or {}
        data[a.mark] = {"built": now_iso(), "dir": str(p.output_dir() / a.mark),
                        "fingerprints": digest.doc_fingerprints(p)}
        write_json(p.dir / "outputs.json", data)
        out(data[a.mark], f"Recorded output '{a.mark}' as built from the current digest.")
        return 0
    st = _outputs_status(p)
    labels = digest.doc_labels(p)
    idx = {d["doc_id"]: d["relpath"] for d in p.index().get("docs", [])}
    if JSON:
        out({"output_dir": str(p.output_dir()), "outputs": st})
        return 0
    print(f"Outputs folder: {p.output_dir()}")
    if not st:
        print("No outputs recorded yet.")
    for name, o in st.items():
        print(f"- {name}: built {o['built']}")
        for d in o["stale_docs"]:
            print(f"    stale: {labels.get(d, '?')} {idx.get(d, d)}")
    return 0


def cmd_postbuild(a) -> int:
    p = open_pile(a.pile)
    pr = profiles().get(a.profile)
    if not pr:
        raise PileDigestError(f"unknown profile '{a.profile}'")
    cmd = pr.get("post_build")
    if not cmd:
        out({"ran": False}, f"Profile '{a.profile}' has no post_build command.")
        return 0
    if isinstance(cmd, str):
        raise PileDigestError("post_build must be a list of arguments, e.g. [\"make\", \"pdf\"] (never a shell string)")
    outdir = p.output_dir() / a.profile
    if not outdir.is_dir():
        raise PileDigestError(f"{outdir} does not exist yet; write the outputs first")
    args = [os.path.expanduser(str(c)).replace("{out}", str(outdir)).replace("{pile}", str(p.root)) for c in cmd]
    r = run(args, cwd=outdir, timeout=3600)
    res = {"ran": True, "cmd": args, "returncode": r.returncode, "stdout": r.out()[-4000:], "stderr": r.err()[-4000:]}
    out(res, f"$ {' '.join(args)}\nexit {r.returncode}\n{r.out()[-3000:]}\n{r.err()[-3000:]}")
    return 0 if r.ok else 1


# ------------------------------------------------------------------ misc


def cmd_unlock(a) -> int:
    p = open_pile(a.pile)
    inv = read_json(p.inventory_path, {}) or {}
    e = (inv.get("files") or {}).get(a.relpath)
    if not e:
        raise PileDigestError(f"no file '{a.relpath}' in this pile's inventory (run prepare first)")
    if not sys.stdin.isatty():
        raise PileDigestError("run this in your own terminal so the password is typed privately")
    pw = getpass.getpass(f"Password for {a.relpath}: ")
    res = converters.unlock_pdf(Path(e["path"]), pw, p.work_dir(e["sha"][:16]))
    if not res:
        raise PileDigestError("could not unlock the file (wrong password, or qpdf is missing)")
    print("Unlocked. A decrypted copy is kept in pile-digest's private state folder; run prepare again.")
    return 0


def cmd_forget(a) -> int:
    p = registry.require(a.pile)
    if not a.yes:
        raise PileDigestError("this deletes all pile-digest state for the pile (not your files). Add --yes to confirm.")
    registry.forget(p["id"])
    out({"forgot": p["id"]}, f"Forgot pile '{p['name']}'. Your files were not touched.")
    return 0


def cmd_gc(a) -> int:
    p = open_pile(a.pile)
    keep = {d["doc_id"] for d in p.index().get("docs", [])}
    removed = 0
    for sub in ("docs", "work"):
        d = p.dir / sub
        if not d.is_dir():
            continue
        for f in d.iterdir():
            did = f.stem if f.is_file() else f.name
            if did not in keep:
                shutil.rmtree(f, ignore_errors=True) if f.is_dir() else f.unlink()
                removed += 1
    if p.results_dir.is_dir():
        for f in p.results_dir.iterdir():
            if f.name.split("-p", 1)[0] not in keep:
                f.unlink()
                removed += 1
    shutil.rmtree(p.renders_dir, ignore_errors=True)
    out({"removed": removed}, f"Removed {removed} leftover items.")
    return 0


def cmd_config(a) -> int:
    if a.action == "show":
        cfg = config.load(read_json(open_pile(a.pile).dir / "config.json", {}) if a.pile else None)
        out(cfg)
        return 0
    try:
        value = json.loads(a.value)
    except (TypeError, ValueError):
        value = a.value
    if a.key not in config.DEFAULTS:
        raise PileDigestError(f"unknown setting '{a.key}'. Known: {', '.join(sorted(config.DEFAULTS))}")
    if a.pile:
        p = open_pile(a.pile)
        data = read_json(p.dir / "config.json", {}) or {}
        data[a.key] = value
        write_json(p.dir / "config.json", data)
    else:
        config.set_user_value(a.key, value)
    out({a.key: value}, f"Set {a.key} = {json.dumps(value)}" + (f" for pile {a.pile}" if a.pile else ""))
    return 0


def cmd_ledger(a) -> int:
    p = open_pile(a.pile)
    s = ledger.summarize(p.ledger_path)
    if JSON:
        out(s)
        return 0
    print(f"{s['calls']} calls, {human_tokens(s['total_tokens'])} tokens, API-price equivalent ${s['cost_usd']:.2f}")
    for k, v in sorted(s["by"].items()):
        print(f"  {k:<28} calls {v['calls']:>4}  in {human_tokens(v['input']):>7}  cache-w {human_tokens(v['cache_create']):>7}"
              f"  cache-r {human_tokens(v['cache_read']):>7}  out {human_tokens(v['output']):>7}  pages {v['pages']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="pd.py", description="pile-digest: understand big document piles cheaply.")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--version", action="version", version=VERSION)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        sp = sub.add_parser(name, help=help_, description=help_)
        sp.set_defaults(fn=fn)
        return sp

    s = add("doctor", cmd_doctor, "check tools and the claude CLI (free; --live makes one tiny model call)")
    s.add_argument("--live", action="store_true")
    s = add("scan", cmd_scan, "find new piles and changes in known piles (free)")
    s.add_argument("path", nargs="?")
    s.add_argument("--register", action="store_true", help="register the given path as a pile if it is new")
    add("list", cmd_list, "list registered piles")
    s = add("add", cmd_add, "register a folder as a pile")
    s.add_argument("path")
    s.add_argument("--name")
    s.add_argument("--force", action="store_true")
    s.add_argument("--loose", action="store_true", help="only the files directly in this folder")
    s = add("watch", cmd_watch, "manage watch folders (every subfolder becomes a pile)")
    s.add_argument("action", choices=["add", "remove", "list"])
    s.add_argument("folder", nargs="?")
    s = add("prepare", cmd_prepare, "inventory, convert and triage a pile, then estimate (free, local)")
    s.add_argument("pile")
    s = add("estimate", cmd_estimate, "show the token estimate (free)")
    s.add_argument("pile")
    s = add("pilot", cmd_pilot, "read a small sample with the cheap model to calibrate (MODEL CALLS, small)")
    s.add_argument("pile")
    s.add_argument("--budget", type=int)
    s = add("approve", cmd_approve, "record that the user approved the current estimate")
    s.add_argument("pile")
    s.add_argument("--note")
    s = add("run", cmd_run, "run the approved pipeline; resumable (MODEL CALLS)")
    s.add_argument("pile")
    s.add_argument("--stages", help=f"comma list from {STAGES}")
    s.add_argument("--budget", type=int, help="stop before this many tokens are used in this run")
    s.add_argument("--prepare", action="store_true", help="re-scan the pile first")
    s.add_argument("--no-sections", action="store_true", help="skip section digests")
    s = add("rerun", cmd_rerun, "re-read vision pages at a higher level (MODEL CALLS)")
    s.add_argument("pile")
    s.add_argument("--level", choices=["tiles", "strong"], required=True)
    s.add_argument("--doc", help="limit to one document (relpath or doc id)")
    s.add_argument("--budget", type=int)
    s = add("status", cmd_status, "show progress, spend, review queue and output staleness")
    s.add_argument("pile", nargs="?")
    s = add("review", cmd_review, "list pages and claims waiting for the main model")
    s.add_argument("pile")
    s = add("page", cmd_page, "print a page's transcription; --image renders its image for viewing")
    s.add_argument("pile")
    s.add_argument("ref", help="D<n>:<page> or a page id")
    s.add_argument("--image", action="store_true")
    s.add_argument("--tiles", action="store_true", help="render high-resolution tiles instead of one image")
    s.add_argument("--suggested", action="store_true")
    s = add("fix", cmd_fix, "store the main model's corrected transcription for a page")
    s.add_argument("pile")
    s.add_argument("ref")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--file")
    g.add_argument("--suggested", action="store_true", help="use the spot check's suggested correction")
    s.add_argument("--summary")
    s.add_argument("--note")
    s = add("reclass", cmd_reclass, "force a page's class, e.g. read a garbled text page from its image")
    s.add_argument("pile")
    s.add_argument("ref")
    s.add_argument("--cls", choices=["vision", "hybrid", "text-ok", "blank"], required=True)
    s.add_argument("--note")
    s = add("accept", cmd_accept, "mark a reviewed page as fine as it is")
    s.add_argument("pile")
    s.add_argument("ref")
    s.add_argument("--note")
    s = add("verify", cmd_verify, "record the verdict on a claim flagged in the source")
    s.add_argument("pile")
    s.add_argument("ref")
    s.add_argument("--verdict", choices=["source-error", "correct", "unclear"], required=True)
    s.add_argument("--note", required=True)
    s = add("errata", cmd_errata, "list verified source errors and corrected transcriptions")
    s.add_argument("pile")
    s = add("digest", cmd_digest, "rebuild the digest (section digests only if approved)")
    s.add_argument("pile")
    s.add_argument("--no-sections", action="store_true")
    s.add_argument("--budget", type=int)
    add("profiles", cmd_profiles, "list output profiles")
    s = add("outputs", cmd_outputs, "show outputs and which are stale; --mark records a build")
    s.add_argument("pile")
    s.add_argument("--mark", metavar="PROFILE")
    s = add("postbuild", cmd_postbuild, "run a profile's post_build command in its output folder")
    s.add_argument("pile")
    s.add_argument("profile")
    s = add("unlock", cmd_unlock, "type the password for a protected PDF (run in your own terminal)")
    s.add_argument("pile")
    s.add_argument("relpath")
    s = add("forget", cmd_forget, "delete pile-digest's state for a pile (your files are untouched)")
    s.add_argument("pile")
    s.add_argument("--yes", action="store_true")
    s = add("gc", cmd_gc, "remove leftovers of files no longer in the pile")
    s.add_argument("pile")
    s = add("config", cmd_config, "show or set settings (global, or per pile with --pile)")
    s.add_argument("action", choices=["show", "set"])
    s.add_argument("key", nargs="?")
    s.add_argument("value", nargs="?")
    s.add_argument("--pile")
    s = add("ledger", cmd_ledger, "token use per stage and model")
    s.add_argument("pile")
    return ap


def main(argv=None) -> int:
    global JSON
    ap = build_parser()
    a = ap.parse_args(argv)
    JSON = a.json
    if a.cmd == "config" and a.action == "set" and (a.key is None or a.value is None):
        ap.error("config set needs KEY and VALUE")
    try:
        return int(a.fn(a) or 0)
    except PileDigestError as exc:
        if JSON:
            print(json.dumps({"error": str(exc)}))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted: everything finished so far is saved; run the same command to resume", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
