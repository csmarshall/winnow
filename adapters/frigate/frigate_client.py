#!/usr/bin/env python3
"""Thin client for Frigate's HTTP API (stdlib-only, auth-optional).

Winnow is a companion: it talks to a Frigate instance — local or on another box
— over the network. This wraps the endpoints the curation flow needs, handles
auth gracefully (works whether the instance has auth on or off), and can PROBE an
instance to discover its API path prefix + auth requirement rather than assuming.

Auth model (see ADR-0005): we target auth-ON as the strict superset. If creds are
configured we log in and carry the token (both as a cookie and as a Bearer
header, so it works regardless of which Frigate expects); if not, we call
directly (auth-off instance or unauthenticated port). 401 triggers one re-login.

Config (env, or a .env file beside this script / at the project root):
    FRIGATE_URL        base URL, e.g. http://127.0.0.1:5001   (default)
    FRIGATE_USER       admin username (omit for an auth-off instance)
    FRIGATE_PASSWORD   password        (omit for an auth-off instance)
    FRIGATE_API_PREFIX "" or "/api"  — usually auto-detected by probe

CLI:
    python3 frigate_client.py probe                 # map endpoints + auth
    python3 frigate_client.py ping                  # version + auth status
    python3 frigate_client.py train-list <model>    # GET train images
    python3 frigate_client.py faces                 # list faces
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar


def _seg(s: str) -> str:
    """URL-encode a single path segment. Model names can contain spaces
    (e.g. 'Mystery Machine') — left raw they break the request URL."""
    return urllib.parse.quote(str(s), safe="")


# ---- config -----------------------------------------------------------------
def load_dotenv() -> None:
    """Minimal .env loader (no dependency): KEY=VALUE lines into os.environ."""
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (os.path.join(here, ".env"),
                 os.path.join(here, "..", "..", ".env")):
        if os.path.isfile(path):
            for line in open(path):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"\''))


class FrigateError(RuntimeError):
    pass


class FrigateClient:
    def __init__(self, url=None, user=None, password=None, api_prefix=None):
        load_dotenv()
        self.base = (url or os.environ.get("FRIGATE_URL", "http://127.0.0.1:5001")).rstrip("/")
        self.user = user if user is not None else os.environ.get("FRIGATE_USER")
        self.password = password if password is not None else os.environ.get("FRIGATE_PASSWORD")
        # "" (root) or "/api"; if unset we leave None and let probe/ensure detect
        self.api = api_prefix if api_prefix is not None else os.environ.get("FRIGATE_API_PREFIX")
        self._jar = CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._jar))
        self._token = None

    # ---- low-level ----------------------------------------------------------
    def _raw(self, method, path, body=None, headers=None, timeout=30):
        url = self.base + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self._token:
            req.add_header("Authorization", f"Bearer {self._token}")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        return self._opener.open(req, timeout=timeout)

    def _status(self, path, method="GET"):
        """Return HTTP status for a path (no exceptions), for probing."""
        try:
            return self._raw(method, path).status
        except urllib.error.HTTPError as e:
            return e.code
        except Exception:
            return None

    def login(self) -> bool:
        """Log in if creds are configured. Captures JWT via cookie jar + Bearer.
        Returns True if authenticated, False if running credential-less."""
        if not (self.user and self.password):
            return False
        prefix = self._ensure_prefix()
        last = None
        for p in (f"{prefix}/login", "/api/login", "/login"):
            try:
                resp = self._raw("POST", p,
                                 {"user": self.user, "password": self.password})
                raw = resp.read().decode() or "{}"
                # token may be in body and/or set as a cookie (jar handles cookie)
                try:
                    self._token = json.loads(raw).get("access_token") or \
                        json.loads(raw).get("token")
                except Exception:
                    self._token = None
                if not self._token:
                    for c in self._jar:
                        if "token" in c.name.lower() or c.value.count(".") == 2:
                            self._token = c.value
                            break
                return True
            except urllib.error.HTTPError as e:
                last = e.code
                if e.code in (404, 405):
                    continue  # wrong login path, try next
                raise FrigateError(f"login failed at {p}: HTTP {e.code}")
            except Exception as e:
                last = e
        raise FrigateError(f"could not find a working /login endpoint (last={last})")

    def _ensure_prefix(self):
        """Auto-detect whether the API lives under /api (nginx port 5000/8971)
        or at root (the loopback app on 5001). Check /api first and require a
        NON-HTML 200 — port 5000 serves the SPA at root, so a bare 200 is not
        enough to prove the API is there."""
        if self.api is not None:
            return self.api
        for pref in ("/api", ""):
            try:
                r = self._raw("GET", f"{pref}/version")
                if r.status == 200 and "html" not in r.headers.get("Content-Type", "").lower():
                    self.api = pref
                    return pref
            except Exception:
                continue
        self.api = ""  # fall back to root
        return self.api

    def request(self, method, path, body=None):
        """Authenticated JSON request with one re-login retry on 401. `path` is
        prefix-less (e.g. '/classification/X/train'); the API prefix is resolved
        here so callers never touch it."""
        full = self._ensure_prefix() + path
        try:
            resp = self._raw(method, full, body)
            return json.loads(resp.read().decode() or "null")
        except urllib.error.HTTPError as e:
            if e.code == 401 and (self.user and self.password):
                self.login()
                resp = self._raw(method, full, body)
                return json.loads(resp.read().decode() or "null")
            raise FrigateError(f"{method} {full} -> HTTP {e.code}: "
                               f"{e.read().decode()[:200]}")

    # ---- high-level API (paths confirmed via probe before relied upon) ------
    def version(self):
        return self._raw("GET", f"{self._ensure_prefix()}/version").read().decode().strip()

    def get_config(self):
        """Frigate's full runtime config (incl. classification.custom models)."""
        return self.request("GET", "/config")

    def list_train(self, model):
        return self.request("GET", f"/classification/{_seg(model)}/train")

    def get_dataset(self, model):
        return self.request("GET", f"/classification/{_seg(model)}/dataset")

    def generate_object_examples(self, model_name, label):
        return self.request("POST", "/classification/generate_examples/object",
                            {"model_name": model_name, "label": label})

    def categorize(self, model, category, training_file):
        return self.request("POST", f"/classification/{_seg(model)}/dataset/categorize",
                            {"category": category, "training_file": training_file})

    def delete_train(self, model, training_files):
        """Delete crops from a model's train pool (e.g. the redundant per-event
        siblings after keeping a diverse few — ADR-0015). No-op on empty list."""
        if not training_files:
            return None
        return self.request("POST", f"/classification/{_seg(model)}/train/delete",
                            {"ids": list(training_files)})

    def reclassify(self, model, category, image_id, new_category):
        return self.request("POST",
                            f"/classification/{_seg(model)}/dataset/{_seg(category)}/reclassify",
                            {"id": image_id, "new_category": new_category})

    def delete_dataset_images(self, model, category, ids):
        return self.request("POST",
                            f"/classification/{_seg(model)}/dataset/{_seg(category)}/delete",
                            {"ids": ids})

    def train(self, model):
        return self.request("POST", f"/classification/{_seg(model)}/train")

    def delete_model(self, model):
        return self.request("DELETE", f"/classification/{_seg(model)}")

    # ---- media (served at /clips/... at root, not under the API prefix) -----
    def media_url(self, relpath):
        """Browser-loadable URL for a clips asset, e.g. media_url('Scooby/train/x.webp')."""
        return f"{self.base}/clips/{relpath}"

    def train_image_url(self, model, filename):
        return self.media_url(f"{_seg(model)}/train/{_seg(filename)}")

    def event_snapshot_url(self, event_id):
        """Full-frame snapshot for an event (the whole scene behind a crop)."""
        return f"{self.base}{self._ensure_prefix()}/events/{event_id}/snapshot.jpg"

    def event_clip_url(self, event_id):
        """Event video clip (scrubbable, when still within retention)."""
        return f"{self.base}{self._ensure_prefix()}/events/{event_id}/clip.mp4"

    def recording_snapshot_url(self, camera, frame_time):
        """Full RECORD-resolution still from recordings at an exact time."""
        return f"{self.base}{self._ensure_prefix()}/{camera}/recordings/{frame_time}/snapshot.jpg"

    def recording_snapshot_exists(self, camera, frame_time):
        """True if a recording frame exists at frame_time on camera. Lets the
        candidate builder recover the camera (and thus the full scene) for a face
        whose tracked event has aged out of the events DB but whose recording
        (longer retention) is still on disk."""
        if not (camera and frame_time):
            return False
        return self._status(
            f"{self._ensure_prefix()}/{camera}/recordings/{frame_time}/snapshot.jpg") == 200

    def get_event(self, event_id):
        """Event record (camera, start_time, label, …) — for resolving the camera."""
        return self.request("GET", f"/events/{event_id}")

    def fetch_media(self, relpath, timeout=20):
        """Raw bytes of a clips asset (for the VLM)."""
        req = urllib.request.Request(self.media_url(relpath))
        if self._token:
            req.add_header("Authorization", f"Bearer {self._token}")
        return self._opener.open(req, timeout=timeout).read()

    def fetch_train_image(self, model, filename):
        return self.fetch_media(f"{model}/train/{filename}")

    def list_faces(self):
        return self.request("GET", "/faces")

    def delete_faces(self, name, ids):
        return self.request("POST", f"/faces/{name}/delete", {"ids": ids})

    def classify_face_train(self, name, training_file):
        """Assign an unclassified train-pool face crop to a name (like categorize)."""
        return self.request("POST", f"/faces/train/{name}/classify",
                            {"training_file": training_file})

    # ---- face LIBRARY ops (already-committed crops, ADR-0016) ---------------
    # v0.17 has /faces/{name}/register (upload a file) and /faces/{name}/delete
    # but NOT /faces/{name}/reclassify (move from person A to person B in one
    # call). The move endpoint lands in dev (v0.18+). We probe once at runtime
    # and use the one-shot move when available; otherwise we do register-to-new
    # then delete-from-old (same end result, ~2x the API work, works today).
    def _has_face_reclassify(self) -> bool:
        if not hasattr(self, "_face_reclassify_cached"):
            # OPTIONS isn't supported, so probe with a known-name + bogus body.
            # 422 (body validation) = endpoint exists; 404 = it doesn't.
            # An empty `name` would 404 against a real route too, so use the
            # first face library name we can find. Falls back to False on error.
            try:
                names = [n for n in (self.list_faces() or {}) if n != "train"]
                name = names[0] if names else "Charles"   # any name; we only read status
                code = self._status(f"{self._ensure_prefix()}/faces/{_seg(name)}/reclassify",
                                    method="POST")
                self._face_reclassify_cached = code is not None and code != 404
            except Exception:
                self._face_reclassify_cached = False
        return self._face_reclassify_cached

    def _multipart_upload(self, path: str, file_bytes: bytes, filename: str,
                          content_type: str = "application/octet-stream"):
        """POST `file_bytes` as a multipart form field 'file' to `path`. Returns
        parsed JSON. urllib doesn't ship a multipart builder, so we hand-build
        it (no extra dep). Used by face_register + face_recognize."""
        boundary = "----winnowUpload" + str(os.getpid())
        body = (f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n").encode()
        body += file_bytes + f"\r\n--{boundary}--\r\n".encode()
        full = self._ensure_prefix() + path
        req = urllib.request.Request(self.base + full, data=body, method="POST")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        if self._token:
            req.add_header("Authorization", f"Bearer {self._token}")
        resp = self._opener.open(req, timeout=60)
        return json.loads(resp.read().decode() or "null")

    def face_register(self, name: str, file_bytes: bytes, filename: str = "face.webp"):
        """Upload `file_bytes` directly into the `<name>` face library — Frigate
        detects, aligns, embeds, and stores it. The v0.17 path used for Winnow's
        library reassign (paired with delete_faces on the old name) and the
        rescan-recordings tool (high-res frames -> high-quality library entries)."""
        ct = ("image/webp" if filename.lower().endswith(".webp")
              else "image/jpeg" if filename.lower().endswith((".jpg", ".jpeg"))
              else "application/octet-stream")
        return self._multipart_upload(f"/faces/{_seg(name)}/register", file_bytes, filename, ct)

    def face_recognize(self, file_bytes: bytes, filename: str = "frame.jpg"):
        """Run Frigate's face detection + recognition against the CURRENT
        library on an uploaded image. Returns
            {"success": True, "score": <float>, "face_name": <name>}
        on a confident match, or {"success": False, "message": "..."} when
        no face was detected / no match. Critical for the rescan tool: we use
        the freshly-trimmed library as the authority, NOT old sub_labels
        (which were assigned when the library was bloated and could be wrong)."""
        ct = ("image/jpeg" if filename.lower().endswith((".jpg", ".jpeg"))
              else "image/webp" if filename.lower().endswith(".webp")
              else "application/octet-stream")
        return self._multipart_upload("/faces/recognize", file_bytes, filename, ct)

    def list_events(self, after: float | None = None, before: float | None = None,
                    labels: str | None = None, sub_labels: str | None = None,
                    has_snapshot: bool | None = None, limit: int = 1000):
        """List Frigate events (people/objects detected by tracking). Used by the
        rescan tool to walk recent person events for face harvesting."""
        from urllib.parse import urlencode
        q: dict = {"limit": int(limit)}
        if after is not None: q["after"] = after
        if before is not None: q["before"] = before
        if labels: q["labels"] = labels
        if sub_labels: q["sub_labels"] = sub_labels
        if has_snapshot is not None: q["has_snapshot"] = 1 if has_snapshot else 0
        return self.request("GET", "/events?" + urlencode(q))

    def fetch_event_snapshot(self, event_id: str, timeout: int = 20) -> bytes:
        """Download an event's snapshot as raw bytes — Frigate's already-picked
        best frame for that tracked-object event (the face/object IS present)."""
        prefix = self._ensure_prefix()
        req = urllib.request.Request(
            self.base + f"{prefix}/events/{_seg(event_id)}/snapshot.jpg")
        if self._token:
            req.add_header("Authorization", f"Bearer {self._token}")
        return self._opener.open(req, timeout=timeout).read()

    def face_reclassify(self, old_name: str, image_id: str, new_name: str):
        """Move a face from <old_name>'s library to <new_name>'s. Adaptive:
        uses Frigate's one-shot /faces/{name}/reclassify when available
        (v0.18+), otherwise falls back to register-then-delete (v0.17). Same
        outward semantics either way; safe order (register-first) so a
        partial failure leaves a duplicate, never a lost crop."""
        if self._has_face_reclassify():
            return self.request(
                "POST", f"/faces/{_seg(old_name)}/reclassify",
                {"id": image_id, "new_name": new_name})
        # v0.17 fallback: upload to new -> delete from old
        bytes_ = self.fetch_media(f"faces/{_seg(old_name)}/{_seg(image_id)}")
        self.face_register(new_name, bytes_, filename=image_id)
        self.delete_faces(old_name, [image_id])
        return {"ok": True, "via": "register+delete"}


# ---- CLI (probe / smoke tests) ---------------------------------------------
def _probe(c: FrigateClient):
    print(f"base = {c.base}")
    print(f"creds configured: user={'yes' if c.user else 'no'} "
          f"password={'yes' if c.password else 'no'}")
    print("\n-- unauthenticated path discovery --")
    candidates = ["/version", "/api/version", "/stats", "/api/stats",
                  "/config", "/api/config", "/faces", "/api/faces",
                  "/login", "/api/login"]
    codes = {}
    for p in candidates:
        codes[p] = c._status(p)
        print(f"  {str(codes[p]):>4}  GET {p}")
    # infer prefix
    if codes.get("/version") == 200:
        pref = ""
    elif codes.get("/api/version") == 200:
        pref = "/api"
    else:
        pref = "?"
    print(f"\ninferred API prefix: {pref!r}")
    auth_on = (codes.get(f"{pref}/stats") == 401) if pref != "?" else None
    print(f"auth required (stats=401): {auth_on}")
    if c.user and c.password:
        print("\n-- attempting login --")
        try:
            c.login()
            print(f"  login OK; token captured: {'yes' if c._token else 'no (cookie-only)'}")
            c.api = pref if pref != "?" else c.api
            print(f"  GET {c.api}/classification ... faces:",
                  c._status(f"{c.api}/faces"))
        except FrigateError as e:
            print(f"  login error: {e}")
    else:
        print("\n(no creds set — set FRIGATE_USER/PASSWORD in .env to test the auth path)")


def main(argv):
    c = FrigateClient()
    cmd = argv[0] if argv else "probe"
    try:
        if cmd == "probe":
            _probe(c)
        elif cmd == "ping":
            c.login()
            print("version:", c.version(), "| authed:", bool(c._token or not (c.user and c.password)))
        elif cmd == "train-list":
            c.login()
            imgs = c.list_train(argv[1])
            print(f"{len(imgs)} train images for {argv[1]}:", imgs[:5], "...")
        elif cmd == "faces":
            c.login()
            print(json.dumps(c.list_faces(), indent=2)[:800])
        else:
            print(__doc__)
            return 2
    except (FrigateError, IndexError) as e:
        print(f"error: {e}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
