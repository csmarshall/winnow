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

    def test_brindle_stocky_is_luna(self):
        self.assertEqual(self.d("stocky", "brindle", False), "Scooby")

    def test_black_lean_harness_is_frankie(self):
        self.assertEqual(self.d("lean", "solid_black", True), "Scrappy")

    def test_black_lean_no_harness_is_frankie(self):
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
        cands, categorize, face_classify, deletes, noop = commit.plan_actions()
        cats = {c["cid"]: cat for c, cat in categorize}
        self.assertEqual(cats["Scooby|Scooby|a"], "Scooby")          # positive -> identity
        self.assertEqual(cats["Scooby|Other dog|b"], "none")     # negative -> none
        self.assertEqual(len(face_classify), 1)                # confirmed face -> assign

    def test_idempotent_skip_committed(self):
        self._write("committed.jsonl", [{"cid": "Scooby|Scooby|a"}])
        _, categorize, _, _, _ = commit.plan_actions()
        self.assertNotIn("Scooby|Scooby|a", {c["cid"] for c, _ in categorize})

    def test_reject_face_goes_to_deletes(self):
        # ADR-0014: a face "reject" deletes it from the source (not categorize/noop)
        self._write("verdicts.jsonl", [{"cid": "face|Mario|tf", "verdict": "reject"}])
        _, _, face_classify, deletes, _ = commit.plan_actions()
        self.assertEqual([c["cid"] for c in deletes], ["face|Mario|tf"])
        self.assertEqual(face_classify, [])


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
    """Manual mode (option a, swipe-consistent): a multi-class model shows each crop
    ONCE — in the pool of Frigate's guessed class ('Is this <guess>?') with the normal
    swipe cross, NOT a choices/button dialog. Reassign (incl. 'none') corrects it.
    A binary model keeps the lone yes/no pool. Regression for 'seeing the same image
    multiple times' AND 'why is there a which-is-this dialog instead of swipe'."""

    class _TrainFake(FakeClient):
        def __init__(self, files): self._files = files
        def list_train(self, model): return self._files
        def get_event(self, e): return {"camera": "back_yard"}

    def test_multiclass_one_card_per_crop_in_guess_pool_no_choices(self):
        files = ["1779.5-aa-1779.6-Scooby-0.9.webp", "1779.7-bb-1779.8-Scrappy-0.8.webp"]
        cfg = build_candidates.EXAMPLE_MODELS["Scooby"]          # multi-class dog
        out = build_candidates.from_model_manual(self._TrainFake(files), "Scooby", cfg)
        self.assertEqual(len(out), 2)                            # ONE per crop, not per class
        by = {c["identity"]: c for c in out}
        self.assertIn("Scooby", by); self.assertIn("Scrappy", by)   # pooled by Frigate's guess
        self.assertEqual(by["Scooby"]["cid"], "Scooby|1779.5-aa-1779.6-Scooby-0.9.webp")
        self.assertEqual(by["Scooby"]["confidence"], 0.9)           # score parsed from filename
        for rec in out:
            self.assertNotIn("choices", rec)                       # swipe cross, not a dialog
            self.assertNotIn("group", rec)

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
            _, categorize, _, _, _ = commit.plan_actions()
            self.assertEqual([(c["model"], cat) for c, cat in categorize],
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
            _, _, face_classify, _, _ = commit.plan_actions()
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
