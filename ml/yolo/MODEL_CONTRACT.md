# AWS inference artifact contract

The detector bundle contains `model.onnx`, `model-manifest.json`, and
`runtime-parity.json`. The service rejects the bundle unless all of these are true:

- `schema_version` is `pothole-yolo-model-manifest-v1`, `task` is
  `pothole_detection`, and the only model class is `0 = pothole`;
- the application contract is prompt `road-damage-v5`, schema version 4, exactly one
  input image, and capture mode `manual` or `drive`;
- the application response contains exactly `image_quality`, `assessment`,
  `damage_type`, `size`, and `description`;
- `image_quality` is `acceptable` or `rejected`, and `assessment` is `damaged` or
  `undamaged`; an undamaged or rejected result has null `damage_type` and `size`;
- a positive result from this one-class model uses `damage_type=pothole_cavity`; the
  model does not invent patch, breakup, rut, physical-size, or repair claims;
- `image_size` is 640 and the export is fixed-shape raw ONNX with
  `dynamic=false`, `simplify=true`, and `nms=false`;
- the ONNX SHA-256 and byte count match the manifest, and every dataset, training,
  validation-threshold, sealed-test, runtime-module, and parity hash agrees;
- both validation and test accuracy gates passed and are recomputed from the exact
  sealed prediction rows during release;
- the sealed validation and test reports include fixed-threshold box/image confusion
  metrics, COCO-style 101-point `map_50` and `map_50_95`, and 95% percentile intervals
  from 2,000 complete-`leakage_group` bootstrap resamples;
- parity ran on Linux CPython 3.12 with the target Lambda architecture and CPU
  execution provider, using the exact pinned NumPy, ONNX Runtime, Pillow, and Lambda
  base-image versions;
- the configured confidence threshold, maximum decoded pixels, and one-image limit
  equal the values in the sealed parity receipt.

Input to the ONNX graph is one RGB float32 tensor `[1,3,640,640]`, normalized to
`[0,1]` after an aspect-preserving letterbox with fill `(114,114,114)`. Output is the
raw Ultralytics tensor. Each prediction has centre x/y, width/height in letterboxed
pixels, and a class-0 score. The adapter applies the manifest confidence threshold,
class-agnostic NMS, and the reverse-letterbox transform.

Release parity replays every held-out test image through the deployed detector module:
bounded decode and EXIF transpose, the `acceptable`/`rejected` quality gate, letterbox,
ONNX CPU inference, confidence filtering, NMS, reverse letterbox, two-pixel box filter,
maximum-detection limit, and final five-field verdict. It requires one-to-one detection
agreement with sealed `.pt` predictions: matched IoU at least 0.98, absolute confidence
drift at most 0.03, no unmatched detections, and `damaged`/`undamaged` agreement with
human truth for every acceptable image.

COCO-style AP is calculated from all stored candidates at the fixed 0.01 candidate
floor and a maximum of 100 detections per image. `map_50_95` averages IoUs 0.50 through
0.95 in steps of 0.05. It is independent of the selected deployment threshold, unlike
the reported box/image precision, recall, and F1. A confidence interval is unavailable
rather than fabricated when fewer than two independent leakage groups exist. No metric
in a manifest is an accuracy claim unless it is backed by the sealed held-out prediction
and evaluation receipts; the repository currently has no trained release artifact.

There is no coordinate or vertical-position suppression. A real cavity remains a
positive when it is at the road edge, and the ground-truth policy covers asphalt,
concrete, gravel, dirt, and mud roads. Damage wholly outside the road or track boundary
is a negative. An `UnsurfacedRoad` dataset label describes material, not damage, and is
never automatically used as a negative or positive.

Both manual and Drive Mode submit one selected image. There is no multi-frame or
temporal-consistency contract in the YOLO adapter. Authentication, request IDs,
payload/concurrency limits, request-count caps, and spend caps belong to the AWS
gateway and deployment.
