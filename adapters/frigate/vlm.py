#!/usr/bin/env python3
"""Reusable, source-agnostic perception logic for the local VLM pre-sort.

Holds the priming schemes (ADR-0002: the model reports neutral observations;
identity is decided in code here), the Ollama call, and the bucketing decision.
Operates on a PIL image — it doesn't care whether that came from a Frigate train
file, a crop, or anywhere else.
"""
from __future__ import annotations

import base64
import io
import json
import os
import urllib.request

import numpy as np
from PIL import Image

OLLAMA = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
VLM_MODEL = os.environ.get("WINNOW_VLM_MODEL", "qwen2.5vl:7b")
IR_SAT_THRESH = 18  # mean HSV saturation (0-255) below this => IR/grayscale


# ---- identity mapping (code, not the model — see ADR-0002) ------------------
def dog_decide(p: dict) -> str:
    """Map neutral observations -> dog identity by transparent scoring.
    Relies on proportions (build), the blue harness, and coat PATTERN (brindle
    stripes vs solid black); ambiguous evidence -> 'unsure' not a wrong guess."""
    build = str(p.get("build", "")).lower()
    coat = str(p.get("coat", "")).lower()
    ears = str(p.get("ears", "")).lower()
    harness = bool(p.get("harness"))
    # harness weight 4 (validated): outweighs a brindle reading on the ambiguous
    # stocky+brindle+harness cell. +12pts Scrappy recall for a little Scooby recall.
    # ears: Scrappy (part chihuahua) erect/up; Scooby floppy/down — survives night.
    scrappy = (4 if harness else 0) + (build == "lean") + (coat == "solid_black") + (ears == "up")
    scooby = 2 * (coat == "brindle") + (build == "stocky") + (ears == "down")
    if max(scrappy, scooby) == 0 or scrappy == scooby:
        return "unsure"
    return "Scrappy" if scrappy > scooby else "Scooby"


SCHEMES = {
    "dog": {
        "count_gate": True,    # multi-dog frames aren't clean examples
        "ir_review": False,    # proportions/harness survive night; don't dump IR
        "mapper": dog_decide,  # observations -> identity in code
        "prompt": (
            "Report neutral observations about the SINGLE most prominent dog in "
            "this cropped security-camera image. Do NOT name or identify the dog. "
            "Judge SHAPE/PROPORTIONS and COAT PATTERN — NOT absolute size (the "
            "image is cropped, so scale is meaningless).\n"
            "Report exactly:\n"
            "- build: \"lean\" (delicate, slender, thin legs, fine small head — "
            "toy/chihuahua-like proportions) | \"stocky\" (muscular, blocky, "
            "broad, heavy-boned) | \"unclear\".\n"
            "- coat: \"brindle\" (brown/tan with visible darker tiger-striping, "
            "or a mixed brown-and-black pattern) | \"solid_black\" (uniformly "
            "black, no brown and no stripes anywhere) | \"other\".\n"
            "- harness: true if the dog wears a harness or vest (often blue), "
            "else false.\n"
            "- ears: \"up\" (erect / pricked / standing up, chihuahua-like) | "
            "\"down\" (floppy / folded, lying along the side of the head) | "
            "\"unclear\".\n"
            "Judge the coat accurately in BOTH directions: call it brindle only "
            "if you actually see brown tones or striping; call it solid_black if "
            "it is uniformly black with no brown anywhere. Do not assume a dark "
            "dog is brindle, and do not assume a brown-tinted dog is solid black.\n"
            'Respond ONLY with JSON: {"build":"lean|stocky|unclear",'
            '"coat":"brindle|solid_black|other","ears":"up|down|unclear",'
            '"harness":true|false,"num_dogs":<int>,"confidence":0.0-1.0,'
            '"grayscale":true|false,"reason":"<=15 words"}'
        ),
    },
    "car": {
        "key": "color",
        "count_gate": False,   # VLM vehicle counts are unreliable; don't gate
        "ir_review": True,     # car identity IS color -> defer IR/night frames
        "value_map": {
            "white": "DeLorean",
            "dark": "Batmobile", "dark_gray": "Batmobile", "dark gray": "Batmobile",
            "other": "other", "unknown": "unsure",
        },
        "prompt": (
            "Look at the SINGLE most prominent vehicle in this cropped security-"
            "camera image (the largest / most centered one; ignore any partial "
            "vehicle at the very edge). Report only its body COLOR. Do NOT count "
            "vehicles, do NOT name any make or model, do NOT read badges.\n"
            "Choose exactly one color:\n"
            "- \"white\"  = a white or off-white/cream vehicle.\n"
            "- \"dark\"   = a dark gray, charcoal, or black vehicle.\n"
            "- \"other\"  = a clearly different color (red, blue, silver, tan, etc.).\n"
            "- \"unknown\"= grayscale/infrared night image where color is "
            "unreadable, OR the vehicle is too occluded to judge color.\n"
            "Judge confidently from the paint color you can see; a clearly white "
            "vehicle is high confidence \"white\".\n"
            'Respond ONLY with JSON: {"color":"white|dark|other|unknown",'
            '"confidence":0.0-1.0,"grayscale":true|false,"reason":"<=15 words"}'
        ),
    },
}

# attributes worth logging from the model response (for debugging the mapper)
OBS_KEYS = ("build", "coat", "ears", "harness", "color")


def mean_saturation(img: Image.Image) -> float:
    return float(np.asarray(img.convert("HSV"))[:, :, 1].mean())


def to_b64_jpeg(img: Image.Image, max_side: int = 768) -> str:
    img = img.convert("RGB")
    img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode()


def ask_vlm(prompt: str, b64: str, model: str = None) -> dict:
    body = {
        "model": model or VLM_MODEL,
        "messages": [{"role": "user", "content": prompt, "images": [b64]}],
        "stream": False, "format": "json", "options": {"temperature": 0},
    }
    req = urllib.request.Request(f"{OLLAMA}/api/chat", data=json.dumps(body).encode(),
                                headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        resp = json.load(r)
    return json.loads(resp["message"]["content"])


def decide_bucket(scheme: dict, pred: dict, conf_threshold: float, is_ir: bool):
    """pred (model observations) -> (identity, bucket, confidence, count).
    Ambiguous/low-confidence/multi/IR-when-color-matters route to 'review'."""
    if scheme.get("mapper"):
        cls = scheme["mapper"](pred)
    else:
        raw = str(pred.get(scheme["key"], "")).lower().strip()
        cls = scheme["value_map"].get(raw, "unsure")
    conf = float(pred.get("confidence", 0) or 0)
    count = pred.get("num_dogs", pred.get("num_vehicles", 1)) or 1
    try:
        count = int(count)
    except (TypeError, ValueError):
        count = 1
    bucket = cls
    if (cls == "unsure" or conf < conf_threshold
            or (scheme.get("count_gate") and count > 1)
            or (scheme.get("ir_review") and is_ir and cls not in ("other", "unsure"))):
        bucket = "review"
    return cls, bucket, conf, count


def classify_image(img: Image.Image, scheme: dict, conf_threshold: float,
                   model: str = None) -> dict:
    """Full single-image pass: IR check + VLM + bucketing. Returns a result dict
    (no I/O; caller persists)."""
    sat = mean_saturation(img)
    is_ir = sat < IR_SAT_THRESH
    pred = ask_vlm(scheme["prompt"], to_b64_jpeg(img), model)
    cls, bucket, conf, count = decide_bucket(scheme, pred, conf_threshold, is_ir)
    return {
        "pred": cls, "bucket": bucket, "confidence": conf, "count": count,
        "is_ir": is_ir, "mean_sat": round(sat, 1),
        "obs": {k: pred.get(k) for k in OBS_KEYS if k in pred},
        "reason": pred.get("reason", ""),
    }
