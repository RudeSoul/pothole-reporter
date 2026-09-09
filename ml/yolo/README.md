# Pothole-only YOLO training pipeline

This directory prepares, trains, evaluates, and exports the one-class detector used by
the AWS fallback gateway. It deliberately does **not** turn an image-level label or a
broad road-anomaly box into pothole ground truth.

## Current status (5 September 2026)

There is no trained YOLO weight or ONNX release artifact in this repository yet.

- `eval/labels.json` references 18 locally available images. Six are explicitly
  owner-labelled, owner-licensed positives; none has a bounding box. The other 12 are
  assistant-labelled, disputed, or unlabelled and are excluded. There is therefore no
  owner-verified negative set and no detection-ready positive set.
- `photos/` contains four images with no training manifest or labels, so it is excluded.
- The referenced public source is
  [RAD (Road Anomaly Detection) v3](https://www.kaggle.com/datasets/rohitsuresh15/radroad-anomaly-detection).
  Kaggle's current v3 metadata reports MIT, 7,852,662,180 bytes, and a last update of
  28 November 2023. The image annotation subset previously audited by this project has
  8,394 image/label pairs, 6,463 `RoadDamages` boxes, 499 `SpeedBump` boxes, and 341
  pure-speed-bump frames.
- RAD's own description says `RoadDamages` combines potholes, cracks, protrusions, and
  manholes. It is not a pothole class. The pipeline uses pure speed-bump frames as safe
  hard negatives, but includes a RAD positive only after a human draws pothole-only
  boxes and certifies a complete full-frame review.
- RAD's published train/valid/test folders leak source video and duplicate frame
  variants. `prepare_dataset.py` ignores those split names, groups every frame by source
  video, coalesces exact duplicate pixels, and applies one deterministic 80/10/10 split.

These are data blockers, not software blockers. The checked-in commands are runnable,
but training is supposed to fail closed until the independent-image and capture-group
release floors below are met.

## YOLO licensing decision

This implementation currently pins the Ultralytics training/export stack and uses an
Ultralytics base checkpoint. Ultralytics states that its code and trained/fine-tuned
models are AGPL-3.0 by default, while a proprietary, private, SaaS, or commercial
deployment requires its Enterprise licence. Before training a production model, either
confirm that the whole deployment will comply with AGPL-3.0 or obtain the appropriate
commercial licence; this repository does not make that legal choice for the operator.
See the vendor's [current licence guidance](https://www.ultralytics.com/license).

Before running any pipeline command below, create the pinned environment:

```bash
python3 -m venv ml/yolo/.venv
ml/yolo/.venv/bin/pip install -r ml/yolo/requirements-train.txt
```

## Data contracts

The model has one and only one training class:

```text
class id 0 = pothole
```

Ground truth follows the application contract, not a paved-road or centre-of-frame
shortcut. A visible pothole cavity can be positive on asphalt, concrete, gravel, dirt,
or mud, including at the road edge. Damage wholly outside the road or track boundary
is negative. RAD's `UnsurfacedRoad` class describes road material, so the importer
never turns it into either pothole truth or a hard negative without a human box audit.
Each prepared record is one image and records only its `manual` or `drive` capture
mode; there are no image-role or temporal-consistency labels.

An app export (`road-damage-dataset-*.zip`) contains `labels.json` plus images. Its
positive subtype labels are useful, but an object detector also needs box coordinates.
Complete an audit based on
[`audits/owned-box-audit.example.json`](audits/owned-box-audit.example.json). App ZIPs
do not currently carry an image-rights field; pair them with
[`audits/owned-training-rights.example.json`](audits/owned-training-rights.example.json).
The legacy `eval/labels.json` entries already carry `licence: owner` per image.
The app now exports its central `server_pothole_id` when available. The pipeline uses
that first, then a `drive_id` or `capture_session_id`, and also coalesces owned captures
within 25 metres across exports. Raw identifiers and coordinates are not copied to the
prepared manifest. If every identifier and location is absent, all such owned images
share one conservative leakage group; filenames are never treated as independence.

For a ZIP, the source identity in the audit/rights files is the SHA-256 of the complete
ZIP. For an extracted directory, it is a domain-separated, length-prefixed SHA-256 over
the exact `labels.json` bytes and every referenced image path and image byte, sorted by
path. It is deliberately **not** just the `labels.json` hash, so replacing an image
invalidates the rights and annotation binding. Print the value used by the pipeline:

```bash
ml/yolo/.venv/bin/python -c 'from pathlib import Path; from ml.yolo.prepare_dataset import owned_source_digest; print(owned_source_digest(Path("/secure/export-or.zip")))'
```

For RAD, draw boxes from the full selected frame and use
[`audits/rad-box-audit.example.json`](audits/rad-box-audit.example.json). A reviewed
frame may contain zero boxes and become a negative. `complete_frame_review: true`
means the reviewer checked the entire image, not just the original RAD rectangles.

Do not use model-generated pseudo-boxes in validation or test. If pseudo-labelling is
experimented with later, keep it in training, label its provenance separately, and do
not count it as human ground truth.

## Reproduce the dataset

The parity-sensitive execution stack is pinned, including `torch==2.8.0`,
`torchvision==0.23.0`, `opencv-python==4.10.0.84`, `numpy==1.26.4`,
`onnxruntime==1.19.2`, and `Pillow==11.3.0`. Training, prediction, and release receipts
bind those installed engine versions; a mismatch fails closed.

Check that Kaggle still serves v3 and reports the expected licence before downloading:

```bash
curl -fsSL \
  https://www.kaggle.com/api/v1/datasets/view/rohitsuresh15/radroad-anomaly-detection \
  > ml/yolo/.rad-data-metadata.json
python3 - <<'PY'
import json
d = json.load(open("ml/yolo/.rad-data-metadata.json"))
assert d["currentVersionNumber"] == 3
assert d["licenseName"] == "MIT"
print(d["ref"], d["currentVersionNumber"], d["licenseName"], d["totalBytes"])
PY
```

With Kaggle authentication configured, download the current v3 archive:

```bash
mkdir -p ml/yolo/.rad-data
kaggle datasets download rohitsuresh15/radroad-anomaly-detection \
  --path ml/yolo/.rad-data --unzip
```

Compute the domain-separated identity of exactly the `data.yaml`, images, and labels
the importer consumes. Record it in the RAD audit's `source_sha256` and/or pass it
explicitly during preparation:

```bash
ml/yolo/.venv/bin/python -c 'from pathlib import Path; from ml.yolo.prepare_dataset import rad_source_digest; print(rad_source_digest(Path("ml/yolo/.rad-data")))'
```

`prepare_dataset.py` verifies the exact class schema and, unless
`--allow-incomplete-rad` is used for a fixture, the audited v3 image/label counts. It
consolidates Roboflow frame variants and refuses a changed source.

Prepare a fresh training directory after the box audits are complete:

```bash
ml/yolo/.venv/bin/python ml/yolo/prepare_dataset.py \
  --owned /secure/road-damage-dataset-123.zip \
  --owned-audit /secure/owned-box-audit.json \
  --owned-rights /secure/owned-training-rights.json \
  --rad-root ml/yolo/.rad-data \
  --rad-audit /secure/rad-box-audit.json \
  --rad-source-sha256 <exact-consumed-tree-sha256> \
  --output ml/yolo/build/pothole-v1
```

For a non-training audit of what is currently available in this repository:

```bash
ml/yolo/.venv/bin/python ml/yolo/prepare_dataset.py \
  --owned eval --allow-incomplete \
  --output /tmp/pothole-yolo-data-audit
```

The generated `manifest.json` hashes every selected image, records the capture mode and
leakage group, and says `release_ready: false` with exact blockers if
`--allow-incomplete` was used.

Release readiness has an explicit minimum per class and per independent leakage group:
training needs 200 positive and 200 negative images across at least 20 positive and 20
negative groups; validation and test each need 50 positive and 50 negative images
across at least 10 positive and 10 negative groups. These are conservative safety
floors, not proof that the dataset is representative or that the model is good. Before
training, evaluation, and release, the pipeline re-hashes every prepared image, rebuilds
every label from the sealed boxes, rejects extra files/symlinks, checks every deterministic
group split, and requires the exact sealed, relocatable `dataset.yaml`. Every selected
image is fully decoded under the 12-million-pixel limit before it can count toward a
release floor.

## Train and evaluate

Use an Ultralytics **detection** checkpoint, not a classification checkpoint. Keep its
bytes and SHA-256 so the training receipt can pin the base model:

```bash
mkdir -p ml/yolo/weights
cd ml/yolo/weights
../.venv/bin/python -c "from ultralytics import YOLO; YOLO('yolo11n.pt')"
cd ../../..
shasum -a 256 ml/yolo/weights/yolo11n.pt
```

Fine-tune at the AWS runtime's fixed size:

```bash
ml/yolo/.venv/bin/python ml/yolo/train.py \
  --dataset ml/yolo/build/pothole-v1 \
  --base-model ml/yolo/weights/yolo11n.pt \
  --base-model-sha256 <sha256-from-above> \
  --project ml/yolo/runs --name pothole-yolo-v1 \
  --epochs 100 --batch 16 --device 0
```

Training uses `seed=20260905`, deterministic algorithms, 640 px inputs, the sealed
dataset, and a single class. It writes `training-receipt.json` next to `best.pt`.

Generate low-confidence predictions for validation, select a threshold only on
validation, and apply that frozen threshold once to test:

```bash
ml/yolo/.venv/bin/python ml/yolo/evaluate.py predict \
  --dataset ml/yolo/build/pothole-v1 \
  --weights ml/yolo/runs/pothole-yolo-v1/weights/best.pt \
  --split validation --device cpu \
  --output ml/yolo/runs/pothole-yolo-v1/validation-predictions.json

ml/yolo/.venv/bin/python ml/yolo/evaluate.py select-threshold \
  --predictions ml/yolo/runs/pothole-yolo-v1/validation-predictions.json \
  --min-box-recall 0.90 --min-positive-image-recall 0.90 \
  --max-negative-image-fp-rate 0.05 \
  --output ml/yolo/runs/pothole-yolo-v1/threshold.json

ml/yolo/.venv/bin/python ml/yolo/evaluate.py predict \
  --dataset ml/yolo/build/pothole-v1 \
  --weights ml/yolo/runs/pothole-yolo-v1/weights/best.pt \
  --split test --device cpu \
  --output ml/yolo/runs/pothole-yolo-v1/test-predictions.json

ml/yolo/.venv/bin/python ml/yolo/evaluate.py score-test \
  --predictions ml/yolo/runs/pothole-yolo-v1/test-predictions.json \
  --threshold ml/yolo/runs/pothole-yolo-v1/threshold.json \
  --output ml/yolo/runs/pothole-yolo-v1/test-evaluation.json
```

The enforced gate requires at least 90% box recall, at least 90% positive-image recall,
and at most 5% false-positive negative images at IoU 0.5. These floors cannot be
weakened by CLI flags and are not a claim that the unavailable model has met them.
Receipts also record the full image-level confusion matrix, precision, recall, F1, and
specificity, plus box-level precision, recall, and F1; an all-negative detector cannot
pass the positive-recall gates.
Candidate generation and
threshold selection use 0.01 as the minimum, matching the Terraform and Lambda
configuration contract; a lower selected threshold cannot be released.
Prediction generation also runs every validation/test image through the deployed
quality gate and refuses a `rejected` held-out image. This prevents black, corrupt, or
hopelessly blurred negatives from making the false-positive rate look artificially good.
Ultralytics prediction is also forced to `rect=False`, so held-out `.pt` evaluation uses
the same fixed square 640x640 letterbox shape as the exported ONNX production runtime.

## Export the AWS artifact

Only a passed validation gate and passed sealed test can produce a release bundle.
Build the release gate on the same Lambda Python base and CPU architecture used by
inference (the example is the Terraform default `x86_64`):

```bash
docker build --platform linux/amd64 \
  -f ml/yolo/Dockerfile.release -t pothole-yolo-release:py312 ml/yolo

docker run --rm --platform linux/amd64 -v "$PWD:/workspace" \
  pothole-yolo-release:py312 \
  --weights ml/yolo/runs/pothole-yolo-v1/weights/best.pt \
  --dataset-manifest ml/yolo/build/pothole-v1/manifest.json \
  --training-receipt ml/yolo/runs/pothole-yolo-v1/training-receipt.json \
  --threshold-receipt ml/yolo/runs/pothole-yolo-v1/threshold.json \
  --test-evaluation ml/yolo/runs/pothole-yolo-v1/test-evaluation.json \
  --validation-predictions ml/yolo/runs/pothole-yolo-v1/validation-predictions.json \
  --test-predictions ml/yolo/runs/pothole-yolo-v1/test-predictions.json \
  --model-version pothole-yolo-v1 \
  --output ml/yolo/artifacts/pothole-yolo-v1
```

The build context is the allow-listed `ml/yolo` directory, so application data,
credentials, Git history, and certificates are not sent to the Docker builder. The
release and inference Dockerfiles pin the same reviewed multi-architecture Lambda
Python 3.12 base-image digest; update both together after an intentional review.

For Terraform `lambda_architecture="arm64"`, build and run with
`--platform linux/arm64`. The release command refuses macOS, non-Python-3.12, an
unsupported architecture, a missing CPU execution provider, or dependency versions
that differ from `infra/aws-yolo/service/requirements.txt`.

The eventual deployable files are:

```text
ml/yolo/artifacts/pothole-yolo-v1/model.onnx
ml/yolo/artifacts/pothole-yolo-v1/model-manifest.json
ml/yolo/artifacts/pothole-yolo-v1/runtime-parity.json
```

The exporter forces `imgsz=640`, batch 1, `dynamic=False`, `simplify=True`, ONNX
opset 17, and `nms=False`. It validates a static `[1,3,640,640]` input, a raw
five-channel single-class output, and the absence of ONNX `NonMaxSuppression`.

Before export, the command binds every validation and test row to the exact sealed
split and recomputes threshold selection and both metric gates from the prediction
rows. Before the release directory is created, the exporter loads the production
`infra/aws-yolo/service/detector.py` by file path and replays **every sealed test
record** through its 12-million-pixel decode gate, EXIF transpose, production
`acceptable`/`rejected` image-quality gate, Pillow bilinear letterbox, ONNX Runtime CPU
session, raw tensor parser, confidence filter, class-agnostic NMS,
reverse-letterbox transform, two-pixel box filter, maximum detection limit, and final
five-field API verdict. It compares detections with the sealed `.pt` test predictions
and requires `damaged`/`undamaged` to agree with held-out human truth. There is no
vertical-position gate, so genuine road-edge damage is not suppressed.
Every detection must match one-to-one with IoU at least 0.98 and absolute confidence
drift at most 0.03; no unmatched detection is allowed. The command refuses release if
parity fails and records the production module hash, model/data/weights/prediction
hashes, exact runtime dependency versions, the 12-million-pixel limit, per-record
comparisons, capture mode, canonical decision, tolerances, and gate result in
`runtime-parity.json`.
It also seals Linux, CPython 3.12, target Lambda architecture, CPU execution provider,
and exact AWS dependency versions; the Lambda rejects a bundle made for a different
runtime target.
The tolerances cannot be loosened below 0.95 IoU or above 0.05 confidence drift in code.

The AWS gateway must load all three files and fail closed if the manifest
seal/hash, task, image size, class map, ONNX hash, runtime parity receipt, or
production-runtime code hash is wrong. The raw tensor is
`[center_x, center_y, width, height, class_0_score]`; confidence filtering and
class-agnostic NMS happen in the gateway. A retained class-0 detection maps to the
server's five-field schema-v4 result: `image_quality=acceptable`,
`assessment=damaged`, `damage_type=pothole_cavity`, `size=null`, plus a factual
description. No retained box maps to `assessment=undamaged`; rejected image quality
also returns undamaged with null type and size. Both manual and Drive Mode supply
exactly one selected image. Release parity seals prompt `road-damage-v5`, schema 4,
the exact five output fields, both capture modes, the one-image limit, and the
decoded-pixel ceiling so AWS configuration drift fails closed.

Run the pipeline tests in the same pinned environment with:

```bash
ml/yolo/.venv/bin/python -m unittest -v ml.yolo.tests.test_pipeline
```
