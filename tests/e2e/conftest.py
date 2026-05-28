"""Pytest fixtures for the browser smoke tests.

Runs the real review_app server IN-PROCESS against a temp review/ dir seeded with
fake candidates + targets, on an ephemeral port. Playwright then drives the actual
served page. Because it's in-process, tests can also read review_app's state
(VERDICT, FLAGGED_CIDS) directly if needed. Dev-only — never imported by runtime.
"""
import json
import os
import socket
import sys
import threading
from http.server import ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "winnow"))
import review_app  # noqa: E402


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# A blank data: URL renders instantly with no network — we test buttons, not images.
_IMG = "data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw=="

SEED_CANDS = [
    {"cid": "face|Mario|t1", "kind": "person", "identity": "Mario",
     "confidence": 0.9, "img_url": _IMG, "full_url": _IMG, "clip_url": None,
     "meta": {}, "source": "face_train", "face_train": "t1.webp"},
    {"cid": "Scooby|Scooby|d1", "kind": "dog", "identity": "Scooby", "model": "Scooby",
     "confidence": 0.8, "img_url": _IMG, "full_url": _IMG, "clip_url": None,
     "meta": {}, "source": "manual", "group": "Scooby|d1"},
]
# Toad/Toadette share a prefix — exercises the typeahead's shared-prefix filtering.
SEED_TARGETS = {"person": ["Luigi", "Mario", "Peach", "Toad", "Toadette", "Yoshi"],
                "dog": {"Scooby": ["Scooby", "Scrappy"]}}


@pytest.fixture
def winnow(tmp_path):
    """Seed a temp review dir, point review_app at it, serve it on a free port."""
    rev = tmp_path / "review"
    rev.mkdir()
    (rev / "candidates.jsonl").write_text(
        "".join(json.dumps(c) + "\n" for c in SEED_CANDS))
    (rev / "targets.json").write_text(json.dumps(SEED_TARGETS))

    review_app.REVIEW = str(rev)
    review_app.CANDS = str(rev / "candidates.jsonl")
    review_app.VERDICTS = str(rev / "verdicts.jsonl")
    review_app.FLAGGED = str(rev / "flagged.jsonl")
    review_app.TARGETS_FILE = str(rev / "targets.json")
    review_app.REFRESH_STATE_FILE = str(rev / ".refresh.json")
    review_app.REFRESH_FN = None
    review_app.load()

    port = _free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), review_app.H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield {"url": f"http://127.0.0.1:{port}", "review": rev}
    finally:
        srv.shutdown()
