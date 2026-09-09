#!/usr/bin/env python3
"""Production-runtime parity gate for a raw YOLO ONNX release.

The ONNX side loads the AWS detector implementation and exercises its real
single-image decode, quality gate, letterbox, raw-output parser, confidence
filter, NMS, reverse-letterbox transform, and five-field API adapter. It is
compared with sealed held-out Ultralytics ``.pt`` predictions and human truth
produced by ``evaluate.py``.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import os
import platform
import re
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from .common import (
        CAPTURE_MODES, DETECTION_ASSESSMENTS, DETECTION_DAMAGE_TYPES,
        DETECTION_IMAGE_QUALITIES, DETECTION_OUTPUT_FIELDS,
        DETECTION_SIZES, PipelineError, detection_api_contract, normalise_box,
        sealed, sha256_file,
    )
    from .evaluate import box_iou
except ImportError:
    from common import (  # type: ignore
        CAPTURE_MODES, DETECTION_ASSESSMENTS, DETECTION_DAMAGE_TYPES,
        DETECTION_IMAGE_QUALITIES, DETECTION_OUTPUT_FIELDS,
        DETECTION_SIZES, PipelineError, detection_api_contract, normalise_box,
        sealed, sha256_file,
    )
    from evaluate import box_iou  # type: ignore


MIN_SAFE_MATCH_IOU = 0.95
MAX_SAFE_CONFIDENCE_DELTA = 0.05
DEFAULT_MATCH_IOU = 0.98
DEFAULT_CONFIDENCE_DELTA = 0.03
MAX_DECODED_PIXELS = 12_000_000
LAMBDA_BASE_IMAGE_DIGEST = (
    "sha256:ab6df78b68b50723c93741bb7f9ea9f68c7cf3433359b33e4f37db5266567f9c")


def detection_contract() -> dict[str, Any]:
    """Return the application contract sealed into parity and model receipts."""
    return detection_api_contract()


def expected_runtime_dependency_versions() -> dict[str, str]:
    requirements = (
        Path(__file__).resolve().parents[2]
        / "infra/aws-yolo/service/requirements.txt"
    )
    expected_names = {"numpy", "onnxruntime", "Pillow"}
    versions: dict[str, str] = {}
    try:
        lines = requirements.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise PipelineError(f"cannot read AWS runtime requirements: {error}") from error
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^=\s]+)", line)
        if (not match or match.group(1) not in expected_names
                or match.group(1) in versions):
            raise PipelineError(
                "AWS runtime requirements must contain only exact runtime pins")
        versions[match.group(1)] = match.group(2)
    if set(versions) != expected_names:
        raise PipelineError("AWS runtime dependency pins are incomplete")
    return versions


def runtime_dependency_versions() -> dict[str, str]:
    expected = expected_runtime_dependency_versions()
    versions = {}
    for distribution, required in expected.items():
        try:
            versions[distribution] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError as error:
            raise PipelineError(f"runtime parity dependency is missing: {distribution}") from error
        if versions[distribution] != required:
            raise PipelineError(
                f"runtime parity requires {distribution}=={required}, "
                f"found {versions[distribution]}")
    return versions


def _lambda_architecture(machine: str) -> str:
    normalized = machine.strip().lower()
    if normalized in {"x86_64", "amd64"}:
        return "x86_64"
    if normalized in {"aarch64", "arm64"}:
        return "arm64"
    raise PipelineError(f"unsupported Lambda release architecture: {machine}")


def runtime_execution_environment() -> dict[str, str]:
    try:
        import onnxruntime as ort
    except ImportError as error:
        raise PipelineError("ONNX Runtime is missing from the release environment") from error
    execution = {
        "python_major_minor": f"{sys.version_info.major}.{sys.version_info.minor}",
        "python_implementation": platform.python_implementation(),
        "system": platform.system(),
        "lambda_architecture": _lambda_architecture(platform.machine()),
        "onnx_provider": "CPUExecutionProvider",
        "lambda_base_image_digest": os.environ.get(
            "POTHOLE_LAMBDA_BASE_IMAGE_DIGEST", ""),
    }
    if execution["python_major_minor"] != "3.12":
        raise PipelineError("release parity must run with the Lambda Python 3.12 runtime")
    if execution["python_implementation"] != "CPython" or execution["system"] != "Linux":
        raise PipelineError("release parity must run in a Linux CPython Lambda-compatible image")
    if "CPUExecutionProvider" not in ort.get_available_providers():
        raise PipelineError("release parity requires ONNX Runtime CPUExecutionProvider")
    if execution["lambda_base_image_digest"] != LAMBDA_BASE_IMAGE_DIGEST:
        raise PipelineError("release parity is not running on the pinned Lambda base image")
    return execution


def _prediction_list(row: Mapping[str, Any], threshold: float,
                     where: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    raw_predictions = row.get("predictions", [])
    if not isinstance(raw_predictions, list):
        raise PipelineError(f"{where}: predictions must be an array")
    for index, prediction in enumerate(raw_predictions):
        if not isinstance(prediction, dict):
            raise PipelineError(f"{where}: prediction {index} is not an object")
        try:
            confidence = float(prediction["confidence"])
        except (KeyError, TypeError, ValueError) as error:
            raise PipelineError(f"{where}: malformed confidence") from error
        if not 0.0 <= confidence <= 1.0:
            raise PipelineError(f"{where}: confidence is outside [0,1]")
        box = normalise_box(prediction.get("box"), f"{where}.predictions[{index}].box")
        if confidence >= threshold:
            result.append({"box": box, "confidence": confidence})
    result.sort(key=lambda item: item["confidence"], reverse=True)
    return result


def validate_runtime_verdict(value: Any, where: str) -> dict[str, Any]:
    """Validate the exact schema-v4 response emitted by the AWS adapter."""
    if not isinstance(value, dict) or set(value) != set(DETECTION_OUTPUT_FIELDS):
        raise PipelineError(
            f"{where}: runtime verdict must contain exactly "
            + ", ".join(DETECTION_OUTPUT_FIELDS))
    quality = value.get("image_quality")
    assessment = value.get("assessment")
    damage_type = value.get("damage_type")
    size = value.get("size")
    description = value.get("description")
    if quality not in DETECTION_IMAGE_QUALITIES:
        raise PipelineError(f"{where}: image_quality is invalid")
    if assessment not in DETECTION_ASSESSMENTS:
        raise PipelineError(f"{where}: assessment is invalid")
    if damage_type is not None and damage_type not in DETECTION_DAMAGE_TYPES:
        raise PipelineError(f"{where}: damage_type is invalid")
    if size is not None and size not in DETECTION_SIZES:
        raise PipelineError(f"{where}: size is invalid")
    if not isinstance(description, str) or not description.strip():
        raise PipelineError(f"{where}: description must be non-empty")
    if quality == "rejected" and (
            assessment != "undamaged" or damage_type is not None or size is not None):
        raise PipelineError(f"{where}: rejected image has a contradictory damage verdict")
    if quality == "acceptable" and assessment == "damaged" and damage_type is None:
        raise PipelineError(f"{where}: damaged assessment requires damage_type")
    if assessment == "undamaged" and (damage_type is not None or size is not None):
        raise PipelineError(f"{where}: undamaged assessment requires null type and size")
    return {field: value[field] for field in DETECTION_OUTPUT_FIELDS}


def compare_prediction_sets(
    reference_rows: Sequence[Mapping[str, Any]],
    runtime_rows: Sequence[Mapping[str, Any]],
    *,
    confidence_threshold: float,
    minimum_match_iou: float = DEFAULT_MATCH_IOU,
    maximum_confidence_delta: float = DEFAULT_CONFIDENCE_DELTA,
    require_canonical_verdict: bool = False,
) -> dict[str, Any]:
    """Compare post-NMS detections and the production five-field decision."""
    if not 0.0 < confidence_threshold <= 1.0:
        raise PipelineError("parity confidence threshold must be in (0,1]")
    if not MIN_SAFE_MATCH_IOU <= minimum_match_iou <= 1.0:
        raise PipelineError(
            f"parity minimum IoU may not be lower than {MIN_SAFE_MATCH_IOU}")
    if not 0.0 <= maximum_confidence_delta <= MAX_SAFE_CONFIDENCE_DELTA:
        raise PipelineError(
            f"parity confidence tolerance may not exceed {MAX_SAFE_CONFIDENCE_DELTA}")
    reference_by_id: dict[str, Mapping[str, Any]] = {}
    runtime_by_id: dict[str, Mapping[str, Any]] = {}
    for name, source, target in (
        ("reference", reference_rows, reference_by_id),
        ("runtime", runtime_rows, runtime_by_id),
    ):
        for row in source:
            item_id = row.get("source_item_id")
            if not isinstance(item_id, str) or not item_id or item_id in target:
                raise PipelineError(
                    f"{name} parity rows need unique source_item_id values")
            target[item_id] = row
    if set(reference_by_id) != set(runtime_by_id):
        missing = sorted(set(reference_by_id) - set(runtime_by_id))
        extra = sorted(set(runtime_by_id) - set(reference_by_id))
        raise PipelineError(
            f"runtime/reference parity record sets differ ({len(missing)} missing, "
            f"{len(extra)} extra)")
    if not reference_by_id:
        raise PipelineError("runtime parity requires held-out test records")

    rows: list[dict[str, Any]] = []
    all_ious: list[float] = []
    all_confidence_deltas: list[float] = []
    violations = 0
    canonical_decision_mismatches = 0
    matched_total = reference_unmatched_total = runtime_unmatched_total = 0
    for item_id in sorted(reference_by_id):
        reference_row = reference_by_id[item_id]
        runtime_row = runtime_by_id[item_id]
        reference_hash = reference_row.get("image_sha256")
        if reference_hash != runtime_row.get("image_sha256"):
            raise PipelineError(f"parity image hash differs for {item_id}")
        references = _prediction_list(
            reference_row, confidence_threshold, f"reference[{item_id}]")
        runtime = _prediction_list(runtime_row, confidence_threshold, f"runtime[{item_id}]")
        unmatched_runtime = set(range(len(runtime)))
        matches: list[dict[str, Any]] = []
        unmatched_reference = 0
        for reference in references:
            possible = [
                (box_iou(reference["box"], runtime[index]["box"]), index)
                for index in unmatched_runtime
            ]
            match_iou, match_index = max(possible, default=(0.0, -1))
            if match_index < 0 or match_iou < minimum_match_iou:
                unmatched_reference += 1
                continue
            unmatched_runtime.remove(match_index)
            confidence_delta = abs(
                reference["confidence"] - runtime[match_index]["confidence"])
            all_ious.append(match_iou)
            all_confidence_deltas.append(confidence_delta)
            matches.append({
                "iou": match_iou,
                "reference_confidence": reference["confidence"],
                "runtime_confidence": runtime[match_index]["confidence"],
                "confidence_delta": confidence_delta,
                "within_tolerance": confidence_delta <= maximum_confidence_delta,
            })
        row_violations = (
            unmatched_reference + len(unmatched_runtime)
            + sum(not match["within_tolerance"] for match in matches)
        )
        canonical_fields: dict[str, Any] = {}
        if require_canonical_verdict:
            truth_boxes = reference_row.get("truth_boxes")
            if not isinstance(truth_boxes, list):
                raise PipelineError(
                    f"reference[{item_id}]: truth_boxes are required for canonical parity")
            capture_mode = runtime_row.get("capture_mode")
            if capture_mode not in CAPTURE_MODES:
                raise PipelineError(f"runtime[{item_id}]: capture_mode is invalid")
            verdict = validate_runtime_verdict(
                runtime_row.get("runtime_verdict"), f"runtime[{item_id}]")
            expected_assessment = "damaged" if truth_boxes else "undamaged"
            canonical_match = (
                verdict["image_quality"] == "acceptable"
                and verdict["assessment"] == expected_assessment
                and (
                    verdict["damage_type"] == "pothole_cavity"
                    if truth_boxes else verdict["damage_type"] is None
                )
            )
            canonical_decision_mismatches += int(not canonical_match)
            row_violations += int(not canonical_match)
            canonical_fields = {
                "capture_mode": capture_mode,
                "expected_assessment": expected_assessment,
                "runtime_verdict": verdict,
                "canonical_verdict_matches_truth": canonical_match,
            }
        violations += row_violations
        matched_total += len(matches)
        reference_unmatched_total += unmatched_reference
        runtime_unmatched_total += len(unmatched_runtime)
        rows.append({
            "source_item_id": item_id,
            "image_sha256": reference_hash,
            "reference_detections": len(references),
            "runtime_detections": len(runtime),
            "matched": len(matches),
            "unmatched_reference": unmatched_reference,
            "unmatched_runtime": len(unmatched_runtime),
            "matches": matches,
            **canonical_fields,
            "gate_passed": row_violations == 0,
        })
    return {
        "records_compared": len(rows),
        "reference_detections": matched_total + reference_unmatched_total,
        "runtime_detections": matched_total + runtime_unmatched_total,
        "matched_detections": matched_total,
        "unmatched_reference_detections": reference_unmatched_total,
        "unmatched_runtime_detections": runtime_unmatched_total,
        "minimum_observed_iou": min(all_ious) if all_ious else None,
        "maximum_observed_confidence_delta": (
            max(all_confidence_deltas) if all_confidence_deltas else None),
        "canonical_decisions_compared": len(rows) if require_canonical_verdict else 0,
        "canonical_decision_mismatches": canonical_decision_mismatches,
        "violations": violations,
        "gate_passed": violations == 0,
        "rows": rows,
    }


def _load_runtime_module(path: Path) -> Any:
    if not path.is_file():
        raise PipelineError(f"production AWS detector module is missing: {path}")
    module_name = "pothole_yolo_production_detector_for_parity"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise PipelineError(f"cannot load production AWS detector module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as error:
        raise PipelineError(f"cannot import production AWS detector module: {error}") from error
    if getattr(module, "YoloOnnxDetector", None) is None:
        raise PipelineError("production module has no YoloOnnxDetector")
    return module


def _analysed_image(runtime: Any, *, width: int, height: int,
                    quality: str, detections: tuple[Any, ...]) -> Any:
    """Instantiate the production adapter's single-image record."""
    return runtime.AnalysedImage(
        width=width, height=height, quality=quality, detections=detections)


def run_production_onnx(
    *,
    model_path: Path,
    dataset_root: Path,
    reference_rows: Sequence[Mapping[str, Any]],
    dataset_records: Sequence[Mapping[str, Any]],
    runtime_module_path: Path,
    confidence_threshold: float,
    nms_iou_threshold: float,
    maximum_detections: int,
) -> list[dict[str, Any]]:
    """Execute held-out images through the actual AWS tensor/postprocess code."""
    try:
        import numpy as np
        import onnxruntime as ort
        from PIL import Image
    except ImportError as error:
        raise PipelineError(
            "runtime parity needs numpy, Pillow and onnxruntime from requirements-train.txt") from error
    runtime = _load_runtime_module(runtime_module_path)
    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    session = ort.InferenceSession(
        str(model_path), sess_options=options, providers=["CPUExecutionProvider"])
    inputs = session.get_inputs()
    outputs = session.get_outputs()
    if len(inputs) != 1 or len(outputs) != 1 or inputs[0].shape != [1, 3, 640, 640]:
        raise PipelineError("parity requires one static [1,3,640,640] input and one output")
    detector = object.__new__(runtime.YoloOnnxDetector)
    detector.np = np
    detector.Image = Image
    detector.input_width = 640
    detector.input_height = 640
    detector.confidence_threshold = confidence_threshold
    detector.nms_iou_threshold = nms_iou_threshold
    detector.max_detections = maximum_detections
    record_by_id = {
        record.get("source_item_id"): record for record in dataset_records
        if record.get("split") == "test"
    }
    if len(record_by_id) != sum(record.get("split") == "test" for record in dataset_records):
        raise PipelineError("dataset test records need unique source_item_id values")
    reference_ids = {row.get("source_item_id") for row in reference_rows}
    if reference_ids != set(record_by_id):
        raise PipelineError("sealed PT predictions do not cover every held-out test record")
    rows: list[dict[str, Any]] = []
    for reference in reference_rows:
        item_id = reference["source_item_id"]
        record = record_by_id[item_id]
        capture_mode = record.get("capture_mode")
        if capture_mode not in CAPTURE_MODES:
            raise PipelineError(f"dataset capture mode is invalid for {item_id}")
        image_path = dataset_root / record["image"]
        if not image_path.is_file() or sha256_file(image_path) != reference.get("image_sha256"):
            raise PipelineError(f"held-out image bytes do not match manifest for {item_id}")
        if reference.get("truth_boxes") != record.get("boxes"):
            raise PipelineError(f"sealed test truth boxes differ from dataset for {item_id}")
        try:
            image = runtime.decode_image_bytes(
                image_path.read_bytes(), max_decoded_pixels=MAX_DECODED_PIXELS)
            quality = runtime.image_quality(image, np)
            if quality == "rejected":
                detections: tuple[Any, ...] = ()
            elif quality == "acceptable":
                tensor, ratio, pad_x, pad_y = detector._tensor(image)
                raw_outputs = session.run(None, {inputs[0].name: tensor})
                if len(raw_outputs) != 1:
                    raise PipelineError("ONNX parity run returned more than one output")
                detections = detector._postprocess(
                    raw_outputs[0], width=image.width, height=image.height,
                    ratio=ratio, pad_x=pad_x, pad_y=pad_y)
            else:
                raise PipelineError(f"production quality gate returned {quality!r}")
            verdict = runtime.canonical_verdict(
                _analysed_image(
                    runtime, width=image.width, height=image.height,
                    quality=quality, detections=tuple(detections)),
                confidence_threshold=confidence_threshold,
                language="en",
            )
        except PipelineError:
            raise
        except Exception as error:
            raise PipelineError(f"production runtime failed on {item_id}: {error}") from error
        predictions = []
        for detection in detections:
            width = (detection.x2 - detection.x1) / image.width
            height = (detection.y2 - detection.y1) / image.height
            box = normalise_box([
                (detection.x1 + detection.x2) / (2 * image.width),
                (detection.y1 + detection.y2) / (2 * image.height),
                width,
                height,
            ], f"runtime output {item_id}")
            predictions.append({"box": list(box), "confidence": detection.confidence})
        rows.append({
            "source_item_id": item_id,
            "image_sha256": reference["image_sha256"],
            "capture_mode": capture_mode,
            "runtime_verdict": validate_runtime_verdict(
                verdict, f"production verdict {item_id}"),
            "predictions": predictions,
        })
    return rows


def build_parity_receipt(
    *,
    onnx_path: Path,
    weights_sha256: str,
    dataset_manifest_sha256: str,
    reference_prediction_sha256: str,
    runtime_module_path: Path,
    reference_rows: Sequence[Mapping[str, Any]],
    runtime_rows: Sequence[Mapping[str, Any]],
    runtime_environment: Mapping[str, str],
    runtime_execution: Mapping[str, str],
    confidence_threshold: float,
    nms_iou_threshold: float,
    maximum_detections: int,
    minimum_match_iou: float = DEFAULT_MATCH_IOU,
    maximum_confidence_delta: float = DEFAULT_CONFIDENCE_DELTA,
) -> dict[str, Any]:
    expected_dependencies = {"numpy", "onnxruntime", "Pillow"}
    if set(runtime_environment) != expected_dependencies or not all(
        isinstance(value, str) and value for value in runtime_environment.values()
    ):
        raise PipelineError(
            "runtime parity environment must identify numpy, onnxruntime and Pillow")
    if runtime_execution != {
        "python_major_minor": "3.12",
        "python_implementation": "CPython",
        "system": "Linux",
        "lambda_architecture": runtime_execution.get("lambda_architecture"),
        "onnx_provider": "CPUExecutionProvider",
        "lambda_base_image_digest": LAMBDA_BASE_IMAGE_DIGEST,
    } or runtime_execution.get("lambda_architecture") not in {"x86_64", "arm64"}:
        raise PipelineError("runtime parity did not run in a supported Lambda environment")
    metrics = compare_prediction_sets(
        reference_rows, runtime_rows,
        confidence_threshold=confidence_threshold,
        minimum_match_iou=minimum_match_iou,
        maximum_confidence_delta=maximum_confidence_delta,
        require_canonical_verdict=True,
    )
    return sealed({
        "schema_version": "pothole-yolo-runtime-parity-v1",
        "task": "pothole_detection",
        "split": "test",
        "detection_contract": detection_contract(),
        "onnx_sha256": sha256_file(onnx_path),
        "weights_sha256": weights_sha256,
        "dataset_manifest_sha256": dataset_manifest_sha256,
        "reference_prediction_sha256": reference_prediction_sha256,
        "runtime_module_sha256": sha256_file(runtime_module_path),
        "runtime_module": "infra/aws-yolo/service/detector.py",
        "runtime_environment": dict(runtime_environment),
        "runtime_execution": dict(runtime_execution),
        "runtime_path_exercised": [
            "single-image Pillow decode and EXIF transpose",
            "production acceptable/rejected image-quality gate",
            "Pillow bilinear 640px letterbox",
            "ONNX Runtime CPU raw output",
            "confidence filter",
            "class-agnostic NMS",
            "reverse letterbox",
            "minimum two-pixel box filter",
            "maximum detection limit",
            "production five-field schema-v4 verdict",
        ],
        "decision": {
            "confidence_threshold": confidence_threshold,
            "nms_iou_threshold": nms_iou_threshold,
            "maximum_detections": maximum_detections,
        },
        "runtime_limits": {
            "maximum_decoded_pixels": MAX_DECODED_PIXELS,
            "input_images_per_request": 1,
        },
        "tolerances": {
            "minimum_matched_box_iou": minimum_match_iou,
            "maximum_absolute_confidence_delta": maximum_confidence_delta,
            "maximum_unmatched_reference_detections": 0,
            "maximum_unmatched_runtime_detections": 0,
            "maximum_canonical_decision_mismatches": 0,
        },
        "metrics": metrics,
        "records_compared": metrics["records_compared"],
        "gate_passed": metrics["gate_passed"],
    }, field="parity_receipt_sha256")
