#!/usr/bin/env python3
"""Render the README charts from data.json as plain SVG (standard library only).

The SVGs carry their own light and dark colours (prefers-color-scheme), so they
read well on GitHub in either theme. Run: python3 docs/stats/make_charts.py
"""

from __future__ import annotations

import json
from pathlib import Path
from xml.sax.saxutils import escape

HERE = Path(__file__).resolve().parent
W = 760
FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"

# Validated categorical slots 1-3 (light / dark), text and axis tokens.
STYLE = """
<style>
  .t1 { fill: #0b0b0b; } .t2 { fill: #52514e; } .t3 { fill: #6b6a65; }
  .grid { stroke: #e1e0d9; } .base { stroke: #c3c2b7; }
  .s1 { fill: #2a78d6; } .s2 { fill: #eb6834; } .s3 { fill: #1baf7a; } .s0 { fill: #a3a29b; }
  .hatch-line { stroke: #2a78d6; } .hatch-bg { fill: #2a78d6; fill-opacity: 0.18; }
  .gap { stroke: #ffffff; }
  @media (prefers-color-scheme: dark) {
    .t1 { fill: #f0efec; } .t2 { fill: #c3c2b7; } .t3 { fill: #a3a29b; }
    .grid { stroke: #2c2c2a; } .base { stroke: #383835; }
    .s1 { fill: #3987e5; } .s2 { fill: #d95926; } .s3 { fill: #199e70; } .s0 { fill: #6b6a65; }
    .hatch-line { stroke: #3987e5; } .hatch-bg { fill: #3987e5; fill-opacity: 0.22; }
    .gap { stroke: #0d1117; }
  }
  text { font-family: %s; }
</style>
""" % FONT


def svg(h: int, title: str, desc: str, body: str) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{h}" viewBox="0 0 {W} {h}" role="img" '
            f'aria-labelledby="t d"><title id="t">{escape(title)}</title><desc id="d">{escape(desc)}</desc>'
            f'{STYLE}{body}</svg>\n')


def text(x, y, s, cls="t1", size=13, anchor="start", weight=400) -> str:
    return (f'<text x="{x:.1f}" y="{y:.1f}" class="{cls}" font-size="{size}" text-anchor="{anchor}" '
            f'font-weight="{weight}">{escape(s)}</text>')


def bar(x, y, w, h, cls, r=4) -> str:
    """A horizontal bar anchored at the baseline (x), rounded only at its data end."""
    w = max(w, 2)
    r = min(r, w / 2, h / 2)
    return (f'<path class="{cls}" d="M{x:.1f},{y:.1f} H{x + w - r:.1f} Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f} '
            f'V{y + h - r:.1f} Q{x + w:.1f},{y + h:.1f} {x + w - r:.1f},{y + h:.1f} H{x:.1f} Z"/>')


def human(n: float) -> str:
    if n >= 1e6:
        return f"{n / 1e6:.2f}M".replace(".00M", "M")
    if n >= 1e3:
        return f"{n / 1e3:.0f}k" if n >= 1e4 else f"{n / 1e3:.1f}k"
    return str(int(n))


def ticks(xmax: float, x0: float, plot_w: float, top: float, bottom: float, step: float, fmt) -> str:
    out = []
    v = 0.0
    while v <= xmax * 1.0001:
        x = x0 + plot_w * v / xmax
        out.append(f'<line class="grid" x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{bottom}" stroke-width="1"/>')
        out.append(text(x, bottom + 16, fmt(v), "t3", 11, "middle"))
        v += step
    return "".join(out)


# ----------------------------------------------------------------- charts


def chart_triage(d: dict) -> str:
    parts = d["triage"]
    total = sum(p["pages"] for p in parts)
    x0, x1, y, h = 24, W - 24, 92, 34
    plot_w = x1 - x0
    body = [text(24, 30, f"How the {total} pages were handled", "t1", 16, weight=600),
            text(24, 52, "Local triage decides, for free, which pages a model has to look at.", "t2", 13)]
    # Legend with counts (identity is never colour alone).
    lx = 24
    for i, p in enumerate(parts, 1):
        body.append(f'<rect class="s{i}" x="{lx}" y="64" width="10" height="10" rx="2"/>')
        lab = f"{p['label']}: {p['pages']}"
        body.append(text(lx + 15, 73, lab, "t2", 12))
        lx += 15 + len(lab) * 6.6 + 22
    x = x0
    for i, p in enumerate(parts, 1):
        w = plot_w * p["pages"] / total
        last = i == len(parts)
        if last:
            body.append(bar(x, y, w, h, f"s{i}"))
        else:
            body.append(f'<rect class="s{i}" x="{x:.1f}" y="{y}" width="{w:.1f}" height="{h}"/>')
        if i > 1:
            body.append(f'<line class="gap" x1="{x:.1f}" y1="{y}" x2="{x:.1f}" y2="{y + h}" stroke-width="2"/>')
        share = p["pages"] / total
        cx = x + w / 2
        if w > 90:
            body.append(text(cx, y + h + 20, f"{share:.0%} · {p['note']}", "t2", 12, "middle"))
        x += w
    body.append(text(24, y + h + 44, "Skipped and free pages cost nothing; text-layer pages cost a few hundred cheap-model "
                                    "tokens; only image pages pay for vision.", "t3", 11.5))
    return svg(y + h + 60, "How the pages were handled", "Stacked bar of pages by treatment: " +
               ", ".join(f"{p['label']} {p['pages']}" for p in parts), "".join(body))


def chart_main(d: dict) -> str:
    rows = d["main_model_reading"]
    xmax = 450000
    x0, lab_w, top, rh, bh = 24, 200, 74, 40, 20
    plot_x = x0 + lab_w
    plot_w = W - plot_x - 90
    bottom = top + rh * len(rows)
    body = [text(24, 30, "What the main model has to read", "t1", 16, weight=600),
            text(24, 52, "Tokens of input for the expensive model to get oriented in the same pile.", "t2", 13)]
    body.append(ticks(xmax, plot_x, plot_w, top - 6, bottom, 100000, lambda v: human(v) if v else "0"))
    for i, r in enumerate(rows):
        y = top + i * rh + (rh - bh) / 2
        cls = "s0" if r["kind"] == "baseline" else "s1"
        w = plot_w * r["tokens"] / xmax
        body.append(text(plot_x - 10, y + bh / 2 + 4.5, r["label"], "t1", 13, "end"))
        body.append(bar(plot_x, y, w, bh, cls))
        body.append(text(plot_x + w + 8, y + bh / 2 + 4.5, f"{human(r['tokens'])}", "t1", 13, weight=600))
    body.append(f'<line class="base" x1="{plot_x}" y1="{top - 6}" x2="{plot_x}" y2="{bottom}" stroke-width="1"/>')
    base = rows[0]["tokens"]
    best = rows[-1]["tokens"]
    body.append(text(24, bottom + 44, f"The overview is {base / best:.0f}× smaller than the pile. Grey: estimate for "
                                      "reading every page; blue: measured digest size.", "t3", 11.5))
    return svg(bottom + 60, "What the main model has to read", "Bar chart: " +
               ", ".join(f"{r['label']} {human(r['tokens'])} tokens" for r in rows), "".join(body))


def chart_stages(d: dict) -> str:
    rows = d["stages"]
    xmax = 900000
    x0, lab_w, top, rh, bh = 24, 200, 92, 40, 20
    plot_x = x0 + lab_w
    plot_w = W - plot_x - 120
    bottom = top + rh * len(rows)
    total = sum(r["tokens"] for r in rows)
    body = [text(24, 30, "Where the worker tokens went", "t1", 16, weight=600),
            text(24, 52, f"Measured, per stage, for the 1.0 run ({human(total)} tokens; API-price equivalent "
                         f"${sum(r['usd'] for r in rows):.2f}).", "t2", 13)]
    body.append('<defs><pattern id="hatch" width="6" height="6" patternUnits="userSpaceOnUse" '
                'patternTransform="rotate(45)"><rect class="hatch-bg" width="6" height="6"/>'
                '<line class="hatch-line" x1="0" y1="0" x2="0" y2="6" stroke-width="2"/></pattern></defs>')
    lx = 24
    for cls, lab in (("s1", "Haiku (cheap)"), ("s2", "Sonnet (stronger)")):
        body.append(f'<rect class="{cls}" x="{lx}" y="64" width="10" height="10" rx="2"/>')
        body.append(text(lx + 15, 73, lab, "t2", 12))
        lx += 15 + len(lab) * 6.6 + 22
    body.append('<rect fill="url(#hatch)" x="%d" y="64" width="10" height="10" rx="2"/>' % lx)
    body.append(text(lx + 15, 73, "removed in 2.0", "t2", 12))
    body.append(ticks(xmax, plot_x, plot_w, top - 6, bottom, 200000, lambda v: human(v) if v else "0"))
    for i, r in enumerate(rows):
        y = top + i * rh + (rh - bh) / 2
        w = plot_w * r["tokens"] / xmax
        body.append(text(plot_x - 10, y + bh / 2 + 4.5, r["label"], "t1", 13, "end"))
        if r.get("removed_in"):
            p = bar(plot_x, y, w, bh, "s1").replace('class="s1"', 'fill="url(#hatch)"')
            body.append(p)
        else:
            body.append(bar(plot_x, y, w, bh, "s1" if r["model"] == "Haiku" else "s2"))
        body.append(text(plot_x + w + 8, y + bh / 2 + 4.5, f"{human(r['tokens'])} · ${r['usd']:.2f}", "t1", 13))
    body.append(f'<line class="base" x1="{plot_x}" y1="{top - 6}" x2="{plot_x}" y2="{bottom}" stroke-width="1"/>')
    sonnet = sum(r["usd"] for r in rows if r["model"] == "Sonnet") / sum(r["usd"] for r in rows)
    body.append(text(24, bottom + 44, f"Sonnet did {sum(r['tokens'] for r in rows if r['model'] == 'Sonnet') / total:.0%} "
                                      f"of the tokens but {sonnet:.0%} of the cost. 2.0 drops the tiles stage and sends "
                                      "flagged pages straight to Sonnet.", "t3", 11.5))
    return svg(bottom + 60, "Where the worker tokens went", "Bar chart of tokens per stage: " +
               ", ".join(f"{r['label']} ({r['model']}) {human(r['tokens'])}" for r in rows), "".join(body))


def chart_caching(d: dict) -> str:
    rows = d["caching"]
    xmax = 0.0005
    x0, lab_w, top, rh, bh = 24, 200, 74, 40, 20
    plot_x = x0 + lab_w
    plot_w = W - plot_x - 110
    bottom = top + rh * len(rows)
    body = [text(24, 30, "Cost of one image call", "t1", 16, weight=600),
            text(24, 52, "Same page image, same prompt, measured API-price equivalent per call.", "t2", 13)]
    body.append(ticks(xmax, plot_x, plot_w, top - 6, bottom, 0.0001, lambda v: f"${v:.4f}" if v else "$0"))
    for i, r in enumerate(rows):
        y = top + i * rh + (rh - bh) / 2
        w = plot_w * r["usd"] / xmax
        body.append(text(plot_x - 10, y + bh / 2 + 4.5, r["label"], "t1", 13, "end"))
        body.append(bar(plot_x, y, w, bh, "s0" if i == 0 else "s1"))
        body.append(text(plot_x + w + 8, y + bh / 2 + 4.5, f"${r['usd']:.5f}", "t1", 13, weight=600))
    body.append(f'<line class="base" x1="{plot_x}" y1="{top - 6}" x2="{plot_x}" y2="{bottom}" stroke-width="1"/>')
    saved = 1 - rows[1]["usd"] / rows[0]["usd"]
    body.append(text(24, bottom + 44, f"{saved:.0%} cheaper: each worker call carries new images, so cache writes were "
                                      "rarely read back.", "t3", 11.5))
    return svg(bottom + 60, "Cost of one image call", "Bar chart: " +
               ", ".join(f"{r['label']} ${r['usd']}" for r in rows), "".join(body))


def main() -> None:
    d = json.loads((HERE / "data.json").read_text())
    out = {"triage.svg": chart_triage(d), "main-model.svg": chart_main(d), "stages.svg": chart_stages(d),
           "caching.svg": chart_caching(d)}
    for name, s in out.items():
        (HERE / name).write_text(s, encoding="utf-8")
        print("wrote", HERE / name)


if __name__ == "__main__":
    main()
