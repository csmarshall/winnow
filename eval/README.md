# Winnow eval harness — does diversity-curation actually help?

Offline A/B to answer task #5: **does a diverse, capped, deduped dataset train a
*better* (or no worse) classifier than the bloated "everything" dataset?** We measure
*before* changing production (validate-first), because Frigate exposes no eval/test
API — but it does save the trained model to `model_cache/<model>/model.tflite` +
`labelmap.txt`, so we run inference ourselves.

This is **eval tooling** — none of it is wired into the live Winnow commit path.

## Setup (one time)
```sh
python3 -m venv eval/.venv --system-site-packages   # reuses system numpy/Pillow
eval/.venv/bin/pip install ai-edge-litert            # the TFLite runtime
```

## The tools
- `split_holdout.py` — cluster-aware train/holdout split (whole near-dup clusters go to
  one side, so the holdout can't leak into training and inflate the bloated model).
- `diversity_sample.py` — regenerate a diverse, capped, deduped dataset from a source.
- `score_model.py` — run a `model.tflite` over a labeled holdout → accuracy + confusion.

## The experiment (one model type at a time, e.g. Dogs)

**1. Split, leakage-free** (read-only on your data):
```sh
eval/.venv/bin/python eval/split_holdout.py \
  --src /opt/nvr/frigate/clips/Dogs/dataset --out /tmp/dogs_split \
  --holdout-frac 0.2 --apply
```
→ `/tmp/dogs_split/train` (the "everything" training set) + `/tmp/dogs_split/holdout`.

**2. Build the reduced training set** from the *same* train split:
```sh
eval/.venv/bin/python eval/diversity_sample.py \
  --src /tmp/dogs_split/train --out /tmp/dogs_reduced --cap 80 --dedup 4 --apply
```

**3. Train two eval models in Frigate** *(your step — Frigate owns training)*:
   - Create `Dogs_everything` (object `dog`, sub_label) and `Dogs_reduced`.
   - Populate their dataset dirs: `clips/Dogs_everything/dataset/` ← `/tmp/dogs_split/train`,
     `clips/Dogs_reduced/dataset/` ← `/tmp/dogs_reduced` (sudo copy; root-owned).
   - Train both in the Frigate UI/API → each writes `model_cache/Dogs_*/model.tflite`.

**4. Score both on the SAME holdout** and compare:
```sh
V=eval/.venv/bin/python
$V eval/score_model.py --model /opt/sw/frigate/config/model_cache/Dogs_everything/model.tflite \
   --labels /opt/sw/frigate/config/model_cache/Dogs_everything/labelmap.txt --holdout /tmp/dogs_split/holdout
$V eval/score_model.py --model /opt/sw/frigate/config/model_cache/Dogs_reduced/model.tflite \
   --labels /opt/sw/frigate/config/model_cache/Dogs_reduced/labelmap.txt --holdout /tmp/dogs_split/holdout
```

**5. Read it:** if `reduced` ≥ `everything` accuracy (and per-class recall holds), diversity
wins → green-light deploying event-tagging (#1) + regenerating the live dataset. If it
loses, raise `--cap` / lower `--dedup` and re-run, or keep the current approach. Repeat
for Cars.

## Notes
- The everything-vs-reduced comparison is *relative* through one identical preprocessing
  pipeline, so it's valid even though we can't perfectly match Frigate's internal preprocessing.
- Selecting the keep-set by time-spread vs lowest-confidence vs quality is itself testable
  here — swap the sampler strategy and re-score (ties into tasks #8/#9).
