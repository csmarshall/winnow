#!/usr/bin/env python3
"""Winnow review app — an always-ready Tinder-style swipe UI for curating
training-data candidates. Zero dependencies (stdlib http.server + vanilla JS).

Source-agnostic core: it serves review/candidates.jsonl, records decisions to
review/verdicts.jsonl, and (if an adapter injects a refresh hook) can pull fresh
candidates on demand or on a timer — so a non-technical reviewer just opens the
page, sees the pools, swipes, and clicks "Check for new" when caught up.

An adapter wires it up and starts it:
    import review_app
    review_app.REFRESH_FN = my_refresh_callable      # does the source-specific work
    review_app.serve(interval=1800, first_load=True) # bind 0.0.0.0:8077

REFRESH_FN runs in a background thread; candidates reload when it finishes.
Per-reviewer "where am I" is a short-TTL client cookie (ADR-0007); the durable
decisions live server-side in verdicts.jsonl.
"""
from __future__ import annotations

import base64
import json
import os
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REVIEW = os.path.join(HERE, "review")
CANDS = os.path.join(REVIEW, "candidates.jsonl")
VERDICTS = os.path.join(REVIEW, "verdicts.jsonl")
FLAGGED = os.path.join(REVIEW, "flagged.jsonl")
COMMITTED = os.path.join(REVIEW, "committed.jsonl")
TARGETS_FILE = os.path.join(REVIEW, "targets.json")
REFRESH_STATE_FILE = os.path.join(REVIEW, ".refresh_state.json")
PORT = int(os.environ.get("REVIEW_PORT", "8077"))

KIND_ORDER = {"person": 0, "dog": 1, "car": 2}
MIME = {".webp": "image/webp", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png"}

_lock = threading.Lock()
CAND: dict[str, dict] = {}
VERDICT: dict[str, str] = {}
FLAGGED_CIDS: set = set()    # flagged -> excluded from future queues (auto-hide)
TARGETS: dict = {}           # assignable subtypes for the "?" reassign picker
COMMITTED_CIDS: set = set()  # already pushed to the source (idempotency)

# refresh + commit hooks + state (injected by the adapter). Commit is USER-
# triggered, not part of refresh (ADR-0013): verdicts accumulate locally until
# the reviewer hits Commit, which pushes them to the source and rebuilds.
REFRESH_FN = None            # callable() -> summary str
COMMIT_FN = None             # callable() -> summary str  (push verdicts + rebuild)
SOURCE_NAME = "the backend"  # adapter sets this for the UI (e.g. "Frigate")
REFRESH = {"status": "idle", "last": None, "summary": ""}
COMMIT = {"status": "idle", "last": None, "summary": ""}
_refresh_lock = threading.Lock()
_commit_lock = threading.Lock()


def load() -> None:
    with _lock:
        CAND.clear()
        VERDICT.clear()
        if os.path.exists(CANDS):
            for line in open(CANDS):
                c = json.loads(line)
                CAND[c["cid"]] = c
        if os.path.exists(VERDICTS):
            for line in open(VERDICTS):
                v = json.loads(line)
                if v.get("verdict") == "__undo__":
                    VERDICT.pop(v["cid"], None)
                else:
                    VERDICT[v["cid"]] = v["verdict"]
        FLAGGED_CIDS.clear()
        if os.path.exists(FLAGGED):
            for line in open(FLAGGED):
                try:
                    FLAGGED_CIDS.add(json.loads(line)["cid"])
                except Exception:
                    pass
        TARGETS.clear()
        if os.path.exists(TARGETS_FILE):
            try:
                TARGETS.update(json.load(open(TARGETS_FILE)))
            except Exception:
                pass
        COMMITTED_CIDS.clear()
        if os.path.exists(COMMITTED):
            for line in open(COMMITTED):
                try:
                    COMMITTED_CIDS.add(json.loads(line)["cid"])
                except Exception:
                    pass
    if os.path.exists(REFRESH_STATE_FILE):
        try:
            REFRESH.update(json.load(open(REFRESH_STATE_FILE)))
            REFRESH["status"] = "idle"
        except Exception:
            pass


def _is_assignment(v) -> bool:
    """A verdict that claims the image for a subtype: a plain 'yes' or a '?'
    reassignment ('assign:<target>'). 'no'/'none'/'skip'/None do not claim it."""
    return v == "yes" or (isinstance(v, str) and v.startswith("assign:"))


def _uncommitted() -> int:
    """Decisions made but not yet pushed to the source — the ones a commit would
    actually act on (yes / reassignment / chosen subtype; no & skip don't push)."""
    n = 0
    for cid, v in VERDICT.items():
        if cid in COMMITTED_CIDS or cid not in CAND:
            continue
        if (v in ("yes", "reject") or (isinstance(v, str) and v.startswith("assign:"))
                or v in (CAND[cid].get("choices") or [])):
            n += 1        # reject pushes a delete, so it counts as work to commit
    return n


def _assigned_groups() -> set:
    """Groups (one per image) already claimed in some pool — binary-sweep
    exclusivity: their candidates in OTHER subtype pools drop out. AI-mode
    candidates have no 'group', so they're never affected."""
    return {CAND[cid].get("group") for cid, v in VERDICT.items()
            if _is_assignment(v) and cid in CAND and CAND[cid].get("group")}


def identities() -> list[dict]:
    """Aggregate pool counts per (kind, identity, bucket). Two buckets exist:
    'review' (the daily train-pool work) and 'library' (already-committed
    cleanup, ADR-0016). The same identity can appear in both — they're
    distinct pools with distinct verdicts."""
    agg: dict[tuple, dict] = {}
    assigned = _assigned_groups()

    def _row(kind, ident, bucket):
        return agg.setdefault((kind, ident, bucket),
                              {"kind": kind, "identity": ident, "bucket": bucket,
                               "total": 0, "yes": 0, "no": 0, "skip": 0, "moved": 0})

    for c in CAND.values():
        if c["cid"] in FLAGGED_CIDS:
            continue  # flagged -> hidden from the pool entirely
        v = VERDICT.get(c["cid"])
        if v is None and c.get("group") in assigned:
            continue  # claimed by another pool — not part of this pool's work
        bucket = c.get("bucket") or "review"
        a = _row(c["kind"], c["identity"], bucket)
        a["total"] += 1
        if v is None:
            pass
        elif v == "skip":
            a["skip"] += 1
        elif v in ("no", "none", "reject"):
            a["no"] += 1        # reject = "not one of ours" — counts as a rejection
        elif isinstance(v, str) and v.startswith("assign:"):
            a["moved"] += 1        # reassigned OUT of this (source/guess) pool
            target = v[len("assign:"):]
            if target != c["identity"]:   # reshuffle: ALLOCATED + decided in the target pool
                t = _row(c["kind"], target, bucket)
                t["total"] += 1
                t["yes"] += 1
        else:                      # "yes" (or, in N-way mode, a chosen subtype)
            a["yes"] += 1
    rows = list(agg.values())
    for a in rows:
        a["pending"] = a["total"] - a["yes"] - a["no"] - a["skip"] - a["moved"]
    rows.sort(key=lambda a: (a["bucket"] != "review",     # review first, library after
                             KIND_ORDER.get(a["kind"], 9), a["identity"]))
    return rows


def _build_id() -> str:
    """Identifies the current candidate build. Changes whenever candidates.jsonl is
    (re)written — a rebuild, refresh, or clear-and-restart — but is stable across a
    benign process restart. The client pins its resume pointer to this so a stale
    pointer into a wiped/regenerated dataset is discarded instead of offered."""
    try:
        return str(int(os.path.getmtime(CANDS)))
    except OSError:
        return "0"


def status() -> dict:
    ids = identities()
    with_pending = [r for r in ids if r["pending"] > 0]
    last = REFRESH["last"]
    return {
        "identities": ids,
        "build_id": _build_id(),
        "targets": TARGETS,         # assignable subtypes for the "?" reassign picker
        "n_pools": len(with_pending),
        "n_types": len({r["kind"] for r in with_pending}),
        "total_pending": sum(r["pending"] for r in ids),
        "uncommitted": _uncommitted(),
        "source_name": SOURCE_NAME,
        "refresh": {
            "status": REFRESH["status"],
            "summary": REFRESH["summary"],
            "last_age": (time.time() - last) if last else None,
            "enabled": REFRESH_FN is not None,
        },
        "commit": {
            "status": COMMIT["status"],
            "summary": COMMIT["summary"],
            "enabled": COMMIT_FN is not None,
        },
    }


def queue(kind: str, identity: str, bucket: str = "review") -> list[dict]:
    """Build the swipe queue for one pool. `bucket` is "review" (train-pool /
    daily) or "library" (already-committed cleanup, ADR-0016). The same
    (kind, identity) can exist in both — they don't share candidates."""
    out = []
    assigned = _assigned_groups()
    for c in CAND.values():
        cand_bucket = c.get("bucket") or "review"
        if (c["kind"] == kind and c["identity"] == identity
                and cand_bucket == bucket and c["cid"] not in VERDICT):
            if c.get("group") in assigned or c["cid"] in FLAGGED_CIDS:
                continue  # confirmed elsewhere, or flagged (auto-hidden)
            out.append({"cid": c["cid"], "confidence": c["confidence"],
                        "kind": c["kind"], "identity": c["identity"],
                        "bucket": cand_bucket,     # "review" or "library" (ADR-0016)
                        "model": c.get("model"),   # client needs these for the "?" picker
                        "unidentified": c.get("unidentified", False),  # face with no guess
                        "reason": c.get("reason", ""), "meta": c.get("meta", {}),
                        "source": c.get("source", ""), "question": c.get("question"),
                        "img_url": c.get("img_url"), "full_url": c.get("full_url"),
                        "full_url_alt": c.get("full_url_alt"), "clip_url": c.get("clip_url"),
                        "box": c.get("box"), "choices": c.get("choices"),
                        "keep_urls": c.get("keep_urls"),   # event keep-set (lightbox filmstrip)
                        "scene_urls": c.get("scene_urls")})   # library: per-camera scene fallbacks
    out.sort(key=lambda c: (c["confidence"] is None, c["confidence"] or 0))
    return out


def _fetch_b64(url):
    """Base64 of an image URL (for embedding the presented crop in a flag)."""
    if not url:
        return None
    try:
        return base64.b64encode(
            urllib.request.urlopen(url, timeout=10).read()).decode()
    except Exception:
        return None


def record(cid: str, verdict: str) -> None:
    with _lock:
        VERDICT[cid] = verdict
        with open(VERDICTS, "a") as f:
            f.write(json.dumps({"cid": cid, "identity": CAND[cid]["identity"],
                                "kind": CAND[cid]["kind"], "verdict": verdict,
                                "ts": time.time()}) + "\n")


def _do_refresh():
    if REFRESH_FN is None:
        return
    if not _refresh_lock.acquire(blocking=False):
        return  # already running
    try:
        REFRESH["status"] = "running"
        summary = REFRESH_FN() or "done"
        load()
        REFRESH.update(status="idle", last=time.time(), summary=str(summary))
    except Exception as e:
        REFRESH.update(status="error", summary=f"error: {e}")
    finally:
        try:
            json.dump({"last": REFRESH["last"], "summary": REFRESH["summary"]},
                      open(REFRESH_STATE_FILE, "w"))
        except Exception:
            pass
        _refresh_lock.release()


def trigger_refresh() -> bool:
    """Start a refresh in the background if one isn't already running."""
    if REFRESH_FN is None or REFRESH["status"] == "running":
        return False
    threading.Thread(target=_do_refresh, daemon=True).start()
    return True


def _do_commit(retrain: bool = True):
    """Run a commit in the background. `retrain` (default True) controls whether
    the adapter triggers a model retrain after the push. UI sends retrain=False
    for bulk library-cleanup sessions (ADR-0016) so dozens of micro-corrections
    don't kick off a retrain per batch."""
    if COMMIT_FN is None:
        return
    if not _commit_lock.acquire(blocking=False):
        return
    try:
        COMMIT["status"] = "running"
        # COMMIT_FN's signature was historically zero-arg; pass retrain only if it
        # accepts it (back-compat with older adapters that don't support the toggle).
        import inspect
        kwargs = {"retrain": retrain} if "retrain" in inspect.signature(COMMIT_FN).parameters else {}
        summary = COMMIT_FN(**kwargs) or "done"   # push verdicts to source + rebuild
        load()                               # committed items drop out, leftovers stay
        now = time.time()
        COMMIT.update(status="idle", last=now, summary=str(summary))
        # COMMIT_FN rebuilds the candidate queue, so the data IS fresh as of now —
        # stamp the refresh clock too, else the home screen wrongly says "last
        # refreshed <long ago>. Check for new" right after a commit.
        REFRESH.update(last=now, summary="refreshed as part of commit")
    except Exception as e:
        COMMIT.update(status="error", summary=f"error: {e}")
    finally:
        _commit_lock.release()


def trigger_commit(retrain: bool = True) -> bool:
    """User-triggered: push accumulated verdicts to the source, then rebuild.
    `retrain` (default True per ADR-0013 / consistency with normal commit) can
    be False for bulk library-cleanup sessions where you don't want a retrain
    after every micro-batch (ADR-0016)."""
    if COMMIT_FN is None or COMMIT["status"] == "running":
        return False
    threading.Thread(target=_do_commit, args=(retrain,), daemon=True).start()
    return True


PAGE = r"""<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Winnow</title>
<style>
 :root{color-scheme:dark}
 body{font:16px/1.45 system-ui,sans-serif;margin:0;background:#0e0e10;color:#eee}
 header{padding:14px 18px;border-bottom:1px solid #2a2a2e;display:flex;
   align-items:center;gap:12px}
 header h1{font-size:17px;margin:0;font-weight:700}
 #back{cursor:pointer;color:#8bf;display:none;font-size:15px}
 #dbg{margin-left:auto;cursor:pointer;opacity:.3;font-size:18px}
 #dbg.on{opacity:1}
 .wrap{max-width:780px;margin:0 auto;padding:18px}
 .summary{font-size:18px;margin:6px 0 4px}
 .resume{display:inline-block;margin:8px 0;background:#2b5;color:#031;font-weight:700;
   padding:10px 16px;border-radius:10px;cursor:pointer}
 .group h2{font-size:13px;text-transform:uppercase;letter-spacing:.06em;
   color:#888;margin:22px 0 8px}
 .cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:12px}
 .id{background:#1a1a1e;border:1px solid #2c2c32;border-radius:10px;padding:13px;
   cursor:pointer;transition:.12s}
 .id:hover{border-color:#56f;transform:translateY(-2px)}
 .id.done{opacity:.45}
 .id .n{font-weight:700;font-size:16px}
 .id .c{color:#999;font-size:13px;margin-top:3px}
 .bar{height:8px;background:#333;border-radius:3px;margin-top:9px;overflow:hidden;display:flex}
 .bar span{display:block;height:100%}
 .legend{color:#999;font-size:12px;margin:2px 0 10px}
 .commit{margin:10px 0;padding:12px 14px;border-radius:10px;background:#16161c;
   border:1px solid #2a2a32;color:#cdd;font-size:15px}
 .commit.ready{background:#16301f;border-color:#2a7a4a;color:#dfe}
 .commit.running{background:#1c1c30}
 .commitbtn{background:#27ae60;margin-left:6px;padding:11px 18px;font-size:15px}
 .commitlink{background:none;color:#7bd;font-weight:600;padding:2px 4px;font-size:13px;
   text-decoration:underline;cursor:pointer}
 .commitnote{margin:8px 0;padding:9px 12px;border-radius:9px;background:#15212e;
   border:1px solid #2c4a63;color:#bcd;font-size:13px;line-height:1.45}
 /* library-cleanup section header (ADR-0016) — visually distinct from daily review */
 .libhead{margin:32px 0 4px;padding-top:18px;border-top:1px dashed #2a2a32}
 .libhead h2{margin:0;color:#dbb} .libsub{color:#888;font-size:13px;margin:4px 0 10px}
 .fresh{margin:26px 0 6px;padding:14px;border-top:1px solid #2a2a2e;color:#aaa;
   font-size:14px;display:flex;align-items:center;gap:14px;flex-wrap:wrap}
 #swipe{display:none;text-align:center}
 .card{position:relative;display:inline-block;max-width:560px;width:100%;
   background:#1a1a1e;border:1px solid #2c2c32;border-radius:14px;padding:14px;
   transition:transform .18s,opacity .18s}
 .card img{width:100%;max-height:60vh;object-fit:contain;background:#000;border-radius:8px}
 .meta{color:#9be;font-size:14px;margin-top:8px;min-height:1.3em}
 .sub{color:#888;font-size:12px}
 .ask{font-size:22px;font-weight:700;margin:10px 0}
 .btns{display:flex;gap:14px;justify-content:center;margin-top:14px;flex-wrap:wrap}
 /* secondary row under the cross: small, muted pills — a rare action shouldn't
    have a bigger touch target than the common verdicts, so keep these compact */
 .btns2{display:flex;gap:10px;justify-content:center;margin-top:14px;flex-wrap:wrap}
 .btns2 button{height:40px;padding:0 16px;font-size:14px;opacity:.72}
 .btns2 button:hover,.btns2 button:active{opacity:1}
 button{font:700 16px system-ui;border:0;border-radius:10px;padding:15px 28px;
   cursor:pointer;color:#fff}
 /* Inverted-T, exactly like the arrow cluster: Skip=↑ on top; No=← / Undo=↓ /
    Yes=→ across the bottom. All four are one identical touch size (108x58). */
 /* ONE 5-way cross for every mode so a key always lands in the same spot:
    Undo(top ↑) / No-or-Reject(← lft) · Reassign(center) · Yes(→ rgt) / Skip(↓ bot). */
 .dpad{display:grid;gap:10px;justify-content:center;margin:14px auto 0;
   grid-template-columns:repeat(3,108px);
   grid-template-areas:". top ." "lft mid rgt" ". bot .";}
 .dpad>.top{grid-area:top}.dpad>.no{grid-area:lft}
 .dpad>.reassign{grid-area:mid}.dpad>.yes{grid-area:rgt}.dpad>.skip{grid-area:bot}
 .dp{width:108px;height:58px;padding:0;font-size:16px}
 .dpad .reassign.dp{font-size:14px;white-space:normal;line-height:1.1}
 .no{background:#c0392b}.yes{background:#27ae60}.skip{background:#555}
 .undo{background:#e0a800;color:#241a00}      /* amber — distinct from gray Skip */
 .back{background:#3a3a40}
 .reassign{background:#f3a7c6;color:#4a1430}  /* light pink — "it's actually …" */
 .refresh{background:#2563eb}.flag{background:#8a6d3b}
 /* "?" reassign typeahead */
 #reassign{position:fixed;inset:0;background:#000c;z-index:110;display:none;
   align-items:center;justify-content:center;padding:14px}
 .rabox{background:#1c1c22;border:1px solid #34343e;border-radius:14px;padding:18px;
   width:min(440px,94vw);max-height:82vh;display:flex;flex-direction:column;gap:12px}
 .ratitle{font-size:17px;font-weight:700}
 #ratext{font:600 18px system-ui;padding:13px;border-radius:10px;border:1px solid #444;
   background:#0e0e12;color:#fff;width:100%;box-sizing:border-box}
 #ralist{flex:1;min-height:0;overflow-y:auto;display:flex;flex-direction:column;gap:7px}
 .raitem{padding:13px 14px;border-radius:9px;background:#26262e;cursor:pointer;
   font-size:16px}
 .raitem:active,.raitem:hover{background:#37374a}
 /* inset ring (not outline) so the scroll container doesn't clip it at the edges */
 .raitem.active{background:#42425a;box-shadow:inset 0 0 0 2px #f3a7c6}
 .raitem.picked{background:#2a9d4e;color:#eafff0;box-shadow:inset 0 0 0 2px #8f8}  /* confirm flash */
 .ranew{background:#3a2531;color:#f3a7c6;font-weight:700}
 .raconfirm{background:#1f3a28;color:#9be8ab;font-weight:700}  /* confirm the guess */
 .ramore{padding:8px 14px;color:#888;font-size:13px;text-align:center}
 .swipe-yes{transform:translateX(120%) rotate(8deg);opacity:0}
 .swipe-no{transform:translateX(-120%) rotate(-8deg);opacity:0}
 .hint{color:#666;font-size:12px;margin-top:12px}
 /* keyboard shortcuts are desktop-only; on touch the labeled buttons are the UI */
 @media (pointer:coarse){.hint,#lbhint{display:none}}
 .prog{color:#aaa;font-size:14px;margin-bottom:8px}
 .empty{padding:50px 0;color:#7c7;font-size:18px}
 .zoomhint{color:#666;font-size:12px;margin-top:6px}
 /* event keep-set filmstrip in the lightbox: the exact crops that will be trained */
 .keepstrip{margin-top:10px;max-width:96vw}
 .keeplbl{color:#bcd;font-size:13px;margin-bottom:6px;text-align:center}
 .keepthumbs{display:flex;gap:6px;overflow-x:auto;justify-content:center;padding-bottom:4px}
 .keepthumbs img{height:72px;width:auto;border-radius:6px;border:2px solid #2a7a4a;background:#000}
 /* lightbox: full frame / scrubbable clip, with an unmissable close */
 #lb{position:fixed;inset:0;background:#000e;z-index:100;display:none;
   flex-direction:column;align-items:center;justify-content:center}
 #lbmedia{flex:1;min-height:0;display:flex;flex-direction:column;align-items:center;
   justify-content:center;gap:14px;width:100%;padding:8px;box-sizing:border-box}
 #lbmedia video,#lbmedia img{max-width:96vw;max-height:78vh;width:auto;height:auto;
   object-fit:contain;border-radius:8px;background:#000}
 /* For tiny library face crops (~40×54 px, the only image when no recording is
    available), force a visible upscale so the reviewer can actually see what they're
    judging. Pixelated to keep edges crisp — better than browser bilinear blur. */
 #lbmedia img.tiny{min-height:60vh;image-rendering:pixelated;image-rendering:crisp-edges}
 #lbx{position:absolute;top:max(12px,env(safe-area-inset-top));
   right:max(12px,env(safe-area-inset-right));width:54px;height:54px;border-radius:50%;
   background:#fff;color:#000;font-size:30px;font-weight:800;line-height:54px;
   text-align:center;cursor:pointer;box-shadow:0 2px 12px #000b;z-index:101}
 #lbctl{display:flex;flex-direction:column;gap:10px;align-items:center;width:100%;
   padding:14px;box-sizing:border-box}
 #lbhint{color:#bbb;font-size:13px;text-align:center;
   padding-bottom:max(12px,env(safe-area-inset-bottom))}
 .lbwrap{display:flex;flex-direction:column;align-items:center;gap:14px;max-height:100%}
 .playclip{background:#2563eb;color:#fff;font-weight:700;font-size:16px;border:0;
   border-radius:10px;padding:14px 22px;cursor:pointer}
 .lbframe{position:relative;display:inline-block;line-height:0}
 .lbbox{position:absolute;border:3px solid #2ee66a;border-radius:3px;
   box-shadow:0 0 0 9999px rgba(0,0,0,.45);pointer-events:none}
</style>
<header>
 <span id=back onclick="home()">‹ back</span>
 <h1>🌾 Winnow</h1>
 <span id=dbg onclick="toggleDebug()" title="debug mode: flag broken snapshots">🐞</span>
</header>
<div class=wrap>
 <div id=home></div>
 <div id=swipe>
   <div class=prog id=prog></div>
   <div class=card id=card>
     <img id=img>
     <div class=ask id=ask></div>
     <div class=meta id=reason></div>
     <div class=sub id=sub></div>
   </div>
   <div id=btns></div>
   <div class=btns2 id=btns2></div>
   <div class=hint>←/a no/reject · →/d yes · ↓/s skip · ↑/w/z undo · r reassign</div>
 </div>
</div>
<div id=lb onclick="closeLB(event)">
  <div id=lbx onclick="closeLB(event)">✕</div>
  <div id=lbmedia onclick="event.stopPropagation()"></div>
  <div id=lbctl onclick="event.stopPropagation()"></div>
  <div id=lbhint>←/a no/reject · →/d yes · ↓/s skip · ↑/w back · Esc / ✕</div>
</div>
<div id=reassign onclick="if(event.target===this)closeReassign()">
  <div class=rabox>
    <div class=ratitle id=ratitle>Reassign to…</div>
    <input id=ratext type=text autocomplete=off autocapitalize=words
      placeholder="type a name…" oninput="renderRA(this.value)">
    <div id=ralist></div>
    <button class=back onclick="closeReassign()">Cancel (Esc)</button>
  </div>
</div><script>
let cur=null, q=[], idx=0, hist=[], BUILD=null, TARGETS={};
// Outcome colors: green=match, red=no. Skip and reassign are both "in-between"
// outcomes (not a clean yes/no), so they're two slightly-different yellows —
// shown merged in the progress bar as their average.
function avgHex(a,b){const p=h=>[1,3,5].map(i=>parseInt(h.slice(i,i+2),16));
  const x=p(a),y=p(b),m=i=>Math.round((x[i]+y[i])/2).toString(16).padStart(2,'0');
  return '#'+m(0)+m(1)+m(2);}
const C_YES='#4a7', C_NO='#c0392b', Y_SKIP='#d4a017', Y_MOVED='#e6b54a';
const Y_HUD=avgHex(Y_SKIP,Y_MOVED);   // the "average yellow" for the bar
const POS="winnow_pos";
window.DEBUG=localStorage.getItem('winnow_debug')!=='0';  // default ON; 🐞 toggles off
function toggleDebug(){
  window.DEBUG=!window.DEBUG;
  localStorage.setItem('winnow_debug',window.DEBUG?'1':'0');
  document.getElementById('dbg').className=window.DEBUG?'on':'';
  if(document.getElementById('swipe').style.display!=='none')show();  // refresh flag button
}

function setCookie(v){document.cookie=POS+"="+encodeURIComponent(v)+";max-age=3600;path=/";}
function getCookie(){const m=document.cookie.match(/winnow_pos=([^;]+)/);return m?decodeURIComponent(m[1]):null;}

function ago(s){if(s==null)return"never";s=Math.floor(s);
  if(s<10)return"just now";if(s<90)return s+" sec ago";
  if(s<5400)return Math.round(s/60)+" min ago";
  return Math.round(s/3600)+" hr ago";}

async function home(){
  document.getElementById('swipe').style.display='none';
  document.getElementById('home').style.display='block';
  document.getElementById('back').style.display='none';
  const st=await (await fetch('/api/status')).json();
  BUILD=st.build_id; TARGETS=st.targets||{}; window._src=st.source_name||'the backend';
  // Bucket split (ADR-0016): "review" = daily train-pool work; "library" =
  // already-committed cleanup. Pools are distinct per bucket so the same
  // identity (e.g. Charles) can have both a review row and a library row.
  const review=st.identities.filter(r=>(r.bucket||'review')==='review');
  const library=st.identities.filter(r=>r.bucket==='library');
  const rescan=st.identities.filter(r=>r.bucket==='rescan');
  const reviewPending=review.reduce((s,r)=>s+r.pending,0);
  const libraryTotal=library.reduce((s,r)=>s+r.total,0);
  const rescanTotal=rescan.reduce((s,r)=>s+r.total,0);
  const rescanPending=rescan.reduce((s,r)=>s+r.pending,0);
  const lbl={person:'People',dog:'Dogs',car:'Cars'};
  let h=`<div class=summary>Here are <b>${st.n_pools}</b> pool${st.n_pools==1?'':'s'} to review across <b>${st.n_types}</b> type${st.n_types==1?'':'s'} — ${reviewPending} left.</div>`;
  h+=`<div class=legend>🟩 match · 🟨 skipped / reassigned · 🟥 not a match · ▫ left to do</div>`;
  h+=commitHTML(st);   // user-triggered commit (ADR-0013) — pools stay local until you push
  // resume where you left off — only if the cookie was set against THIS build
  // (a rebuild/clear changes build_id, so a stale pointer is dropped, not offered)
  const pos=getCookie();
  if(pos){try{const p=JSON.parse(pos);
    if(p.build!==BUILD){document.cookie=POS+"=;max-age=0;path=/";}
    else{const r=st.identities.find(x=>x.kind==p.kind&&x.identity==p.identity&&(x.bucket||'review')==(p.bucket||'review'));
      if(r&&r.pending>0)h+=`<div class=resume onclick='open_(${JSON.stringify(p.kind)},${JSON.stringify(p.identity)},${JSON.stringify(p.bucket||'review')})'>▶ Resume ${p.identity} (${r.pending} left)</div>`;}}catch(e){}}
  // Section renderer: groups a bucket's rows by kind and writes the card grid.
  const renderSection = (rows, bucket) => {
    const groups={}; rows.forEach(r=>{(groups[r.kind]=groups[r.kind]||[]).push(r)});
    let out='';
    for(const k of ['person','dog','car']){
      if(!groups[k])continue;
      out+=`<div class=group><h2>${lbl[k]||k}</h2><div class=cards>`;
      for(const r of groups[k]){
        const done=r.pending===0, w=t=>100*t/Math.max(1,r.total);
        const ip=(r.skip||0)+(r.moved||0);   // in-between: skipped + reassigned
        out+=`<div class="id ${done?'done':''}" onclick='open_(${JSON.stringify(r.kind)},${JSON.stringify(r.identity)},${JSON.stringify(bucket)})'>
          <div class=n>${r.identity}</div>
          <div class=c>${r.pending?r.pending+' left':'✓ done'} · ${r.yes}✓ ${r.no}✗${r.skip?' '+r.skip+'⤼':''}${r.moved?' '+r.moved+'🏷':''}</div>
          <div class=bar><span style="width:${w(r.yes)}%;background:${C_YES}"></span><span style="width:${w(ip)}%;background:${Y_HUD}"></span><span style="width:${w(r.no)}%;background:${C_NO}"></span></div></div>`;
      }
      out+='</div></div>';
    }
    return out;
  };
  h+=renderSection(review, 'review');
  // Library-cleanup section (ADR-0016): only show when there's actually anything
  // to curate (otherwise it's noise on a clean install).
  if(library.length){
    h+=`<div class=libhead><h2>🧹 Library cleanup</h2>
        <div class=libsub>Already-committed items — fix mistakes Frigate auto-committed (e.g. wrong-person face matches) without leaving Winnow. ${libraryTotal} item${libraryTotal==1?'':'s'} across ${library.length} pool${library.length==1?'':'s'}.</div></div>`;
    h+=renderSection(library, 'library');
  }
  // Rescan section (ADR-0017): pending face-registers harvested from recent
  // recordings by eval/rescan_recordings.py. Each is "Is this <recognized>?".
  // Yes/Reassign registers the event snapshot into that person's library.
  if(rescan.length){
    h+=`<div class=libhead><h2>🔍 Rescan candidates</h2>
        <div class=libsub>Recent events Frigate's <i>current</i> library recognized as someone — confirm each before it lands in the library. ${rescanPending} of ${rescanTotal} left across ${rescan.length} pool${rescan.length==1?'':'s'}.</div></div>`;
    h+=renderSection(rescan, 'rescan');
  }
  h+=freshHTML(st.refresh);
  document.getElementById('home').innerHTML=h;
}

function freshHTML(rf){
  if(!rf.enabled)return'';
  if(rf.status==='running')return`<div class=fresh>⏳ Checking for new snapshots… this can take a minute.</div>`;
  let msg=`Data last refreshed <b>${ago(rf.last_age)}</b>`;
  if(rf.summary)msg+=` <span style=color:#777>(${rf.summary})</span>`;
  // wrap the whole sentence in ONE span so flex doesn't stack each inline piece
  return`<div class=fresh><span>${msg}</span> <button class=refresh onclick="doRefresh()">Check for new</button></div>`;
}

async function doRefresh(){
  await fetch('/api/refresh',{method:'POST'});
  // poll until done
  const tick=async()=>{const st=await (await fetch('/api/status')).json();
    if(st.refresh.status==='running'){document.getElementById('home').innerHTML=
      `<div class=summary>⏳ Looking for new snapshots…</div><div class=fresh>This can take a moment.</div>`;
      setTimeout(tick,2000);}else{home();}};
  tick();
}

// Commit is user-triggered (ADR-0013). Lead with progress — "X of Y sub-categories
// to review before committing" — rather than nagging per-section.
function commitHTML(st){
  const cm=st.commit||{}; if(!cm.enabled)return'';
  const src=st.source_name||'the backend';
  if(cm.status==='running')return`<div class="commit running">⏳ Committing to ${src}…</div>`;
  const total=st.identities.filter(r=>r.total>0).length;   // sub-categories with work
  const left=st.identities.filter(r=>r.pending>0).length;  // not yet fully reviewed
  const n=st.uncommitted||0;
  if(left>0){
    let note='';
    if(window._postCommit){window._postCommit=false;   // show once, right after a commit
      note=`<div class=commitnote>↻ Heads up: ${src} often surfaces <b>more</b> matches right after a commit — `
        +`retraining re-checks recent detections, so a fresh batch appearing here is normal, not a mistake.</div>`;}
    return note+`<div class=commit><b>${left}</b> of <b>${total}</b> sub-categor${total==1?'y':'ies'} `
      +`to review before committing them to ${src}.`
      +(n?` <button class=commitlink onclick="doCommit(${n})">commit ${n} done now</button>`:'')
      +`</div>`;
  }
  if(n)return `<div class="commit ready">✓ All ${total} sub-categories reviewed — `
    +`<button class=commitbtn onclick="doCommit(${n})">⬆ Commit ${n} to ${src}</button></div>`;
  return `<div class=commit>✓ All reviewed and committed to ${src}.</div>`;
}
async function doCommit(n){
  const src=(window._src||'the backend');
  // Retrain toggle (ADR-0016): default ON (consistent with normal commit).
  // OFF is for bulk library-cleanup sessions where dozens of micro-corrections
  // shouldn't kick off a retrain per batch — you trigger one at the end.
  const retrain = confirm('Commit '+n+' decision'+(n==1?'':'s')+' to '+src+'?\n\n'
    + 'OK = commit AND retrain models (default — consistent with daily review)\n'
    + 'Cancel pops up a fix-only option for bulk library cleanup.');
  if(!retrain){
    if(!confirm('Commit '+n+' WITHOUT retraining? Useful for bulk library cleanup; retrain manually when done.'))return;
  }
  await fetch('/api/commit',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({retrain})});
  const tick=async()=>{const st=await (await fetch('/api/status')).json();
    if(st.commit.status==='running'){document.getElementById('home').innerHTML=
      `<div class=summary>⏳ Committing to ${st.source_name||'the backend'}…</div><div class=fresh>Pushing your decisions + retraining.</div>`;
      setTimeout(tick,2000);}else{window._postCommit=true;home();}};  // reassure if more appear
  tick();
}

async function open_(kind,identity,bucket){
  bucket = bucket || 'review';
  cur={kind,identity,bucket}; setCookie(JSON.stringify({kind,identity,bucket,build:BUILD}));
  q=await (await fetch('/api/queue?kind='+encodeURIComponent(kind)
      +'&identity='+encodeURIComponent(identity)
      +'&bucket='+encodeURIComponent(bucket))).json();
  idx=0; hist=[];
  document.getElementById('home').style.display='none';
  document.getElementById('swipe').style.display='block';
  document.getElementById('back').style.display='inline';
  show();
}

// build the action buttons: subtype choices (manual mode) or Yes/Skip/No.
function voteButtons(c,fn){
  if(c.choices){
    return c.choices.map((ch,i)=>`<button class="${ch=='none'?'skip':'yes'}" onclick="${fn}('${ch}')">${i+1} ${ch}</button>`).join('')
      +`<button class=skip onclick="${fn}('skip')">Skip</button>`;
  }
  return `<button class=no onclick="${fn}('no')">✗ No</button>`
    +`<button class=skip onclick="${fn}('skip')">Skip</button>`
    +`<button class=yes onclick="${fn}('yes')">✓ Yes</button>`;
}
// ONE 5-way cross for EVERY mode (so a key always lands in the same spot):
//   Undo/Back (top ↑) · No-or-Reject (← lft) · Reassign/Identify (center) ·
//   Yes (→ rgt) · Skip (↓ bot).  topHTML = Undo (card) / Back (lightbox).
// Faces say "Reject"/"Identify"; an unidentified face has no Yes; cars have no
// center (nothing to reassign to). Keys map identically across modes.
function crossButtons(c,fn,topHTML){
  const person=c.kind==='person';
  const negLbl=person?'✗ Reject':'✗ No', neg=person?'reject':'no';
  const ra=canReassign(c)
    ? `<button class="reassign dp" onclick="openReassign()">🏷 ${(person&&c.unidentified)?'Identify':'Reassign'}</button>` : '';
  const yes=(person&&c.unidentified)?'' : `<button class="yes dp" onclick="${fn}('yes')">✓ Yes</button>`;
  return `<div class=dpad>
    ${topHTML}
    <button class="no dp" onclick="${fn}('${neg}')">${negLbl}</button>
    ${ra}
    ${yes}
    <button class="skip dp" onclick="${fn}('skip')">Skip</button>
  </div>`;
}

function show(){
  const card=document.getElementById('card');
  card.className='card';
  const btns=document.getElementById('btns'), b2=document.getElementById('btns2');
  if(idx>=q.length){
    document.getElementById('prog').textContent='';
    card.innerHTML='<div class=empty>✓ All done for '+cur.identity+'!<br><br>'
      +'<button class=refresh onclick="home()">‹ Back to pools</button></div>';
    btns.innerHTML=''; b2.innerHTML='';
    return;
  }
  const c=q[idx];
  if(c.choices){                            // manual N-way (rare): simple row + undo
    btns.innerHTML=voteButtons(c,'vote');
    b2.innerHTML='<button class=undo onclick="undo()">⟲ Undo (z)</button>';
  }else{                                    // unified 5-way cross (dog/car/person)
    btns.innerHTML=crossButtons(c,'vote','<button class="undo dp top" onclick="undo()">⟲ Undo</button>');
    b2.innerHTML='';
  }
  document.getElementById('prog').textContent=`${idx+1} / ${q.length}`
    +(hist.length?`   ·   ↶ ${hist.length} to undo`:'');
  const src=c.img_url||('/img?cid='+encodeURIComponent(c.cid));
  const ask=c.question||(c.unidentified?'Who is this?'
    :(c.choices?'Which is this?':('Is this '+cur.identity+'?')));
  card.innerHTML=`<img id=img src="${src}" style="cursor:zoom-in" onclick="openLB(q[idx])">
    <div class=ask>${ask}</div>
    <div class=meta>${c.reason||''}</div>
    <div class=sub>${c.confidence!=null?'model conf '+(+c.confidence).toFixed(2):''}</div>
    <div class=zoomhint>👆 tap image for the full scene${window.DEBUG?' · press f to flag':''}</div>
    ${window.DEBUG?'<div class=zoomhint><button class=flag onclick="flag()">🚩 Flag for review</button></div>':''}`;
  if(idx+1<q.length){const n=q[idx+1];const p=new Image();p.src=n.img_url||('/img?cid='+encodeURIComponent(n.cid));}
}

async function vote(v){
  if(idx>=q.length)return;
  const c=q[idx]; hist.push({cid:c.cid,idx});
  const card=document.getElementById('card');
  if(v==='yes')card.classList.add('swipe-yes');
  if(v==='no')card.classList.add('swipe-no');
  await fetch('/api/verdict',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({cid:c.cid,verdict:v})});
  setTimeout(()=>{idx++;show();},(v==='skip')?0:140);
}

async function undo(){
  if(!hist.length)return;
  const h=hist.pop();
  const post=(id)=>fetch('/api/verdict',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({cid:id,verdict:'__undo__'})});
  await post(h.cid);
  if(h.sib)await post(h.sib);   // reshuffle was two verdicts -> revert both
  idx=h.idx; show();
}

// Lightbox: full-scene STILL first (light); clip loads only if you tap Play.
function openLB(c){
  // full scene: faces -> the EXACT face-capture frame (matches the crop, boxless);
  // dogs/cars -> Frigate's boxed snapshot. Falls back: exact -> best-frame -> crop.
  // Fallback chain for the lightbox image (first 200 wins, last resort = the crop).
  // scene_urls comes from library face candidates (per-camera recording snapshots at
  // the face timestamp — we don't probe at build time, the browser figures out which
  // camera has the frame via onerror progression). ADR-0016.
  window._lbsrcs=[...(c.scene_urls||[]),c.full_url,c.full_url_alt,c.img_url]
    .filter((v,i,a)=>v&&a.indexOf(v)===i);
  window._lbi=0; window._lbcid=c.cid; window._lbalt=c.full_url_alt;
  // #lbmedia holds the full-scene image — and, for an EVENT card, a filmstrip of the
  // exact crops that will be trained, so you can verify they're all the same entity
  // before confirming (ADR-0015). The media buttons live in #lbctl with the cross.
  let strip='';
  if(c.keep_urls && c.keep_urls.length){
    const k=c.keep_urls.length, noun=(c.kind==='person'?'person':c.kind);
    strip='<div class=keepstrip><div class=keeplbl>✓ '+k+' frame'+(k>1?'s':'')
      +' kept for training as <b>'+esc(c.identity)+'</b> — verify it’s the same '+noun+':</div>'
      +'<div class=keepthumbs>'+c.keep_urls.map(u=>'<img src="'+u+'">').join('')+'</div></div>';
  }
  document.getElementById('lbmedia').innerHTML=
    // onload tags any image whose natural size is small (face-library crops are ~40×54)
    // with .tiny so the CSS upscale kicks in. Bigger crops render at natural size.
    '<div class=lbframe><img src="'+(window._lbsrcs[0]||'')+'" onerror="lbImgErr(this)"'
      +' onload="if(this.naturalWidth<200||this.naturalHeight<200)this.classList.add(\'tiny\')"></div>'+strip;
  let media='';
  if(c.clip_url)media+='<button class=playclip onclick="playClip(event,\''+c.clip_url+'\')">▶ Play clip</button>';
  if(window._lbalt && window._lbalt!==c.full_url)   // faces: jump to the boxed best frame
    media+='<button class=back onclick="lbShowAlt(event)">🎯 Best frame</button>';
  let ctl=(media?'<div class=btns2>'+media+'</div>':'');
  if(c.choices){                            // manual N-way: row + Back
    ctl+=voteButtons(c,'lbVote')+'<button class=back onclick="closeLB(event)">‹ Back</button>';
  }else{                                    // unified 5-way cross; top = Back
    ctl+=crossButtons(c,'lbVote','<button class="back dp top" onclick="closeLB(event)">‹ Back</button>');
  }
  if(window.DEBUG)ctl+='<div class=btns2><button class=flag onclick="flag()">🚩 Flag</button></div>';
  document.getElementById('lbctl').innerHTML=ctl;
  document.getElementById('lbhint').innerHTML='←/a no/reject · →/d yes · ↓/s skip · ↑/w back · Esc'
    +(window.DEBUG?(' · f flag<br><span style=opacity:.55>'+(c.cid||'')+'</span>'):'');
  document.getElementById('lb').style.display='flex';
}
function lbImgErr(img){            // walk the fallback chain, then show a notice
  window._lbi++;
  if(window._lbi<window._lbsrcs.length){img.src=window._lbsrcs[window._lbi];}
  else if(img.parentNode){img.parentNode.innerHTML=
    '<div class=empty>⚠ image unavailable<br><small>recording/snapshot may have aged out — tap 🚩 Flag</small></div>';}
}
async function flag(cid){
  if(!window.DEBUG)return;
  cid=cid||window._lbcid||(q[idx]&&q[idx].cid);   // lightbox or current card
  if(!cid)return;
  await fetch('/api/flag',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({cid})});
  if(document.getElementById('lb').style.display==='flex')closeLB();
  idx++; show();                     // flagged -> auto-hidden henceforth; move on
}
function playClip(e,url){e.stopPropagation();
  document.getElementById('lbmedia').innerHTML='<video src="'+url+'" controls autoplay muted loop playsinline '
    +'onerror="this.parentNode.innerHTML=\'<div class=empty>⚠ clip unavailable (may have aged out of recording retention)</div>\'"></video>';
}
function lbShowAlt(e){if(e)e.stopPropagation();   // jump to Frigate's boxed best frame
  const img=document.querySelector('#lbmedia .lbframe img');
  if(img&&window._lbalt)img.src=window._lbalt;
}
function closeLB(e){if(e)e.stopPropagation();
  document.getElementById('lbmedia').innerHTML='';   // unloads any playing video
  document.getElementById('lb').style.display='none';
}
function lbVote(v){closeLB();vote(v);}   // decide right from the popup, then return

// ---- "?" reassign: typeahead over known subtypes of the same type ----------
// Reassignable when it's a face, OR a MULTI-class classifier (>1 category to pick
// among). A binary model (one category, e.g. a single-car Batmobile) has nothing to
// reassign to, so no reassign is offered. Kind-agnostic — dogs and cars alike.
function targetsFor(c){
  if(!c)return [];
  if(c.kind==='person')return TARGETS.person||[];
  return (TARGETS[c.kind]&&TARGETS[c.kind][c.model])||[];
}
function canReassign(c){
  if(!c)return false;
  if(c.kind==='person')return true;
  return targetsFor(c).length>1;   // multi-class classifier only
}
function openReassign(){
  const c=q[idx]; if(!canReassign(c))return;
  window._racid=c.cid; window._ratargets=targetsFor(c); window._racur=c.identity;
  window._racand=c;   // kept for kind/model in assignTo (reshuffle decision)
  document.getElementById('ratitle').textContent=
    'Reassign — pick or type a new '+(c.kind==='person'?'person':c.kind);
  const t=document.getElementById('ratext'); t.value='';
  renderRA('');
  document.getElementById('reassign').style.display='flex';
  setTimeout(()=>t.focus(),60);
}
function closeReassign(){document.getElementById('reassign').style.display='none';}
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function renderRA(query){
  // reference known targets by INDEX (assignTo(window._ratargets[i])) — never
  // interpolate the name into the onclick (a JSON.stringify'd name carries double
  // quotes that break the double-quoted attribute, which silently killed clicks).
  // Cap the visible matches (typeahead: keep typing to narrow); list also scrolls.
  const ql=query.trim().toLowerCase(), list=window._ratargets||[], LIMIT=8;
  const all=list.filter(t=>t.toLowerCase().includes(ql));
  window._rahits=all.slice(0,LIMIT);   // navigable names
  window._raidx=ql?0:-1;               // no pre-highlight on a blank field
  // the current identity (Frigate's guess / this pool) stays in the list so
  // filtering still finds it, but it's relabeled as a CONFIRM — picking it
  // reasserts the original guess (recorded as a plain "yes", not a reassignment).
  let h=window._rahits.map((t,vi)=>{
    const cur=(t===window._racur);
    const cls="raitem"+(vi===window._raidx?" active":"")+(cur?" raconfirm":"");
    const label=cur?("✓ Yes — it's "+esc(t)+" after all"):esc(t);
    return `<div class="${cls}" onclick="assignTo(window._rahits[${vi}])">${label}</div>`;
  }).join('');
  if(all.length>LIMIT)h+=`<div class=ramore>+${all.length-LIMIT} more — keep typing to narrow</div>`;
  const exact=list.some(t=>t.toLowerCase()===ql);   // case-insensitive: no dup
  if(ql && !exact)
    h+=`<div class="raitem ranew" onclick="assignToTyped()">➕ Create “${esc(query.trim())}”</div>`;
  if(!h)h='<div class=raitem style="opacity:.5;cursor:default">type a name…</div>';
  document.getElementById('ralist').innerHTML=h;
}
function assignToTyped(){const v=document.getElementById('ratext').value.trim(); if(v)assignTo(v);}
// arrow up/down through the filtered names — highlight + preview-fill the field
function raActive(d){
  const hits=window._rahits||[]; if(!hits.length)return;
  window._raidx=Math.max(0,Math.min(hits.length-1,(window._raidx||0)+d));
  const items=document.getElementById('ralist').querySelectorAll('.raitem');
  hits.forEach((t,vi)=>{ if(items[vi])items[vi].classList.toggle('active',vi===window._raidx); });
  document.getElementById('ratext').value=hits[window._raidx];   // populate the name
  if(items[window._raidx])items[window._raidx].scrollIntoView({block:'nearest'});
}
function raEnter(){                       // Enter: the highlighted name, else create typed
  const hits=window._rahits||[], i=window._raidx;
  if(i>=0 && hits[i]){assignTo(hits[i]);return;}   // an explicit (typed/arrowed) highlight
  const v=document.getElementById('ratext').value.trim();
  if(!v)return;                                    // blank + nothing chosen -> ignore
  assignTo(hits[0]||v);                            // typed: first match, else create
}
async function assignTo(name){
  // casing: reuse an existing subtype's canonical spelling so "luigi" -> "Luigi" (no dup)
  const canon=(window._ratargets||[]).find(t=>t.toLowerCase()===name.trim().toLowerCase());
  const target=(canon||name.trim()); if(!target)return;
  const c=window._racand, cid=window._racid; if(!cid)return;
  // flash the chosen list item green for a beat so the pick registers before advancing
  const vis=window._rahits||[], items=document.querySelectorAll('#ralist .raitem');
  const hi=vis.indexOf(target);   // rendered items map to _rahits (the visible slice)
  const picked=(hi>=0?items[hi]:document.querySelector('#ralist .ranew'));
  if(picked)picked.classList.add('picked');
  const post=(id,v)=>fetch('/api/verdict',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({cid:id,verdict:v})});
  const existing=(window._ratargets||[]).some(t=>t===target);
  if(target===window._racur){            // confirm the guess -> real "yes" (green)
    hist.push({cid,idx}); post(cid,'yes');
  } else {                               // reassign -> single verdict. identities()
    // reshuffles it (ADR-0014): marked reassigned-out of its source pool AND shown
    // allocated/decided in the target pool; commit categorizes/classifies to target.
    hist.push({cid,idx}); post(cid,'assign:'+target);
  }
  // feedback lives ON the dialog: the chosen item flashes green (above), the dialog
  // stays a beat so it registers, then closes and advances — no separate floating toast.
  setTimeout(()=>{ closeReassign();
    if(document.getElementById('lb').style.display==='flex')closeLB();
    idx++; show(); }, 320);
}

document.addEventListener('keydown',e=>{
  if(document.getElementById('reassign').style.display==='flex'){   // modal owns keys
    if(e.key==='Escape')closeReassign();
    else if(e.key==='ArrowDown'){e.preventDefault();raActive(1);}
    else if(e.key==='ArrowUp'){e.preventDefault();raActive(-1);}
    else if(e.key==='Enter'){e.preventDefault();raEnter();}
    return;
  }
  const lbopen=document.getElementById('lb').style.display==='flex';
  if(!lbopen && document.getElementById('swipe').style.display==='none')return;
  const c=q[idx], act=lbopen?lbVote:vote;
  if(e.key==='Escape'||e.key==='Backspace'){if(lbopen)closeLB();return;}
  if(e.key==='f'){flag();return;}      // flag from the card or the lightbox
  if(e.key==='z'){undo();return;}
  if(e.key==='r'){openReassign();return;}   // "it's actually…" reassign
  const k=e.key.toLowerCase();      // arrow keys OR wasd, matching the cross
  if(e.key==='ArrowUp'||k==='w'){e.preventDefault();lbopen?closeLB():undo();return;}  // ↑ = top (Undo/Back)
  if(e.key==='ArrowDown'||k==='s'){e.preventDefault();act('skip');return;}            // ↓ = Skip
  if(c&&c.choices){                 // manual mode: number keys pick a subtype
    const n=parseInt(e.key);
    if(n>=1&&n<=c.choices.length){act(c.choices[n-1]);return;}
    return;
  }
  const face=c&&c.kind==='person';
  if(e.key==='ArrowRight'||k==='d'){ if(!(face&&c.unidentified))act('yes'); }  // no Yes for Needs ID
  else if(e.key==='ArrowLeft'||k==='a')act(face?'reject':'no');                // ← = Reject (faces) / No (dogs)
});
document.getElementById('dbg').className=window.DEBUG?'on':'';
home();
</script>
"""


class H(BaseHTTPRequestHandler):
    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # the page + API are dynamic and redeployed often — never let a browser
        # serve a stale cached copy (e.g. old JS missing newly-shipped buttons).
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj):
        self._send(200, "application/json", json.dumps(obj).encode())

    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            self._send(200, "text/html; charset=utf-8", PAGE.encode())
        elif u.path == "/api/status":
            self._json(status())
        elif u.path == "/api/identities":
            self._json(identities())
        elif u.path == "/api/queue":
            qs = parse_qs(u.query)
            self._json(queue(qs.get("kind", [""])[0], qs.get("identity", [""])[0],
                             qs.get("bucket", ["review"])[0]))   # ADR-0016
        elif u.path == "/img":
            cid = parse_qs(u.query).get("cid", [""])[0]
            c = CAND.get(cid)
            if not c or not c.get("img") or not os.path.exists(c["img"]):
                self._send(404, "text/plain", b"not found")
                return
            ext = os.path.splitext(c["img"])[1].lower()
            with open(c["img"], "rb") as f:
                self._send(200, MIME.get(ext, "application/octet-stream"), f.read())
        else:
            self._send(404, "text/plain", b"not found")

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/api/refresh":
            started = trigger_refresh()
            self._json({"started": started, "status": REFRESH["status"]})
            return
        if u.path == "/api/commit":
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            retrain = bool(body.get("retrain", True))   # default ON (Charles's pick)
            started = trigger_commit(retrain=retrain)
            self._json({"started": started, "status": COMMIT["status"]})
            return
        if u.path == "/api/flag":
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            c = CAND.get(body.get("cid"))
            if c:
                rec = {k: c.get(k) for k in ("cid", "kind", "identity", "model",
                       "training_file", "img_url", "full_url", "full_url_alt",
                       "clip_url", "box", "confidence")}
                rec["note"] = body.get("note", "")
                rec["ts"] = time.time()
                # Embed the actually-presented crop so the log is self-contained
                # and reviewable even after the source ages out (debug artifact).
                rec["image_b64"] = _fetch_b64(c.get("img_url"))
                with _lock:
                    FLAGGED_CIDS.add(c["cid"])
                    with open(FLAGGED, "a") as f:
                        f.write(json.dumps(rec) + "\n")
            self._json({"ok": True})
            return
        if u.path == "/api/verdict":
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            cid, verdict = body.get("cid"), body.get("verdict")
            if cid in CAND:
                if verdict == "__undo__":
                    with _lock:
                        VERDICT.pop(cid, None)
                        with open(VERDICTS, "a") as f:
                            f.write(json.dumps({"cid": cid, "verdict": "__undo__",
                                                "ts": time.time()}) + "\n")
                elif (verdict in ("yes", "no", "skip", "reject")
                      or (isinstance(verdict, str) and verdict.startswith("assign:"))
                      or verdict in (CAND[cid].get("choices") or [])):
                    record(cid, verdict)
            self._json({"ok": True})
            return
        self._send(404, "text/plain", b"not found")


def _autorefresh_loop(interval):
    while True:
        time.sleep(interval)
        trigger_refresh()


def serve(refresh_fn=None, interval=None, first_load=False):
    global REFRESH_FN
    if refresh_fn is not None:
        REFRESH_FN = refresh_fn
    load()
    print(f"loaded {len(CAND)} candidates, {len(VERDICT)} verdicts; "
          f"refresh {'enabled' if REFRESH_FN else 'disabled'}")
    if first_load and REFRESH_FN and not CAND:
        print("no candidates yet — running initial refresh")
        trigger_refresh()
    if interval and REFRESH_FN:
        threading.Thread(target=_autorefresh_loop, args=(interval,), daemon=True).start()
        print(f"auto-refresh every {interval}s")
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), H)
    print(f"Winnow on http://0.0.0.0:{PORT}")
    srv.serve_forever()


if __name__ == "__main__":
    serve()
