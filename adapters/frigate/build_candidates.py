#!/usr/bin/env python3
"""Build the review queue from native train-pool classifications + face library.

Reads results_<model>.jsonl (from classify.py) and the Frigate face library
(via the API), and writes review/candidates.jsonl — the source-agnostic queue the
swipe app consumes. Each candidate carries what `commit.py` needs to act through
the API (model + training_file) and a browser-loadable image URL.

role=positive  -> confirmed images get `categorize`d into dataset/<identity>
role=negative  -> the classifier's "other" bucket; confirmed -> dataset/none
(faces: role=positive; rejects are deleted via the faces API at commit.)

Usage:  python3 build_candidates.py
"""
from __future__ import annotations

import json
import os
import re

from frigate_client import FrigateClient
from commit import _keep_subset   # the per-event keep-set selector (ADR-0015)

HERE = os.path.dirname(os.path.abspath(__file__))
REVIEW = os.path.join(HERE, "..", "..", "review")
CLIPS_DISK = os.environ.get("FRIGATE_CLIPS_DISK", "/opt/nvr/frigate/clips")
FACE_SKIP = {"train"}
NEEDS_ID = "Needs ID"   # pool for faces Frigate detected but couldn't name (ADR-0014)

# Frigate burns a timestamp into its event snapshot, rendered in the Frigate
# container's TZ (UTC on the host) -> looks wrong vs local, and the camera's own OSD
# already shows local time. Off by default; set WINNOW_SNAPSHOT_TIMESTAMP=1 to
# keep Frigate's overlay. (Camera-OSD timestamp is a camera setting, not ours.)
SHOW_TIMESTAMP = os.environ.get("WINNOW_SNAPSHOT_TIMESTAMP", "").lower() in (
    "1", "true", "yes")

# Normally one frame per event (the best/clearest) — reviewing every near-identical
# frame is the 'duplicates' problem. Set WINNOW_DEDUP=0 for a shakedown run that
# surfaces EVERY frame (max volume, lots of duplicates).
DEDUP = os.environ.get("WINNOW_DEDUP", "1").lower() in ("1", "true", "yes")

# Model config is DISCOVERED from Frigate at refresh time (see discover_models) —
# no model names are hardcoded, so the repo never contains the real ones. These
# fictional examples are only the offline/test fallback when Frigate's config is
# unreachable. (Shape: per model -> kind, positive identities, negative label, noun.)
EXAMPLE_MODELS = {
    # A multi-class dog classifier (categories: Scooby / Scrappy / none).
    "Scooby": {"kind": "dog", "identities": ["Scooby", "Scrappy"],
               "negative": "Other dog", "noun": "dog"},
    # Each car is its OWN binary sub_label classifier (categories: <car> / none).
    "Batmobile": {"kind": "car", "identities": ["Batmobile"],
                  "negative": "none", "noun": "car"},
    "DeLorean": {"kind": "car", "identities": ["DeLorean"],
                 "negative": "none", "noun": "car"},
}


def discover_models(client) -> dict:
    """Build the dog/car model config from Frigate's own config — no hardcoded
    names. Each custom classification model's object type gives the kind; its
    dataset categories (minus 'none') give the identities. People are handled via
    face recognition, so person-classifiers are skipped. Falls back to
    EXAMPLE_MODELS when Frigate's config can't be read (offline / tests)."""
    try:
        custom = ((client.get_config().get("classification") or {}).get("custom")) or {}
    except Exception as e:
        print(f"  (model discovery failed: {e}; using example models)")
        return dict(EXAMPLE_MODELS)
    out = {}
    for name, mc in custom.items():
        if not mc.get("enabled", True):
            continue
        objs = (mc.get("object_config") or {}).get("objects") or []
        kind = "dog" if "dog" in objs else "car" if "car" in objs else None
        if kind is None:
            continue   # person-classifiers etc. — people come via faces
        try:
            cats = [c for c in (client.get_dataset(name).get("categories") or {})
                    if c != "none"]
        except Exception:
            cats = []
        out[name] = {"kind": kind, "identities": cats or [name],
                     "negative": "none", "noun": kind}
    return out or dict(EXAMPLE_MODELS)


def cid(model, identity, training_file):
    return f"{model}|{identity}|{training_file}"


def results_path(model):
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", model)
    return os.path.join(REVIEW, f"results_{safe}.jsonl")


def train_event_id(fname):
    parts = fname.split("-")
    return "-".join(parts[:2]) if len(parts) >= 2 else fname


def _real_event(filename):
    """True for real Frigate train crops (named <unixts>.<frac>-<rand>-…). Seed/
    sample files like example_003.jpg aren't real events — no snapshot/clip — so
    they can't be reviewed with context and are excluded."""
    return bool(re.match(r"\d+\.\d+-", filename or ""))


def _dedup_by_event(files):
    """One frame per event. Frigate emits many near-identical frames per event;
    reviewing all of them is the 'duplicates' problem. Deterministic (sorted)."""
    seen, out = set(), []
    for f in sorted(files):
        eid = train_event_id(f)
        if eid not in seen:
            seen.add(eid)
            out.append(f)
    return out


def _score_of(f):
    """Confidence from a train filename …-<label>-<score>.ext. Unclassified crops
    (e.g. cars at 0.0) and unparseable names score 0.0 -> tie-broken by filename."""
    try:
        return float(f.rsplit(".", 1)[0].split("-")[-1])
    except (ValueError, IndexError):
        return 0.0


def _best_by_event(files):
    """One frame per event — the highest-score (clearest/most-confident) crop, so a
    sharp shot wins over a near-duplicate bad one. Ties break deterministically by
    filename (sorted). This is the manual-mode analogue of from_model's best-pick."""
    best = {}
    for f in sorted(files):
        eid = train_event_id(f)
        if eid not in best or _score_of(f) > _score_of(best[eid]):
            best[eid] = f
    return list(best.values())


def _attach_media(client, eid, evcache, rec):
    """Full-frame still + clip for an event — for dogs, cars AND faces alike.

    full_url = Frigate's EVENT snapshot with bbox=1: Frigate renders the frame and
    the box together in the same coordinate space, so the box is correctly on the
    tracked object on EVERY camera — including fisheye doorbells where detect and
    record streams are dewarped differently. (We tried drawing Frigate's normalized
    coords onto the record-res recording frame at the face-capture instant; it only
    lined up on cameras whose detect/record streams share an aspect ratio, and put
    the box in the wrong place on the dewarped side-door cam. Letting Frigate draw
    its own box avoids all of that.) Detect-res, but a correct box beats a crisp-
    but-wrong one; the clip gives record-res detail."""
    if not _real_event(eid):
        return  # not a real event -> no full frame / clip (crop-only)
    ts = "1" if SHOW_TIMESTAMP else "0"                # Frigate's UTC overlay on/off
    rec["full_url"] = client.event_snapshot_url(eid) + f"?timestamp={ts}&bbox=1"
    rec["clip_url"] = client.event_clip_url(eid)


_CAM_CACHE: dict = {}    # camera names cached for the life of the process


def _cameras(client):
    """Camera names from Frigate's config, cached for the life of the process."""
    if "names" not in _CAM_CACHE:
        try:
            _CAM_CACHE["names"] = list((client.get_config().get("cameras") or {}).keys())
        except Exception:
            _CAM_CACHE["names"] = []
    return _CAM_CACHE["names"]


def _camera_for_ts(client, face_ts, evcache):
    """Recover the camera for a face whose tracked event has aged out of the events
    DB, by probing each camera's recording snapshot at the capture instant and
    returning the first that still has a frame on disk. Recordings outlive event
    metadata, so this reclaims the full scene instead of dropping to the tiny crop.
    Cached per timestamp within a build (evcache is reused across candidates)."""
    key = ("_camts", face_ts)
    if key not in evcache:
        evcache[key] = next(
            (cam for cam in _cameras(client)
             if client.recording_snapshot_exists(cam, face_ts)), None)
    return evcache[key]


def _attach_face_frame(client, eid, face_ts, evcache, rec):
    """Faces: the full scene at the EXACT face-capture instant (boxless), so it
    matches the crop the reviewer is judging — no temporal mismatch, no confusion.
    Frigate's boxed best-frame is offered as a SECONDARY view (full_url_alt) and the
    clip for motion. We don't draw our own box: Frigate's coords are in a dewarped
    detect space that doesn't map onto the record-res frame on fisheye cams (ADR-0009).
    Falls back to the boxed best frame if there's no recording/camera."""
    if not _real_event(eid):
        return
    if eid not in evcache:
        try:
            evcache[eid] = client.get_event(eid)
        except Exception:
            evcache[eid] = None
    ev = evcache[eid]
    cam = ev.get("camera") if ev else None
    # Event aged out of the DB (no camera) but its recording may still be on disk —
    # probe cameras at the capture instant to recover the full scene (ADR-0009).
    if not cam and face_ts:
        cam = _camera_for_ts(client, face_ts, evcache)
    ts = "1" if SHOW_TIMESTAMP else "0"
    rec["clip_url"] = client.event_clip_url(eid)
    # boxed best frame — only meaningful if the event still exists; when it's gone
    # the recording frame (below) is the reliable scene and this alt just 404s.
    rec["full_url_alt"] = client.event_snapshot_url(eid) + f"?timestamp={ts}&bbox=1"
    rec["full_url"] = (client.recording_snapshot_url(cam, face_ts) if (cam and face_ts)
                       else rec["full_url_alt"])      # exact face moment (matches the crop)


def from_model_manual(client, model, cfg):
    """One review card per EVENT (ADR-0015 / Tenet 4). Frigate saves many near-
    identical crops per tracked object; we group the train pool by Frigate's event id
    and present ONE card — the single best (highest-confidence) crop — as "Is this
    <guess>?" (guess = that best crop's label). Deciding the event applies to the
    WHOLE event at commit: a small diverse subset (WINNOW_KEEP_PER_EVENT) is
    categorized to the chosen class and the rest are deleted from the train pool — so
    a parked car is one card, not 19, and training stays diverse (no near-dup overfit,
    Frigate's "cap similar" guidance). Each candidate carries `event_files` (all the
    event's crops) for commit to act on. A binary model still works (its event guess
    is just its one class). Swipe UI is identical to faces: Yes / Reassign / No / Skip."""
    out, evcache = [], {}
    files = [f for f in client.list_train(model) if _real_event(f)]
    ids = list(cfg["identities"])
    events: dict = {}
    for f in files:
        events.setdefault(train_event_id(f), []).append(f)
    for eid, group in events.items():
        rep = max(group, key=_score_of)            # best crop = the representative shown
        parts = rep.rsplit(".", 1)[0].split("-")
        guess = parts[-2] if (len(ids) > 1 and len(parts) >= 2) else ids[0]
        evfiles = sorted(group)
        keep = _keep_subset(evfiles)               # the diverse subset that will be TRAINED
        n = len(evfiles)
        rec = {
            "cid": f"{model}|{eid}",               # ONE candidate per EVENT
            "kind": cfg["kind"], "identity": guess, "role": "positive",
            "model": model, "training_file": rep,  # representative crop
            "event_files": evfiles,                # all crops in the event
            "keep_files": keep,                    # the ones commit keeps; rest are pruned
            # browser URLs for the keep-set, so the lightbox can show exactly what
            # will be trained (hand-validate they're the same entity) — ADR-0015.
            "keep_urls": [client.train_image_url(model, f) for f in keep],
            "img": os.path.join(CLIPS_DISK, model, "train", rep),
            "img_url": client.train_image_url(model, rep),
            "confidence": _score_of(rep),
            "reason": f"Frigate's guess — {n} frame{'s' if n != 1 else ''} in this event"
                      + (f", keeping {len(keep)}" if n > len(keep) else ""),
            "source": "manual", "meta": {"event": eid, "frames": n},
        }
        _attach_media(client, eid, evcache, rec)
        out.append(rec)
    return out


def from_model(client, model, cfg):
    rp = results_path(model)
    if not os.path.exists(rp):
        return []
    ids, neg, noun = cfg["identities"], cfg["negative"], cfg["noun"]
    # one frame per event — keep the BEST (highest-confidence) emittable frame,
    # so a clear shot wins over a near-duplicate bad crop. The clip in the
    # lightbox still gives access to every frame of the event.
    best = {}
    for line in open(rp):
        r = json.loads(line)
        if r.get("bucket") not in ids and r.get("bucket") != "other":
            continue  # review / unsure: not clean training data
        if not _real_event(r.get("training_file", "")):
            continue  # seed/sample file, not a real event
        eid = r.get("event_id")
        if eid not in best or (r.get("confidence") or 0) > (best[eid].get("confidence") or 0):
            best[eid] = r
    out, evcache = [], {}
    for r in best.values():
        b = r["bucket"]
        if b in ids:
            identity, role, q = b, "positive", None
        else:
            identity, role = neg, "negative"
            q = f"Is this a {noun} that is NOT one of ours (a neighbor / passing {noun})?"
        tf = r["training_file"]
        rec = {
            "cid": cid(model, identity, tf),
            "kind": cfg["kind"], "identity": identity, "role": role,
            "model": model, "training_file": tf,           # what commit needs
            "img": os.path.join(CLIPS_DISK, model, "train", tf),   # local app /img
            "img_url": client.train_image_url(model, tf),          # browser/remote
            "confidence": r.get("confidence"), "reason": r.get("reason", ""),
            "source": "classifier",
            "meta": {"time": r.get("start_time"), "obs": r.get("obs")},
        }
        _attach_media(client, r.get("event_id"), evcache, rec)
        if q:
            rec["question"] = q
        out.append(rec)
    return out


def from_faces(client):
    """Faces from the event-linked TRAIN pool (not the flat library). Frigate's
    recognizer has already guessed the name (in the filename), which is the
    pre-sort — the swipe just confirms "Is this <name>?". Commit assigns via
    /faces/train/<name>/classify. Full scene is the face-capture frame (boxless),
    see _attach_face_frame for why we don't reuse the event-snapshot box."""
    out, evcache, best = [], {}, {}
    try:
        faces = client.list_faces()
    except Exception as e:
        print(f"  (skipping faces: {e})")
        return out
    # train filename: <ts>-<rand>-<ts2>-<Name>-<score>.webp. Keep the highest-
    # score (clearest) face per (event, person) — score is Frigate's recognition
    # confidence.
    for f in faces.get("train") or []:
        if not _real_event(f):
            continue  # seed/sample file, not a real recognition event
        parts = f.rsplit(".", 1)[0].split("-")
        if len(parts) < 4:
            continue
        name = parts[-2]
        try:
            conf = float(parts[-1])
        except ValueError:
            conf = None
        key = (train_event_id(f), name) if DEDUP else f   # f is unique per frame
        if key not in best or (conf or 0) > (best[key][0] or 0):
            best[key] = (conf, f, name)
    for conf, f, name in best.values():
        eid = train_event_id(f)
        fp = f.rsplit(".", 1)[0].split("-")
        face_ts = fp[2] if len(fp) >= 3 else None    # capture instant (ts2)
        # Frigate's unrecognized faces (name "unknown"/"none") are NOT dropped — they
        # are the purest "who is this?" candidates (ADR-0014). Pool them as "Needs ID";
        # the reviewer identifies (reassign), rejects, or skips them.
        unidentified = name.lower() in ("unknown", "none")
        rec = {
            "cid": f"face|{name}|{f}",
            "kind": "person", "identity": NEEDS_ID if unidentified else name,
            "role": "positive", "unidentified": unidentified,
            "face_train": f,             # commit -> /faces/train/<name>/classify
            "img": os.path.join(CLIPS_DISK, "faces", "train", f),
            "img_url": client.media_url(f"faces/train/{f}"),
            "confidence": conf,
            "reason": "unrecognized — who is this?" if unidentified else "Frigate face guess",
            "source": "face_train", "meta": {},
        }
        _attach_face_frame(client, eid, face_ts, evcache, rec)   # exact-moment scene + best-frame alt
        out.append(rec)
    return out


# ---- LIBRARY (already-committed crops) sources, ADR-0016 -------------------
# These surface what Frigate's auto-commit (face recognition above threshold, or
# direct UI categorize) already moved into its committed pools, so the reviewer
# can correct wrong matches that NEVER passed through the train pool (the
# Luigi->Mario class of bug). Same swipe UI; different data source: `bucket =
# "library"` separates them on the home page. Yes = a no-op confirmation tracked
# in verdicts so the item doesn't reappear; Reassign / Reject go through the
# library APIs at commit time (see commit.plan_actions).
LIBRARY = os.environ.get("WINNOW_LIBRARY_REVIEW", "1").lower() in ("1", "true", "yes")
LIBRARY_BUCKET = "library"


_LIB_TS_RE = re.compile(r"(\d{9,}\.\d+)")   # unix-timestamp segment in a library filename
_LIB_EVENT_RE = re.compile(r"(\d{9,}\.\d+)-([a-z0-9]+)")  # event id = ts-rand


def _scene_urls_for_face_lib(client, filename: str) -> list[str]:
    """Best-effort full-scene URLs for a face-library file. Face library names are
    `<Name>-<unix_ts>.<ext>` (no event id, no camera) — so we try EACH known
    camera's recording-frame at that timestamp; the first one that returns 200
    wins via the browser's onerror fallback chain in the lightbox. We don't
    probe at build time (would multiply HTTP load by 3× per face)."""
    m = _LIB_TS_RE.search(filename)
    if not m:
        return []
    ts = m.group(1)
    return [client.recording_snapshot_url(cam, ts) for cam in _cameras(client) if cam]


def from_face_library(client) -> list[dict]:
    """One candidate per file in each person's COMMITTED face folder
    (`/clips/faces/<Name>/<file>`), asking "Is this <Name>?". The library is
    returned by /api/faces; we skip the special 'train' pool (handled by
    from_faces). Reassign uses client.face_reclassify (adaptive: one-shot on
    v0.18, register+delete on v0.17 — same end result either way)."""
    out: list[dict] = []
    try:
        faces = client.list_faces() or {}
    except Exception as e:
        print(f"  (skipping face library: {e})")
        return out
    for name, files in faces.items():
        if name in FACE_SKIP or not isinstance(files, list):
            continue
        for f in files:
            rec = {
                "cid": f"face_lib|{name}|{f}",
                "kind": "person", "identity": name,
                "bucket": LIBRARY_BUCKET, "role": "positive",
                "face_lib_name": name, "library_id": f,        # commit needs both
                "img": os.path.join(CLIPS_DISK, "faces", name, f),
                "img_url": client.media_url(f"faces/{name}/{f}"),
                "confidence": None,
                "reason": f"Already in {name}'s library — verify",
                "source": "library_face", "meta": {},
            }
            # Lightbox full-scene: try each camera's recording at the face_ts;
            # browser-side onerror fallback picks the first that 200s, else
            # falls back to the committed crop itself.
            scene = _scene_urls_for_face_lib(client, f)
            if scene:
                rec["scene_urls"] = scene
            out.append(rec)
    return out


def from_dataset_library(client, model: str, cfg: dict) -> list[dict]:
    """One candidate per file in each category of a classifier's committed
    dataset (`/clips/<model>/dataset/<category>/<file>`), asking
    "Is this <category>?". Reassign uses client.reclassify (in-model move),
    Reject uses client.delete_dataset_images. `none` is included so hard-
    negatives can also be reviewed/corrected. Dataset filenames embed the
    Frigate event id (`<Category>-<ts>-<rand>.png`), so we can attach the event
    snapshot + clip exactly like a train candidate — full scene + scrubbable
    clip from the lightbox."""
    out: list[dict] = []
    try:
        ds = client.get_dataset(model) or {}
        cats = ds.get("categories") or {}
    except Exception as e:
        print(f"  (skipping dataset library for {model}: {e})")
        return out
    ts = "1" if SHOW_TIMESTAMP else "0"
    for cat, files in cats.items():
        if not isinstance(files, list):
            continue
        for f in files:
            rec = {
                "cid": f"{model}_lib|{cat}|{f}",
                "kind": cfg["kind"], "identity": cat,
                "bucket": LIBRARY_BUCKET, "role": "positive",
                "model": model, "library_category": cat, "library_id": f,
                "img": os.path.join(CLIPS_DISK, model, "dataset", cat, f),
                "img_url": client.media_url(f"{model}/dataset/{cat}/{f}"),
                "confidence": None,
                "reason": f"Already in {model}/{cat} — verify",
                "source": "library_dataset", "meta": {},
            }
            m = _LIB_EVENT_RE.search(f)
            if m:                                  # event id in the filename
                eid = m.group(0)
                rec["full_url"] = client.event_snapshot_url(eid) + f"?timestamp={ts}&bbox=1"
                rec["clip_url"] = client.event_clip_url(eid)
            out.append(rec)
    return out


RESCAN_BUCKET = "rescan"
RESCAN_FILE = os.path.join(REVIEW, "rescan_candidates.jsonl")


def from_rescan(client) -> list[dict]:
    """One swipe card per pending rescan candidate (review/rescan_candidates.jsonl,
    written by eval/rescan_recordings.py — ADR-0017). Asks "Is this <recognized>?"
    with the event snapshot as the full scene. Yes -> face_register the snapshot
    bytes into Frigate's library at the recognized name; Reassign -> register
    into the chosen target instead; No / Reject -> just drop from the queue.

    We DELIBERATELY surface every entry — even ones where Frigate's `recognize`
    was confident — because aggregate over-attribution is real (a trimmed
    pose-biased library can over-identify the most-frontal-trained person);
    human-in-the-loop confirms each before it touches the library."""
    if not os.path.exists(RESCAN_FILE):
        return []
    ts = "1" if SHOW_TIMESTAMP else "0"
    # Skip events the user has already actioned on a prior pass (committed_cids
    # is checked by review_app, but we also skip locally if we can read it so
    # the candidates file shrinks over time as it gets consumed).
    committed_cids: set = set()
    cm = os.path.join(REVIEW, "committed.jsonl")
    if os.path.exists(cm):
        for ln in open(cm):
            try:
                committed_cids.add(json.loads(ln).get("cid"))
            except Exception:
                pass
    out: list[dict] = []
    for ln in open(RESCAN_FILE):
        try:
            r = json.loads(ln)
        except Exception:
            continue
        eid = r.get("event_id"); name = r.get("recognized")
        if not eid or not name:
            continue
        cid = f"rescan|{name}|{eid}"
        if cid in committed_cids:
            continue
        reason_bits = [f"Frigate recognized as {name} ({float(r.get('score') or 0):.2f})"]
        old = r.get("old_sub_label")
        if old and old != name:
            reason_bits.append(f"was '{old}' before the trim — disagrees")
        rec = {
            "cid": cid,
            "kind": "person", "identity": name,
            "bucket": RESCAN_BUCKET, "role": "positive",
            "rescan_event_id": eid, "rescan_name": name,   # commit needs both
            # `img` is the event snapshot served by Frigate at full event-best-frame res
            "img_url": client.event_snapshot_url(eid) + f"?timestamp={ts}&bbox=1",
            "full_url": client.event_snapshot_url(eid) + f"?timestamp={ts}&bbox=1",
            "clip_url": client.event_clip_url(eid),
            "confidence": float(r.get("score") or 0),
            "reason": " · ".join(reason_bits),
            "source": "rescan", "meta": {
                "event": eid, "camera": r.get("camera"),
                "old_sub_label": old,
            },
        }
        out.append(rec)
    return out


def build(client=None, manual=None) -> dict:
    """Regenerate review/candidates.jsonl. manual=True (or env WINNOW_MANUAL)
    skips the VLM pre-sort and presents the raw train pool to be assigned by
    hand — the fresh-user, no-priming experience. Importable by the daemon."""
    if manual is None:
        manual = os.environ.get("WINNOW_MANUAL", "").lower() in ("1", "true", "yes")
    c = client or FrigateClient()
    if client is None:
        c.login()
    models = discover_models(c)          # from Frigate's config — no hardcoded names
    cands = []
    for model, cfg in models.items():
        cands += (from_model_manual(c, model, cfg) if manual
                  else from_model(c, model, cfg))
    cands += from_faces(c)
    # Library-cleanup pools (ADR-0016): surface ALREADY-committed items so the
    # reviewer can fix wrong matches that bypassed the train-pool review path
    # (e.g. a Luigi face auto-committed to Mario above recognition_threshold).
    # Each library candidate gets bucket="library" so the home page shows them
    # in a distinct section. Filtered out client-side if WINNOW_LIBRARY_REVIEW=0.
    if LIBRARY:
        cands += from_face_library(c)
        for model, cfg in models.items():
            cands += from_dataset_library(c, model, cfg)
    # Rescan-recordings results (ADR-0017): each prior recognize hit becomes a
    # swipe card. Empty when eval/rescan_recordings.py hasn't been run yet.
    cands += from_rescan(c)

    os.makedirs(REVIEW, exist_ok=True)
    out = os.path.join(REVIEW, "candidates.jsonl")
    with open(out, "w") as f:
        for x in cands:
            f.write(json.dumps(x) + "\n")

    # Assignable targets for the "?" reassign picker, keyed by kind:
    #   <kind> -> {model: [its categories + 'none']}  for every classifier (dog, car…);
    #   multi-class models add 'none' so a crop can be reassigned to the hard-negative
    #   bucket ("not one of ours"). The client offers reassign only when a model is
    #   MULTI-class (>1 category), so a binary model correctly has nothing to pick.
    #   person -> known faces in the library (+ reviewer can add a new person).
    targets = {"person": []}
    for model, cfg in models.items():
        ids = list(cfg["identities"])
        targets.setdefault(cfg["kind"], {})[model] = (ids + ["none"]) if len(ids) > 1 else ids
    try:
        targets["person"] = sorted(k for k in c.list_faces().keys() if k != "train")
    except Exception:
        pass
    with open(os.path.join(REVIEW, "targets.json"), "w") as f:
        json.dump(targets, f)

    by = {}
    for x in cands:
        by[(x["kind"], x["identity"])] = by.get((x["kind"], x["identity"]), 0) + 1
    return {"count": len(cands), "by": by, "path": out}


def main() -> int:
    res = build()
    print(f"wrote {res['count']} candidates -> {res['path']}\n")
    for (kind, ident), n in sorted(res["by"].items()):
        print(f"  {kind:7} {ident:16} {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
