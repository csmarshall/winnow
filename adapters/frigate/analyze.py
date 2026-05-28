#!/usr/bin/env python3
"""Diagnose classifier accuracy against human verdicts (ground truth).

Joins review/verdicts.jsonl (yes = prediction confirmed, no = prediction wrong)
with the classifier's results_<label>.jsonl (the prediction + logged signals:
confidence, is_ir, mean_sat, camera). For each identity it reports precision and
then breaks the ERRORS down by every signal we logged — so "why did it miss"
becomes "82% of the bad Scrappies were IR night frames" instead of a guess.

Usage:
  python3 src/analyze.py --label dog
  python3 src/analyze.py --label car
"""
from __future__ import annotations

import argparse
import collections
import json
import os

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_verdicts() -> dict:
    v = {}
    p = os.path.join(HERE, "review", "verdicts.jsonl")
    for line in open(p):
        d = json.loads(line)
        if d.get("verdict") == "__undo__":
            v.pop(d["cid"], None)
        else:
            v[d["cid"]] = d["verdict"]
    return v


def conf_band(c):
    if c is None:
        return "n/a"
    for lo in (0.95, 0.9, 0.8, 0.7, 0.0):
        if c >= lo:
            return f">={lo}"
    return "?"


def sat_band(s):
    if s is None:
        return "n/a"
    return "IR(<18)" if s < 18 else "20s" if s < 30 else "30-60" if s < 60 else "60+"


def breakdown(rows, signal):
    c = collections.Counter(signal(r) for r in rows)
    return ", ".join(f"{k}:{v}" for k, v in sorted(c.items(), key=lambda x: -x[1]))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--label", required=True)
    args = ap.parse_args()

    verdicts = load_verdicts()
    results = {}
    for line in open(os.path.join(HERE, "snapshots", f"results_{args.label}.jsonl")):
        r = json.loads(line)
        results[r["id"]] = r

    # join: cid = "<label>|<identity>|<event_id>"
    per_id = collections.defaultdict(lambda: {"yes": [], "no": [], "skip": []})
    for cid, verdict in verdicts.items():
        parts = cid.split("|", 2)
        if len(parts) != 3 or parts[0] != args.label:
            continue
        _, identity, eid = parts
        r = results.get(eid)
        if r:
            per_id[identity][verdict].append(r)

    print(f"=== {args.label} accuracy vs your verdicts ===\n")
    for identity in sorted(per_id):
        d = per_id[identity]
        ny, nn = len(d["yes"]), len(d["no"])
        prec = ny / (ny + nn) * 100 if (ny + nn) else 0
        print(f"## {identity}: {ny}✓ / {nn}✗  ({prec:.0f}% precision)")
        if nn == 0:
            print("   no errors — clean.\n")
            continue
        errs = d["no"]
        print(f"   errors by IR/day      : {breakdown(errs, lambda r: 'IR' if r['is_ir'] else 'day')}")
        print(f"   errors by confidence  : {breakdown(errs, lambda r: conf_band(r.get('confidence')))}")
        print(f"   errors by saturation  : {breakdown(errs, lambda r: sat_band(r.get('mean_sat')))}")
        print(f"   errors by camera      : {breakdown(errs, lambda r: r.get('camera','?'))}")
        # contrast: what did the CORRECT ones look like, for the same signals?
        ok = d["yes"]
        if ok:
            ir_err = sum(1 for r in errs if r["is_ir"]) / len(errs) * 100
            ir_ok = sum(1 for r in ok if r["is_ir"]) / len(ok) * 100
            print(f"   IR rate: errors {ir_err:.0f}% vs correct {ir_ok:.0f}%")
        print()

    # global takeaway
    all_err = [r for d in per_id.values() for r in d["no"]]
    all_ok = [r for d in per_id.values() for r in d["yes"]]
    if all_err:
        ir_e = sum(1 for r in all_err if r["is_ir"]) / len(all_err) * 100
        print(f"OVERALL: {len(all_ok)}✓ / {len(all_err)}✗  "
              f"({len(all_ok)/(len(all_ok)+len(all_err))*100:.0f}% precision); "
              f"{ir_e:.0f}% of all errors are IR/night frames")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
