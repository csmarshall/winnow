#!/usr/bin/env python3
"""Winnow unit tests (stdlib unittest, no external services).

Covers the regression-prone pure logic: the identity mapper (dog_decide — which
took 4 iterations to get right), bucket routing, filename parsing, candidate
building, commit planning, and the review-app aggregation. A FakeClient stands in
for Frigate so nothing here touches the network or Ollama.

Run:  python3 -m unittest discover -s tests   (from the winnow/ project root)
"""
import json
import os
import sys
import tempfile
import unittest

T = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(T, "..", "adapters", "frigate"))
sys.path.insert(0, os.path.join(T, "..", "winnow"))

import vlm                       # noqa: E402
import classify                 # noqa: E402
import build_candidates         # noqa: E402
import commit                   # noqa: E402
import review_app               # noqa: E402


class FakeClient:
    base = "http://fake:5000"

    def login(self): return False
    def train_image_url(self, m, f): return f"{self.base}/clips/{m}/train/{f}"
    def media_url(self, p): return f"{self.base}/clips/{p}"
    def event_snapshot_url(self, e): return f"{self.base}/api/events/{e}/snapshot.jpg"
    def event_clip_url(self, e): return f"{self.base}/api/events/{e}/clip.mp4"
    def recording_snapshot_url(self, cam, ft): return f"{self.base}/api/{cam}/recordings/{ft}/snapshot.jpg"
    def get_event(self, e): return {"camera": "back_yard", "start_time": 1.0,
                                    "data": {"box": [0.1, 0.2, 0.3, 0.4]}}
    def classify_face_train(self, name, training_file): return {"ok": True}
    def list_faces(self):
        return {"train": ["1779044421.9-abc-1779044435.0-Mario-1.0.webp"],
                "Mario": ["old-library.webp"]}


class TestDogDecide(unittest.TestCase):
    """The identity mapper — the thing that broke repeatedly."""
    def d(self, build, coat, harness):
        return vlm.dog_decide({"build": build, "coat": coat, "harness": harness})

    def test_brindle_stocky_is_scooby(self):
        self.assertEqual(self.d("stocky", "brindle", False), "Scooby")

    def test_black_lean_harness_is_scrappy(self):
        self.assertEqual(self.d("lean", "solid_black", True), "Scrappy")

    def test_black_lean_no_harness_is_scrappy(self):
        self.assertEqual(self.d("lean", "solid_black", False), "Scrappy")

    def test_harness_outweighs_brindle(self):
        # the tuned cell: stocky+brindle+harness -> Scrappy (harness weight 4 > 3)
        self.assertEqual(self.d("stocky", "brindle", True), "Scrappy")

    def test_no_signal_is_unsure(self):
        self.assertEqual(self.d("unclear", "other", False), "unsure")

    def test_tie_is_unsure(self):
        # stocky(+1 scooby) vs solid_black(+1 scrappy) -> tie -> unsure
        self.assertEqual(self.d("stocky", "solid_black", False), "unsure")


class TestDecideBucket(unittest.TestCase):
    DOG = vlm.SCHEMES["dog"]
    CAR = vlm.SCHEMES["car"]

    def test_dog_confident_positive(self):
        pred = {"build": "stocky", "coat": "brindle", "harness": False,
                "confidence": 0.9, "num_dogs": 1}
        cls, bucket, _, _ = vlm.decide_bucket(self.DOG, pred, 0.65, False)
        self.assertEqual((cls, bucket), ("Scooby", "Scooby"))

    def test_low_confidence_routes_to_review(self):
        pred = {"build": "stocky", "coat": "brindle", "harness": False,
                "confidence": 0.4, "num_dogs": 1}
        _, bucket, _, _ = vlm.decide_bucket(self.DOG, pred, 0.65, False)
        self.assertEqual(bucket, "review")

    def test_multi_dog_routes_to_review(self):
        pred = {"build": "stocky", "coat": "brindle", "harness": False,
                "confidence": 0.9, "num_dogs": 2}
        _, bucket, _, _ = vlm.decide_bucket(self.DOG, pred, 0.65, False)
        self.assertEqual(bucket, "review")

    def test_dog_ignores_ir(self):
        # dogs have ir_review False — IR frame still classifies (not forced review)
        pred = {"build": "stocky", "coat": "brindle", "harness": False,
                "confidence": 0.9, "num_dogs": 1}
        _, bucket, _, _ = vlm.decide_bucket(self.DOG, pred, 0.65, True)
        self.assertEqual(bucket, "Scooby")

    def test_car_color_maps_to_identity(self):
        cls, bucket, _, _ = vlm.decide_bucket(
            self.CAR, {"color": "white", "confidence": 0.9}, 0.65, False)
        self.assertEqual((cls, bucket), ("DeLorean", "DeLorean"))

    def test_car_ir_routes_to_review(self):
        # cars have ir_review True — color is unreadable at night
        _, bucket, _, _ = vlm.decide_bucket(
            self.CAR, {"color": "white", "confidence": 0.9}, 0.65, True)
        self.assertEqual(bucket, "review")

    def test_car_other_stays_other(self):
        cls, bucket, _, _ = vlm.decide_bucket(
            self.CAR, {"color": "other", "confidence": 0.9}, 0.65, False)
        self.assertEqual((cls, bucket), ("other", "other"))


class TestFilenameParsing(unittest.TestCase):
    F = "1779730183.503659-ppeatm-1779730184.636706-unknown-0.0.webp"

    def test_event_id(self):
        self.assertEqual(classify.event_id(self.F), "1779730183.503659-ppeatm")

    def test_start_time(self):
        self.assertAlmostEqual(classify.start_time(self.F), 1779730183.503659, places=3)


class TestBuildCandidates(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig = build_candidates.REVIEW
        build_candidates.REVIEW = self.tmp
        rows = [
            {"training_file": "1.0-aa-1.0-unknown-0.0.webp", "bucket": "Scooby",
             "confidence": 0.9, "event_id": "1.0-aa", "start_time": 1.0, "obs": {}},
            {"training_file": "2.0-bb-2.0-unknown-0.0.webp", "bucket": "other",
             "confidence": 0.8, "event_id": "2.0-bb", "start_time": 2.0, "obs": {}},
            {"training_file": "3.0-cc-3.0-unknown-0.0.webp", "bucket": "review",
             "confidence": 0.3, "event_id": "3.0-cc", "start_time": 3.0, "obs": {}},
        ]
        with open(os.path.join(self.tmp, "results_Scooby.jsonl"), "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        self.cfg = build_candidates.EXAMPLE_MODELS["Scooby"]

    def tearDown(self):
        build_candidates.REVIEW = self._orig

    def test_routing_and_urls(self):
        out = build_candidates.from_model(FakeClient(), "Scooby", self.cfg)
        by = {c["identity"]: c for c in out}
        self.assertEqual(len(out), 2)               # review row excluded
        self.assertEqual(by["Scooby"]["role"], "positive")
        self.assertEqual(by["Other dog"]["role"], "negative")
        self.assertIn("question", by["Other dog"])
        self.assertIn("/events/1.0-aa/snapshot.jpg", by["Scooby"]["full_url"])
        self.assertIn("timestamp=0", by["Scooby"]["full_url"])  # Frigate's UTC overlay off
        self.assertTrue(by["Scooby"]["clip_url"].endswith("/events/1.0-aa/clip.mp4"))


class TestCommitPlan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig = commit.REVIEW
        commit.REVIEW = self.tmp
        cands = [
            {"cid": "Scooby|Scooby|a", "kind": "dog", "role": "positive",
             "identity": "Scooby", "model": "Scooby", "training_file": "a"},
            {"cid": "Scooby|Other dog|b", "kind": "dog", "role": "negative",
             "identity": "Other dog", "model": "Scooby", "training_file": "b"},
            {"cid": "face|Mario|tf", "kind": "person", "identity": "Mario",
             "face_train": "1779-x-1779-Mario-1.0.webp"},
        ]
        self._write("candidates.jsonl", cands)
        self._write("verdicts.jsonl", [
            {"cid": "Scooby|Scooby|a", "verdict": "yes"},
            {"cid": "Scooby|Other dog|b", "verdict": "yes"},
            {"cid": "face|Mario|tf", "verdict": "yes"},
        ])

    def _write(self, name, rows):
        with open(os.path.join(self.tmp, name), "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

    def tearDown(self):
        commit.REVIEW = self._orig

    def test_plan(self):
        p = commit.plan_actions()
        cats = {c["cid"]: cat for c, cat, tf in p.categorize}   # (cand, category, file) triples
        self.assertEqual(cats["Scooby|Scooby|a"], "Scooby")          # positive -> identity
        self.assertEqual(cats["Scooby|Other dog|b"], "none")     # negative -> none
        self.assertEqual(len(p.face_classify), 1)                # confirmed face -> assign

    def test_idempotent_skip_committed(self):
        self._write("committed.jsonl", [{"cid": "Scooby|Scooby|a"}])
        p = commit.plan_actions()
        self.assertNotIn("Scooby|Scooby|a", {c["cid"] for c, _, _ in p.categorize})

    def test_reject_face_goes_to_deletes(self):
        # ADR-0014: a face "reject" deletes it from the source (not categorize/noop)
        self._write("verdicts.jsonl", [{"cid": "face|Mario|tf", "verdict": "reject"}])
        p = commit.plan_actions()
        self.assertEqual([c["cid"] for c in p.deletes], ["face|Mario|tf"])
        self.assertEqual(p.face_classify, [])


class TestReviewApp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        review_app.VERDICTS = os.path.join(self.tmp, "verdicts.jsonl")
        review_app.CAND = {
            "dog|Scooby|a": {"cid": "dog|Scooby|a", "kind": "dog", "identity": "Scooby",
                           "confidence": 0.9, "img_url": "u1", "full_url": "f1",
                           "clip_url": "c1", "meta": {}, "source": "x"},
            "dog|Scooby|b": {"cid": "dog|Scooby|b", "kind": "dog", "identity": "Scooby",
                           "confidence": 0.8, "img_url": "u2", "meta": {}, "source": "x"},
            "person|Mario|f": {"cid": "person|Mario|f", "kind": "person",
                                 "identity": "Mario", "confidence": None,
                                 "img_url": "u3", "meta": {}, "source": "x"},
        }
        review_app.VERDICT = {"dog|Scooby|b": "yes"}

    def test_identities_counts(self):
        rows = {(r["kind"], r["identity"]): r for r in review_app.identities()}
        scooby = rows[("dog", "Scooby")]
        self.assertEqual((scooby["total"], scooby["yes"], scooby["pending"]), (2, 1, 1))

    def test_queue_excludes_decided_and_carries_urls(self):
        q = review_app.queue("dog", "Scooby")
        self.assertEqual([c["cid"] for c in q], ["dog|Scooby|a"])   # b is decided
        self.assertEqual(q[0]["full_url"], "f1")
        self.assertEqual(q[0]["clip_url"], "c1")
        self.assertEqual(q[0]["kind"], "dog")     # client needs kind for the "?" picker

    def test_status_summary(self):
        st = review_app.status()
        self.assertEqual(st["n_pools"], 2)     # Scooby + Mario have pending
        self.assertEqual(st["n_types"], 2)     # dog + person

    def test_record_persists(self):
        review_app.record("dog|Scooby|a", "yes")
        self.assertEqual(review_app.VERDICT["dog|Scooby|a"], "yes")
        lines = open(review_app.VERDICTS).read()
        self.assertIn("dog|Scooby|a", lines)


class TestBinarySweep(unittest.TestCase):
    """Manual mode: confirming an image in one subtype removes it from others."""
    def setUp(self):
        # same image 'x' in both Scooby and Scrappy pools, shared group
        review_app.CAND = {
            "Scooby|Scooby|x": {"cid": "Scooby|Scooby|x", "kind": "dog", "identity": "Scooby",
                            "confidence": None, "group": "Scooby|x", "meta": {},
                            "source": "manual", "img_url": "u"},
            "Scooby|Scrappy|x": {"cid": "Scooby|Scrappy|x", "kind": "dog", "identity": "Scrappy",
                               "confidence": None, "group": "Scooby|x", "meta": {},
                               "source": "manual", "img_url": "u"},
        }
        review_app.VERDICT = {"Scooby|Scooby|x": "yes"}   # x confirmed as Scooby

    def test_confirmed_image_drops_from_other_pool(self):
        self.assertEqual(review_app.queue("dog", "Scrappy"), [])   # x gone from Scrappy
        rows = {(r["kind"], r["identity"]): r for r in review_app.identities()}
        self.assertNotIn(("dog", "Scrappy"), rows)                 # no Scrappy work left
        self.assertEqual(rows[("dog", "Scooby")]["yes"], 1)


class TestManualMultiClass(unittest.TestCase):
    """Manual mode (ADR-0015, event-level): one card per EVENT, pooled by the best
    crop's guess, with the normal swipe cross (no choices dialog). One event's many
    near-identical crops collapse to a single card carrying the whole event + a capped
    keep-set. Regression for 'same image keeps coming up' / 'which-is-this dialog'."""

    class _TrainFake(FakeClient):
        def __init__(self, files): self._files = files
        def list_train(self, model): return self._files
        def get_event(self, e): return {"camera": "back_yard"}

    def test_distinct_events_one_card_each_pooled_by_guess(self):
        files = ["1779.5-aa-1779.6-Scooby-0.9.webp", "1779.7-bb-1779.8-Scrappy-0.8.webp"]
        cfg = build_candidates.EXAMPLE_MODELS["Scooby"]          # multi-class dog
        out = build_candidates.from_model_manual(self._TrainFake(files), "Scooby", cfg)
        self.assertEqual(len(out), 2)                            # two events -> two cards
        by = {c["identity"]: c for c in out}
        self.assertIn("Scooby", by); self.assertIn("Scrappy", by)   # pooled by best-crop guess
        self.assertEqual(by["Scooby"]["cid"], "Scooby|1779.5-aa")    # cid = model|event
        self.assertEqual(by["Scooby"]["confidence"], 0.9)
        for rec in out:
            self.assertNotIn("choices", rec)                       # swipe cross, not a dialog
            self.assertNotIn("group", rec)

    def test_one_event_many_crops_collapses_with_capped_keepset(self):
        # 5 crops of ONE event -> ONE card; all carried, keep-set capped at default 3
        files = [f"1779.0-zz-1779.{i}-Scooby-0.{i}.webp" for i in range(1, 6)]
        cfg = build_candidates.EXAMPLE_MODELS["Scooby"]
        out = build_candidates.from_model_manual(self._TrainFake(files), "Scooby", cfg)
        self.assertEqual(len(out), 1)                            # one event -> one card
        c = out[0]
        self.assertEqual(len(c["event_files"]), 5)               # all crops carried
        self.assertEqual(len(c["keep_files"]), 3)                # capped (WINNOW_KEEP_PER_EVENT=3)
        self.assertEqual(len(c["keep_urls"]), 3)                 # lightbox filmstrip set
        self.assertTrue(set(c["keep_files"]) <= set(c["event_files"]))

    def test_binary_single_yesno_pool(self):
        files = ["1779.5-aa-1779.6-Batmobile-0.9.webp"]
        cfg = build_candidates.EXAMPLE_MODELS["Batmobile"]       # binary car
        out = build_candidates.from_model_manual(self._TrainFake(files), "Batmobile", cfg)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["identity"], "Batmobile")
        self.assertNotIn("choices", out[0])

    def test_multiclass_reassign_targets_include_none(self):
        class C(FakeClient):
            def get_config(self):
                return {"classification": {"custom": {"Scooby": {
                    "object_config": {"objects": ["dog"]}}}}}
            def get_dataset(self, m): return {"categories": {"Scooby": [], "Scrappy": [], "none": []}}
            def list_train(self, m): return []
            def list_faces(self): return {"train": []}
        import tempfile, os as _os
        tmp = tempfile.mkdtemp(); orig = build_candidates.REVIEW
        build_candidates.REVIEW = tmp
        try:
            build_candidates.build(C(), manual=True)
            t = json.load(open(_os.path.join(tmp, "targets.json")))
            self.assertIn("none", t["dog"]["Scooby"])           # hard-negative reachable via reassign
        finally:
            build_candidates.REVIEW = orig


class TestFrameSelection(unittest.TestCase):
    """Seed filtering + best-frame-per-event picking (manual dedup)."""
    def test_real_event_filter(self):
        self.assertTrue(build_candidates._real_event("1779.5-abc-1779.6-Scooby-1.0.webp"))
        self.assertFalse(build_candidates._real_event("example_003.jpg"))
        self.assertFalse(build_candidates._real_event(""))

    def test_score_of(self):
        self.assertEqual(build_candidates._score_of("1.0-a-1.0-Scooby-0.97.webp"), 0.97)
        self.assertEqual(build_candidates._score_of("1.0-a-1.0-unknown-0.0.webp"), 0.0)
        self.assertEqual(build_candidates._score_of("garbage"), 0.0)   # unparseable

    def test_best_by_event_keeps_highest_score(self):
        # two crops of event '1.0-a' (scores .4/.9) + one of '2.0-b'
        files = ["1.0-a-1.0-Scooby-0.4.webp", "1.0-a-1.5-Scooby-0.9.webp",
                 "2.0-b-2.0-Scooby-0.7.webp"]
        out = build_candidates._best_by_event(files)
        self.assertEqual(len(out), 2)                       # one per event
        self.assertIn("1.0-a-1.5-Scooby-0.9.webp", out)       # the 0.9 won over 0.4
        self.assertNotIn("1.0-a-1.0-Scooby-0.4.webp", out)


class TestFaceFrameCameraProbe(unittest.TestCase):
    """Aged-out face events: the tracked event is gone (get_event fails -> no
    camera), but the recording outlives event metadata and is still on disk. The
    builder must probe cameras to recover the full scene instead of dropping to the
    event snapshot (which 404s) and then the tiny crop. Regression for the 'click
    shows a small image' bug on faces whose events aged out."""

    def setUp(self):
        build_candidates._CAM_CACHE.clear()   # don't leak camera list across tests

    def _client(self):
        class Probe(FakeClient):
            def get_event(self, e): raise Exception("404 — event aged out")
            def get_config(self):
                return {"cameras": {"front_door": {}, "back_yard": {}, "side_door": {}}}
            def recording_snapshot_exists(self, cam, ft): return cam == "side_door"
        return Probe()

    def test_recovers_camera_from_recording_when_event_gone(self):
        rec = {}
        build_candidates._attach_face_frame(self._client(), "1779.5-abc", "1779.6", {}, rec)
        self.assertEqual(rec["full_url"],
                         "http://fake:5000/api/side_door/recordings/1779.6/snapshot.jpg")
        self.assertNotEqual(rec["full_url"], rec["full_url_alt"])   # not the 404 event snapshot

    def test_falls_back_to_event_snapshot_when_no_recording_anywhere(self):
        c = self._client()
        c.recording_snapshot_exists = lambda cam, ft: False        # nothing on disk
        rec = {}
        build_candidates._attach_face_frame(c, "1779.5-abc", "1779.6", {}, rec)
        self.assertEqual(rec["full_url"], rec["full_url_alt"])       # graceful last resort

    def test_probe_skipped_when_event_still_has_camera(self):
        # the common case: event present -> use its camera, never probe
        rec = {}
        build_candidates._attach_face_frame(FakeClient(), "1779.5-abc", "1779.6", {}, rec)
        self.assertEqual(rec["full_url"],
                         "http://fake:5000/api/back_yard/recordings/1779.6/snapshot.jpg")


class TestUrlEncoding(unittest.TestCase):
    """Model names with spaces (e.g. 'Mystery Machine') must be URL-encoded."""
    def test_seg_encodes_space(self):
        import frigate_client
        self.assertEqual(frigate_client._seg("Mystery Machine"), "Mystery%20Machine")
        self.assertEqual(frigate_client._seg("Scooby"), "Scooby")


class TestCarsConfig(unittest.TestCase):
    def test_cars_are_binary_car_models(self):
        cfg = build_candidates.EXAMPLE_MODELS
        for car in ("Batmobile", "DeLorean"):
            self.assertEqual(cfg[car]["kind"], "car")
            self.assertEqual(cfg[car]["identities"], [car])     # binary: just itself


class TestUnknownFacesSurfaced(unittest.TestCase):
    """ADR-0014: Frigate's unknown/none faces are NOT dropped — they're surfaced
    in the 'Needs ID' pool (unidentified=True) for the human to identify."""
    def test_unknown_faces_go_to_needs_id(self):
        class FaceFake(FakeClient):
            def list_faces(self):
                return {"train": [
                    "1.0-a-1.5-Peach-0.99.webp",   # a guess -> Peach pool
                    "2.0-b-2.5-unknown-0.0.webp",  # unrecognized -> Needs ID
                    "3.0-c-3.5-none-0.0.webp",     # unrecognized -> Needs ID
                ]}
        out = build_candidates.from_faces(FaceFake())
        by_identity = {c["identity"]: c for c in out}
        self.assertIn("Peach", by_identity)
        self.assertIn(build_candidates.NEEDS_ID, by_identity)
        self.assertFalse(by_identity["Peach"]["unidentified"])
        self.assertTrue(by_identity[build_candidates.NEEDS_ID]["unidentified"])


class TestReassign(unittest.TestCase):
    """The "?" reassign: assign:<target> verdicts route to the right action and
    claim the image away from sibling pools."""
    def test_is_assignment(self):
        self.assertTrue(review_app._is_assignment("yes"))
        self.assertTrue(review_app._is_assignment("assign:Scrappy"))
        for v in ("no", "none", "skip", None):
            self.assertFalse(review_app._is_assignment(v))

    def test_commit_routes_dog_reassign_to_categorize(self):
        tmp = tempfile.mkdtemp(); orig = commit.REVIEW; commit.REVIEW = tmp
        try:
            with open(os.path.join(tmp, "candidates.jsonl"), "w") as f:
                f.write(json.dumps({"cid": "Scooby|Scooby|a", "kind": "dog",
                    "identity": "Scooby", "model": "Scooby", "training_file": "a"}) + "\n")
            with open(os.path.join(tmp, "verdicts.jsonl"), "w") as f:
                f.write(json.dumps({"cid": "Scooby|Scooby|a", "verdict": "assign:Scrappy"}) + "\n")
            p = commit.plan_actions(); categorize = p.categorize
            self.assertEqual([(c["model"], cat) for c, cat, tf in categorize],
                             [("Scooby", "Scrappy")])          # reassigned to Scrappy
        finally:
            commit.REVIEW = orig

    def test_commit_routes_face_reassign_to_classify(self):
        tmp = tempfile.mkdtemp(); orig = commit.REVIEW; commit.REVIEW = tmp
        try:
            with open(os.path.join(tmp, "candidates.jsonl"), "w") as f:
                f.write(json.dumps({"cid": "face|Luigi|t", "kind": "person",
                    "identity": "Luigi", "face_train": "t.webp"}) + "\n")
            with open(os.path.join(tmp, "verdicts.jsonl"), "w") as f:
                f.write(json.dumps({"cid": "face|Luigi|t", "verdict": "assign:Peach"}) + "\n")
            p = commit.plan_actions(); face_classify = p.face_classify
            self.assertEqual([(c["cid"], name) for c, name in face_classify],
                             [("face|Luigi|t", "Peach")])  # re-named, not the guess
        finally:
            commit.REVIEW = orig

    def test_reassign_claims_image_from_sibling_pool(self):
        review_app.CAND = {
            "Scooby|Scooby|x": {"cid": "Scooby|Scooby|x", "kind": "dog", "identity": "Scooby",
                            "confidence": None, "group": "Scooby|x", "meta": {},
                            "source": "manual", "img_url": "u"},
            "Scooby|Scrappy|x": {"cid": "Scooby|Scrappy|x", "kind": "dog", "identity": "Scrappy",
                               "confidence": None, "group": "Scooby|x", "meta": {},
                               "source": "manual", "img_url": "u"},
        }
        review_app.VERDICT = {"Scooby|Scrappy|x": "assign:Scrappy"}   # "?" -> Scrappy
        # the image is claimed -> gone from the Scooby pool too (group exclusivity)
        self.assertEqual(review_app.queue("dog", "Scooby"), [])

    def test_reassign_counts_as_moved_not_match(self):
        review_app.CAND = {
            "Scooby|Scooby|x": {"cid": "Scooby|Scooby|x", "kind": "dog", "identity": "Scooby",
                            "confidence": None, "group": "Scooby|x", "meta": {},
                            "source": "manual", "img_url": "u"},
        }
        review_app.VERDICT = {"Scooby|Scooby|x": "assign:Scrappy"}
        scooby = {(r["kind"], r["identity"]): r for r in review_app.identities()}[("dog", "Scooby")]
        self.assertEqual((scooby["moved"], scooby["yes"], scooby["pending"]), (1, 0, 0))

    def test_reshuffle_single_assign_allocates_to_target(self):
        # ADR-0014 (one-candidate-per-crop): a single assign:<target> verdict marks the
        # crop reassigned-OUT of its source/guess pool AND allocated (decided, yes) in
        # the target pool — never re-reviewed in either.
        review_app.FLAGGED_CIDS = set()
        review_app.CAND = {
            "Dogs|x": {"cid": "Dogs|x", "kind": "dog", "identity": "Scooby",
                       "model": "Dogs", "meta": {}, "img_url": "u"},
        }
        review_app.VERDICT = {"Dogs|x": "assign:Scrappy"}
        rows = {(r["kind"], r["identity"]): r for r in review_app.identities()}
        self.assertEqual(rows[("dog", "Scooby")]["moved"], 1)      # reassigned out of source
        self.assertEqual(rows[("dog", "Scooby")]["pending"], 0)
        self.assertEqual(rows[("dog", "Scrappy")]["yes"], 1)       # allocated in target
        self.assertEqual(rows[("dog", "Scrappy")]["pending"], 0)   # decided -> not re-reviewed
        self.assertEqual(review_app.queue("dog", "Scrappy"), [])    # not queued for review


class TestEventLevelCommit(unittest.TestCase):
    """ADR-0015: committing an event categorizes a capped diverse keep-set to the
    decided class and PRUNES the rest from the train pool, in one atomic action."""
    def setUp(self):
        self.tmp = tempfile.mkdtemp(); self._orig = commit.REVIEW; commit.REVIEW = self.tmp
    def tearDown(self): commit.REVIEW = self._orig
    def _w(self, name, rows):
        with open(os.path.join(self.tmp, name), "w") as f:
            for r in rows: f.write(json.dumps(r) + "\n")

    def test_yes_categorizes_keepset_prunes_rest(self):
        ev = [f"1779.0-zz-1779.{i}-DeLorean-0.{i}.webp" for i in range(1, 6)]  # 5 crops, 1 event
        keep = ev[:3]
        self._w("candidates.jsonl", [{"cid": "Cars|1779.0-zz", "kind": "car",
            "identity": "DeLorean", "model": "Cars", "training_file": ev[0],
            "event_files": ev, "keep_files": keep}])
        self._w("verdicts.jsonl", [{"cid": "Cars|1779.0-zz", "verdict": "yes"}])
        p = commit.plan_actions(); categorize = p.categorize; train_deletes = p.train_deletes
        self.assertEqual(sorted(tf for c, cat, tf in categorize), sorted(keep))   # keep-set categorized
        self.assertTrue(all(cat == "DeLorean" for c, cat, tf in categorize))      # as the event's class
        pruned = [f for c, files in train_deletes for f in files]
        self.assertEqual(sorted(pruned), sorted(ev[3:]))                          # rest pruned

    def test_reassign_categorizes_keepset_to_target(self):
        ev = ["1779.0-zz-1779.1-DeLorean-0.9.webp", "1779.0-zz-1779.2-none-0.5.webp"]
        self._w("candidates.jsonl", [{"cid": "Cars|1779.0-zz", "kind": "car",
            "identity": "DeLorean", "model": "Cars", "training_file": ev[0],
            "event_files": ev, "keep_files": ev}])
        self._w("verdicts.jsonl", [{"cid": "Cars|1779.0-zz", "verdict": "assign:Batmobile"}])
        p = commit.plan_actions(); categorize = p.categorize
        self.assertTrue(categorize and all(cat == "Batmobile" for c, cat, tf in categorize))


class TestQueuePayloadContract(unittest.TestCase):
    """The /api/queue payload is a curated subset; it MUST carry the fields the
    reassign UI reads. A missing 'kind' silently disabled the "?" button + 'r'
    (canReassign(c) -> undefined==='person' -> false). This locks the contract."""
    def setUp(self):
        review_app.FLAGGED_CIDS = set()
        review_app.VERDICT = {}
        review_app.CAND = {
            "Scooby|Scooby|a": {"cid": "Scooby|Scooby|a", "kind": "dog", "identity": "Scooby",
                "model": "Scooby", "confidence": 0.5, "img_url": "u", "full_url": "f",
                "clip_url": "c", "meta": {}, "source": "manual", "group": "Scooby|a"},
            "face|Luigi|t": {"cid": "face|Luigi|t", "kind": "person", "identity": "Luigi",
                "confidence": None, "img_url": "u", "full_url": "f", "clip_url": "c",
                "meta": {}, "source": "face_train", "face_train": "t.webp"},
        }

    def test_dog_card_carries_kind_identity_model(self):
        c = review_app.queue("dog", "Scooby")[0]
        # canReassign needs kind; targetsFor(dog) needs model -> the model's categories
        self.assertEqual(c["kind"], "dog")
        self.assertEqual(c["identity"], "Scooby")
        self.assertEqual(c["model"], "Scooby")

    def test_face_card_carries_kind(self):
        c = review_app.queue("person", "Luigi")[0]
        self.assertEqual(c["kind"], "person")   # canReassign(c) must be true

    def test_payload_has_all_ui_fields(self):
        c = review_app.queue("person", "Luigi")[0]
        for f in ("cid", "kind", "identity", "img_url", "full_url", "clip_url",
                  "box", "choices", "confidence"):
            self.assertIn(f, c)


class TestTargetsGeneration(unittest.TestCase):
    """build() writes review/targets.json — the reassign picker's option lists."""
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig = build_candidates.REVIEW
        build_candidates.REVIEW = self.tmp

    def tearDown(self):
        build_candidates.REVIEW = self._orig

    def test_build_writes_reassign_targets(self):
        class C(FakeClient):
            def list_train(self, m): return []
            def list_faces(self): return {"train": [], "Luigi": [], "Peach": []}
        build_candidates.build(client=C(), manual=True)
        t = json.load(open(os.path.join(self.tmp, "targets.json")))
        # multi-class categories + 'none' (hard-negative reachable via reassign)
        self.assertEqual(t["dog"]["Scooby"], ["Scooby", "Scrappy", "none"])
        self.assertEqual(t["person"], ["Luigi", "Peach"])       # face-library names
        self.assertNotIn("DeLorean", t["dog"])                # cars aren't reassignable

    def test_status_exposes_targets(self):
        review_app.FLAGGED_CIDS = set(); review_app.VERDICT = {}; review_app.CAND = {}
        review_app.TARGETS = {"dog": {"Scooby": ["Scooby", "Scrappy"]}, "person": ["Luigi"]}
        st = review_app.status()
        self.assertEqual(st["targets"]["person"], ["Luigi"])
        self.assertEqual(st["targets"]["dog"]["Scooby"], ["Scooby", "Scrappy"])


class TestLibraryCuration(unittest.TestCase):
    """ADR-0016: surface already-committed items (face library + classifier
    datasets) for review, separately from daily train-pool work. Same swipe UI;
    different APIs at commit time."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._b, self._c = build_candidates.REVIEW, commit.REVIEW
        build_candidates.REVIEW = commit.REVIEW = self.tmp

    def tearDown(self):
        build_candidates.REVIEW = self._b
        commit.REVIEW = self._c

    def _w(self, name, rows):
        with open(os.path.join(self.tmp, name), "w") as f:
            for r in rows: f.write(json.dumps(r) + "\n")

    def test_library_candidates_built_for_faces_and_datasets(self):
        class C(FakeClient):
            def list_train(self, m): return []
            def list_faces(self):
                return {"train": [], "Luigi": ["Luigi-1.webp", "Luigi-2.webp"],
                        "Peach": ["Peach-1.webp"]}
            def get_dataset(self, m):
                return {"categories": {"Scooby": ["Scooby-1.png"], "Scrappy": [], "none": []}}
        build_candidates.build(client=C(), manual=True)
        cands = [json.loads(l) for l in open(os.path.join(self.tmp, "candidates.jsonl"))]
        lib = [c for c in cands if c.get("bucket") == "library"]
        # 3 face library + 1 dataset library = 4 (Batmobile/DeLorean have empty datasets)
        self.assertGreaterEqual(len(lib), 4)
        faces = {c["cid"]: c for c in lib if c["source"] == "library_face"}
        self.assertIn("face_lib|Luigi|Luigi-1.webp", faces)
        self.assertEqual(faces["face_lib|Luigi|Luigi-1.webp"]["kind"], "person")
        ds = {c["cid"]: c for c in lib if c["source"] == "library_dataset"}
        scooby = next(c for c in ds.values() if "Scooby-1.png" in c["cid"])
        self.assertEqual(scooby["library_category"], "Scooby")
        self.assertEqual(scooby["model"], "Scooby")

    def test_library_review_disabled_via_env(self):
        # Respect WINNOW_LIBRARY_REVIEW=0 (hide the cleanup section entirely).
        orig = build_candidates.LIBRARY
        build_candidates.LIBRARY = False
        try:
            class C(FakeClient):
                def list_train(self, m): return []
                def list_faces(self):
                    return {"train": [], "Luigi": ["Luigi-1.webp"]}
            build_candidates.build(client=C(), manual=True)
            cands = [json.loads(l) for l in open(os.path.join(self.tmp, "candidates.jsonl"))]
            self.assertEqual([c for c in cands if c.get("bucket") == "library"], [])
        finally:
            build_candidates.LIBRARY = orig

    def test_commit_face_library_reject_calls_delete(self):
        self._w("candidates.jsonl", [{
            "cid": "face_lib|Mario|x.webp", "kind": "person", "identity": "Mario",
            "bucket": "library", "source": "library_face",
            "face_lib_name": "Mario", "library_id": "x.webp"}])
        self._w("verdicts.jsonl", [{"cid": "face_lib|Mario|x.webp", "verdict": "reject"}])
        p = commit.plan_actions()
        self.assertEqual([c["cid"] for c in p.lib_face_dels], ["face_lib|Mario|x.webp"])
        self.assertEqual(p.deletes, [])         # NOT routed to the train-pool deletes path

    def test_commit_face_library_reassign_is_move(self):
        # The Luigi -> Mario correction: assign:Luigi moves out of Mario
        self._w("candidates.jsonl", [{
            "cid": "face_lib|Mario|x.webp", "kind": "person", "identity": "Mario",
            "bucket": "library", "source": "library_face",
            "face_lib_name": "Mario", "library_id": "x.webp"}])
        self._w("verdicts.jsonl", [{"cid": "face_lib|Mario|x.webp", "verdict": "assign:Luigi"}])
        p = commit.plan_actions()
        self.assertEqual(p.lib_face_moves, [(
            {"cid": "face_lib|Mario|x.webp", "kind": "person", "identity": "Mario",
             "bucket": "library", "source": "library_face",
             "face_lib_name": "Mario", "library_id": "x.webp"}, "Luigi")])

    def test_commit_dataset_library_reassign_uses_reclassify(self):
        self._w("candidates.jsonl", [{
            "cid": "Cars_lib|Batmobile|c.png", "kind": "car", "identity": "Batmobile",
            "bucket": "library", "source": "library_dataset",
            "model": "Cars", "library_category": "Batmobile", "library_id": "c.png"}])
        self._w("verdicts.jsonl", [{"cid": "Cars_lib|Batmobile|c.png",
                                    "verdict": "assign:DeLorean"}])
        p = commit.plan_actions()
        self.assertEqual(len(p.lib_data_moves), 1)
        cand, target = p.lib_data_moves[0]
        self.assertEqual((cand["model"], cand["library_category"], target),
                         ("Cars", "Batmobile", "DeLorean"))

    def test_library_yes_is_a_noop_not_a_categorize(self):
        # "Yes" on a library item = "still correct"; no API call needed, just track
        # the verdict so we don't ask again. Critically it must NOT fall through
        # to the train-pool categorize path (which would crash with no training_file).
        self._w("candidates.jsonl", [{
            "cid": "Cars_lib|Batmobile|c.png", "kind": "car", "identity": "Batmobile",
            "bucket": "library", "source": "library_dataset",
            "model": "Cars", "library_category": "Batmobile", "library_id": "c.png"}])
        self._w("verdicts.jsonl", [{"cid": "Cars_lib|Batmobile|c.png", "verdict": "yes"}])
        p = commit.plan_actions()
        self.assertEqual(p.categorize, [])
        self.assertEqual(p.lib_data_moves, [])
        self.assertEqual(p.lib_data_dels, [])
        self.assertIn("Cars_lib|Batmobile|c.png", p.noop)


class TestBucketSeparation(unittest.TestCase):
    """A library candidate must NOT contaminate the daily-review pool counts,
    and queue(kind, identity, bucket) must keep the two buckets distinct."""

    def setUp(self):
        review_app.FLAGGED_CIDS = set(); review_app.VERDICT = {}
        review_app.CAND = {
            "face|Charles|t1": {"cid": "face|Charles|t1", "kind": "person",
                "identity": "Charles", "face_train": "t1.webp", "meta": {}, "img_url": "u",
                "confidence": 0.9},
            "face_lib|Charles|x.webp": {"cid": "face_lib|Charles|x.webp", "kind": "person",
                "identity": "Charles", "bucket": "library", "source": "library_face",
                "face_lib_name": "Charles", "library_id": "x.webp",
                "meta": {}, "img_url": "u", "confidence": None},
        }

    def test_identities_returns_distinct_review_and_library_rows(self):
        rows = {(r["kind"], r["identity"], r["bucket"]): r for r in review_app.identities()}
        self.assertIn(("person", "Charles", "review"), rows)
        self.assertIn(("person", "Charles", "library"), rows)
        self.assertEqual(rows[("person", "Charles", "review")]["total"], 1)
        self.assertEqual(rows[("person", "Charles", "library")]["total"], 1)

    def test_queue_filters_by_bucket(self):
        review_q = review_app.queue("person", "Charles", "review")
        library_q = review_app.queue("person", "Charles", "library")
        self.assertEqual([c["cid"] for c in review_q], ["face|Charles|t1"])
        self.assertEqual([c["cid"] for c in library_q], ["face_lib|Charles|x.webp"])
        self.assertEqual(library_q[0]["bucket"], "library")    # payload carries it


class TestCommitGate(unittest.TestCase):
    """Commit is user-triggered (ADR-0013): _uncommitted counts the actionable
    decisions not yet pushed (yes / reassignment), not no/skip, not already-committed."""
    def setUp(self):
        review_app.FLAGGED_CIDS = set(); review_app.COMMITTED_CIDS = set()
        review_app.CAND = {
            "Scooby|Scooby|a": {"cid": "Scooby|Scooby|a", "kind": "dog",
                "identity": "Scooby", "group": "Scooby|a", "meta": {}},
            "Scooby|Scrappy|b": {"cid": "Scooby|Scrappy|b", "kind": "dog",
                "identity": "Scrappy", "group": "Scooby|b", "meta": {}},
            "face|Mario|t": {"cid": "face|Mario|t", "kind": "person",
                "identity": "Mario", "meta": {}},
        }

    def test_counts_actionable_uncommitted(self):
        review_app.VERDICT = {"Scooby|Scooby|a": "yes",      # actionable
                              "Scooby|Scrappy|b": "assign:Scooby",  # actionable
                              "face|Mario|t": "no"}          # no-op, not counted
        self.assertEqual(review_app._uncommitted(), 2)

    def test_committed_are_excluded(self):
        review_app.VERDICT = {"Scooby|Scooby|a": "yes"}
        self.assertEqual(review_app._uncommitted(), 1)
        review_app.COMMITTED_CIDS = {"Scooby|Scooby|a"}
        self.assertEqual(review_app._uncommitted(), 0)


class TestCommitRefreshClock(unittest.TestCase):
    """A commit rebuilds the candidate queue, so it must stamp REFRESH['last'] —
    otherwise the home screen falsely shows 'last refreshed <long ago>. Check for
    new' right after a commit, even though the data is fresh."""
    def test_commit_stamps_refresh_clock(self):
        review_app.REFRESH.update(last=0.0, summary="")
        prev = review_app.COMMIT_FN
        review_app.COMMIT_FN = lambda: "ok"
        try:
            review_app._do_commit()
        finally:
            review_app.COMMIT_FN = prev
        self.assertGreater(review_app.REFRESH["last"], 0)
        self.assertEqual(review_app.REFRESH["summary"], "refreshed as part of commit")


if __name__ == "__main__":
    unittest.main(verbosity=2)
