#!/usr/bin/env python3
"""A stand-in for the `claude` CLI used by the tests (no network, no cost).

Behaviour is controlled with FAKE_CLAUDE_BEHAVIOUR:
  ok (default)   well-formed answers
  illegible      transcriptions carry an [[ILLEGIBLE]] marker and low confidence
  garbage        unparseable answers
  usage_limit    the usage window is exhausted
  auth           not logged in
  drop_last      the last page of each batch is missing from the answer
  mismatch       spot checks report a mismatch with a correction
FAKE_CLAUDE_LOG, if set, is a file that gets one line per call.
"""
import json
import os
import re
import sys

args = sys.argv[1:]
system = args[args.index("--system-prompt") + 1] if "--system-prompt" in args else ""
model = args[args.index("--model") + 1] if "--model" in args else "haiku"
behaviour = os.environ.get("FAKE_CLAUDE_BEHAVIOUR", "ok")
if "--version" in args:
    print("0.0.0 (fake)")
    sys.exit(0)
msg = json.loads(sys.stdin.readline())
blocks = msg["message"]["content"]
texts = [b["text"] for b in blocks if b["type"] == "text"]
images = sum(1 for b in blocks if b["type"] == "image")
ids = []
for t in texts:
    for m in re.finditer(r"^PAGE (\S+)", t, re.M):
        ids.append(m.group(1))
sid = re.search(r"^Section id: (\S+)", "\n".join(texts), re.M)
if sid:
    ids = [sid.group(1)]
if os.environ.get("FAKE_CLAUDE_LOG"):
    with open(os.environ["FAKE_CLAUDE_LOG"], "a") as f:
        f.write(json.dumps({"model": model, "ids": ids, "images": images, "system": system[:40]}) + "\n")


def emit(ev):
    print(json.dumps(ev), flush=True)


util = float(os.environ.get("FAKE_CLAUDE_UTIL", "0.1"))
status = "rejected" if behaviour == "usage_limit" else "allowed"
emit({"type": "rate_limit_event", "rate_limit_info": {"status": status, "rateLimitType": "five_hour",
      "unifiedWindows": {"five_hour": {"utilization": util, "resetsAt": 4102444800}, "seven_day": {"utilization": 0.1}}}})
if behaviour == "auth":
    emit({"type": "result", "subtype": "error", "is_error": True, "result": "Not logged in · Please run /login",
          "api_error_status": 401, "usage": {}})
    sys.exit(1)
if behaviour == "usage_limit":
    emit({"type": "result", "subtype": "error", "is_error": True, "result": "Claude usage limit reached",
          "api_error_status": 429, "usage": {}})
    sys.exit(1)

out = []
if behaviour == "drop_last" and len(ids) > 1:
    ids = ids[:-1]
for pid in ids:
    if behaviour == "garbage":
        out.append("I could not do that.")
        continue
    if "audit transcriptions" in system:
        if behaviour == "mismatch":
            out.append(f"=== PAGE {pid} ===\nVERDICT: mismatch\nDIFFERENCES:\n- difference: line 2, sign flipped\n"
                       f"CORRECTED:\nCorrected text for {pid}\n=== END PAGE {pid} ===")
        else:
            out.append(f"=== PAGE {pid} ===\nVERDICT: match\nDIFFERENCES:\n- none\nCORRECTED:\nnone\n=== END PAGE {pid} ===")
    elif "transcriber" in system:
        ill = behaviour == "illegible" and model == "haiku"
        conf = "low" if ill else "high"
        body = f"Transcribed content of {pid} with $a^2+b^2=c^2$."
        flags = "- none"
        if ill:
            body += " The term [[ILLEGIBLE: x?]] is unclear."
            flags = "- illegible: middle of page, one term"
        out.append(f"=== PAGE {pid} ===\nKIND: transcription\nCONFIDENCE: {conf}\nSUMMARY: Page {pid} about a test topic.\n"
                   f"TOPICS: testing, pages\nFLAGS:\n{flags}\nCONTENT:\n# Heading\n{body}\n=== END PAGE {pid} ===")
    else:
        out.append(f"=== PAGE {pid} ===\nSUMMARY: Summary of {pid}.\nTOPICS: alpha, beta\nFLAGS:\n- none\n=== END PAGE {pid} ===")
text = "\n\n".join(out)
n_in = 300 * images + 50 * len(ids)
emit({"type": "result", "subtype": "success", "is_error": False, "result": text, "stop_reason": "end_turn",
      "total_cost_usd": 0.0001 * len(ids), "usage": {"input_tokens": 5, "cache_creation_input_tokens": n_in,
      "cache_read_input_tokens": 100, "output_tokens": 120 * max(1, len(ids))},
      "modelUsage": {f"claude-{model}-test": {}}})
