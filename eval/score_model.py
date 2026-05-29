#!/usr/bin/env python3
"""Offline scorer for a Frigate classification model.tflite against a labeled holdout.

Frigate exposes NO eval/test/predict API (inference is live-only), but it saves the
trained model to `model_cache/<model>/model.tflite` + `labelmap.txt`. So we run the
model ourselves and report accuracy + a confusion matrix. Use it to A/B an
"everything" model vs a diversity-"reduced" model on the SAME holdout (task #5).

The holdout is a directory of crops grouped by TRUE class:
    holdout/<TrueClass>/<crop>.png      (class names must match labelmap.txt)

Preprocessing matches MobileNetV2 conventions and is read from the model's input
tensor. Absolute accuracy depends on matching Frigate's exact preprocessing, but the
RELATIVE comparison (everything vs reduced) is valid because both models are scored
through this identical pipeline.

Usage:
  python3 eval/score_model.py \
      --model /opt/sw/frigate/config/model_cache/Dogs/model.tflite \
      --labels /opt/sw/frigate/config/model_cache/Dogs/labelmap.txt \
      --holdout /path/to/holdout/Dogs
"""
from __future__ import annotations

import argparse
import collections
import os

import numpy as np
from PIL import Image


def _interpreter(model_path):
    """LiteRT (ai_edge_litert) preferred; fall back to tflite_runtime, then full TF."""
    for mod, attr in (("ai_edge_litert.interpreter", "Interpreter"),
                      ("tflite_runtime.interpreter", "Interpreter")):
        try:
            m = __import__(mod, fromlist=[attr])
            return getattr(m, attr)(model_path=model_path)
        except Exception:
            continue
    from tensorflow.lite import Interpreter  # last resort
    return Interpreter(model_path=model_path)


def load_labels(path):
    return [ln.strip() for ln in open(path) if ln.strip()]


def preprocess(path, in_shape, in_dtype):
    _, h, w, _ = in_shape
    im = Image.open(path).convert("RGB").resize((int(w), int(h)))
    a = np.asarray(im)
    if np.issubdtype(in_dtype, np.floating):
        a = (a.astype(np.float32) / 127.5) - 1.0     # MobileNetV2 [-1, 1]
    return np.expand_dims(a.astype(in_dtype), 0)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", required=True, help="path to model.tflite")
    ap.add_argument("--labels", required=True, help="path to labelmap.txt")
    ap.add_argument("--holdout", required=True,
                    help="dir of <TrueClass>/<crop> subdirs (class names match labelmap)")
    args = ap.parse_args()

    labels = load_labels(args.labels)
    it = _interpreter(args.model)
    it.allocate_tensors()
    inp, outp = it.get_input_details()[0], it.get_output_details()[0]
    shape, dtype = inp["shape"], inp["dtype"]

    true_classes = sorted(d for d in os.listdir(args.holdout)
                          if os.path.isdir(os.path.join(args.holdout, d)))
    if not true_classes:
        print(f"no <TrueClass>/ subdirs under {args.holdout}")
        return 2
    unknown = [c for c in true_classes if c not in labels]
    if unknown:
        print(f"WARNING: holdout classes not in labelmap (counted as always-wrong): {unknown}")

    conf = {t: collections.Counter() for t in true_classes}
    n = correct = 0
    for true in true_classes:
        d = os.path.join(args.holdout, true)
        for f in sorted(os.listdir(d)):
            p = os.path.join(d, f)
            if not os.path.isfile(p) or f.startswith("."):
                continue
            try:
                x = preprocess(p, shape, dtype)
            except Exception as e:
                print(f"  ! skip {f}: {e}")
                continue
            it.set_tensor(inp["index"], x)
            it.invoke()
            y = it.get_tensor(outp["index"])[0]
            pred = labels[int(np.argmax(y))]
            conf[true][pred] += 1
            n += 1
            correct += int(pred == true)

    if n == 0:
        print("no crops scored")
        return 2
    print(f"\nmodel:   {args.model}")
    print(f"holdout: {args.holdout}")
    print(f"\nACCURACY: {correct}/{n} = {100*correct/n:.1f}%\n")
    preds = labels
    hdr = "true \\ pred".ljust(16) + "".join(p[:10].rjust(11) for p in preds)
    print(hdr)
    for t in true_classes:
        row = t[:15].ljust(16) + "".join(str(conf[t].get(p, 0)).rjust(11) for p in preds)
        print(row)
    # per-class recall
    print("\nper-class recall:")
    for t in true_classes:
        tot = sum(conf[t].values())
        print(f"  {t:16} {conf[t].get(t,0)}/{tot} = {100*conf[t].get(t,0)/max(1,tot):.0f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
