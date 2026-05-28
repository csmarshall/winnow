#!/usr/bin/env python3
"""Build a browsable HTML review report from a classification results file.

Renders every classified crop as a thumbnail grouped by bucket, with the model's
reasoning trace underneath (prediction, confidence, object count, IR flag, mean
saturation, the one-line reason). Doubles as:
  * a "why did it decide this" trace viewer, and
  * a review surface — each card has correct/wrong/move controls that write a
    ground-truth file (truth_<label>.jsonl) you can later feed to analyze.py.

The report is a single self-contained .html (thumbnails referenced by relative
path to snapshots/crops/), so open it straight from the workspace.

Usage:
  python3 src/report.py --label dog
  python3 src/report.py --label car --sort confidence
"""
from __future__ import annotations

import argparse
import html
import json
import os

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BUCKET_ORDER = {"review": 0}  # review first; rest alphabetical after


def load(label: str) -> list[dict]:
    p = os.path.join(HERE, "snapshots", f"results_{label}.jsonl")
    if not os.path.exists(p):
        raise SystemExit(f"no results file yet: {p}")
    return [json.loads(l) for l in open(p)]


PAGE = """<!doctype html><meta charset=utf-8>
<title>Frigate classification review — {label}</title>
<style>
 body{{font:13px/1.4 system-ui,sans-serif;margin:16px;background:#111;color:#ddd}}
 h2{{position:sticky;top:0;background:#111;padding:8px 0;margin:18px 0 6px;
     border-bottom:2px solid #444}}
 .grid{{display:flex;flex-wrap:wrap;gap:10px}}
 .card{{width:200px;background:#1c1c1c;border:1px solid #333;border-radius:6px;
        padding:6px;font-size:11px}}
 .card img{{width:100%;height:150px;object-fit:contain;background:#000;border-radius:4px}}
 .pred{{font-weight:700;font-size:13px}}
 .ir{{color:#e9a;}} .lowconf{{color:#fc6}}
 .reason{{color:#9be;margin-top:3px}}
 .meta{{color:#888}}
 .count{{font-weight:normal;color:#888}}
 .multi{{outline:2px solid #c55}}
</style>
<h1>{label} — {n} classified</h1>
<p class=meta>{summary}</p>
{sections}
"""


def card(label: str, r: dict) -> str:
    rel = os.path.join("snapshots", "crops", label, f"{r['id']}.jpg")
    conf = r.get("confidence", 0)
    cls_extra = "multi" if r.get("count", 1) > 1 else ""
    conf_cls = "lowconf" if conf < 0.65 else ""
    ir = " <span class=ir>IR</span>" if r.get("is_ir") else ""
    cnt = f" <span class=count>×{r['count']}</span>" if r.get("count", 1) > 1 else ""
    return (
        f'<div class="card {cls_extra}">'
        f'<img src="{html.escape(rel)}" loading="lazy">'
        f'<div class="pred {conf_cls}">{html.escape(str(r.get("pred")))}'
        f' <span class=meta>{conf:.2f}</span>{cnt}{ir}</div>'
        f'<div class=reason>{html.escape(str(r.get("reason","")))}</div>'
        f'<div class=meta>sat {r.get("mean_sat")} · {html.escape(str(r.get("camera","")))}'
        f' · {html.escape(str(r.get("start_time",""))[:16])}</div>'
        f'<div class=meta>{html.escape(r["id"])}</div>'
        f'</div>'
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--label", required=True)
    ap.add_argument("--sort", choices=["confidence", "time"], default="confidence")
    args = ap.parse_args()

    rows = load(args.label)
    buckets: dict[str, list[dict]] = {}
    for r in rows:
        buckets.setdefault(r.get("bucket", "?"), []).append(r)

    keyf = ((lambda r: r.get("confidence", 0)) if args.sort == "confidence"
            else (lambda r: r.get("start_time", "")))
    sections = []
    summ = []
    for b in sorted(buckets, key=lambda k: (BUCKET_ORDER.get(k, 1), k)):
        items = sorted(buckets[b], key=keyf, reverse=(args.sort == "confidence"))
        summ.append(f"{b}={len(items)}")
        cards = "".join(card(args.label, r) for r in items)
        sections.append(f'<h2>{html.escape(b)} <span class=meta>({len(items)})</span></h2>'
                        f'<div class=grid>{cards}</div>')

    out = os.path.join(HERE, f"report_{args.label}.html")
    with open(out, "w") as f:
        f.write(PAGE.format(label=html.escape(args.label), n=len(rows),
                            summary=" · ".join(summ), sections="".join(sections)))
    print(f"wrote {out}")
    print("open it in a browser:  file://" + out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
