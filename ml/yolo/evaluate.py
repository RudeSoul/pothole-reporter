#!/usr/bin/env python3
"""Generate sealed predictions, select a validation threshold, and score test."""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:
    from .common import (
        MAX_DECODED_PIXELS, MIN_DETECTION_CONFIDENCE, PipelineError,
        detection_api_contract, installed_training_engine_versions, load_json,
        normalise_box, sealed, sha256_file, validate_prepared_dataset,
        verify_seal, write_json,
    )
except ImportError:
    from common import (  # type: ignore
        MAX_DECODED_PIXELS, MIN_DETECTION_CONFIDENCE, PipelineError,
        detection_api_contract, installed_training_engine_versions, load_json,
        normalise_box, sealed, sha256_file, validate_prepared_dataset,
        verify_seal, write_json,
    )


MIN_RELEASE_IOU_THRESHOLD = 0.5
MIN_RELEASE_BOX_RECALL = 0.90
MIN_RELEASE_POSITIVE_IMAGE_RECALL = 0.90
MAX_RELEASE_NEGATIVE_IMAGE_FP_RATE = 0.05


def quality_gate_contract(runtime_path: Path | None = None) -> dict[str, Any]:
    path = runtime_path or (
        Path(__file__).resolve().parents[2] / "infra/aws-yolo/service/detector.py")
    return {
        "runtime_module_sha256": sha256_file(path),
        "maximum_decoded_pixels": MAX_DECODED_PIXELS,
        "allowed_held_out_quality": ["acceptable"],
    }


def validate_release_constraints(
    *,
    min_box_recall: float,
    min_positive_image_recall: float,
    max_negative_image_fp_rate: float,
    iou_threshold: float,
) -> None:
    values = (
        min_box_recall, min_positive_image_recall,
        max_negative_image_fp_rate, iou_threshold,
    )
    if not all(isinstance(value, (int, float)) and math.isfinite(float(value))
               for value in values):
        raise PipelineError("release evaluation constraints must be finite numbers")
    if not MIN_RELEASE_BOX_RECALL <= min_box_recall <= 1.0:
        raise PipelineError(
            f"minimum box recall may not be weaker than {MIN_RELEASE_BOX_RECALL}")
    if not MIN_RELEASE_POSITIVE_IMAGE_RECALL <= min_positive_image_recall <= 1.0:
        raise PipelineError(
            "minimum positive-image recall may not be weaker than "
            f"{MIN_RELEASE_POSITIVE_IMAGE_RECALL}")
    if not 0.0 <= max_negative_image_fp_rate <= MAX_RELEASE_NEGATIVE_IMAGE_FP_RATE:
        raise PipelineError(
            "maximum negative-image false-positive rate may not be weaker than "
            f"{MAX_RELEASE_NEGATIVE_IMAGE_FP_RATE}")
    if not MIN_RELEASE_IOU_THRESHOLD <= iou_threshold <= 1.0:
        raise PipelineError(
            f"evaluation IoU threshold may not be lower than {MIN_RELEASE_IOU_THRESHOLD}")


def metrics_pass_release_constraints(
    metrics: Mapping[str, Any], constraints: Mapping[str, Any]
) -> bool:
    try:
        box_recall = float(metrics["box"]["recall"])
        positive_recall = float(metrics["image"]["positive_recall"])
        negative_fp_rate = float(metrics["image"]["negative_false_positive_rate"])
        minimum_box_recall = float(constraints["minimum_box_recall"])
        minimum_positive_recall = float(
            constraints["minimum_positive_image_recall"])
        maximum_negative_fp = float(
            constraints["maximum_negative_image_false_positive_rate"])
    except (KeyError, TypeError, ValueError) as error:
        raise PipelineError("evaluation metrics or constraints are malformed") from error
    observed = (box_recall, positive_recall, negative_fp_rate)
    if not all(math.isfinite(value) and 0.0 <= value <= 1.0 for value in observed):
        raise PipelineError("evaluation metrics are outside [0,1]")
    return (
        box_recall >= minimum_box_recall
        and positive_recall >= minimum_positive_recall
        and negative_fp_rate <= maximum_negative_fp
    )


def _production_quality_runtime() -> tuple[Any, Any, Path]:
    runtime_path = Path(__file__).resolve().parents[2] / "infra/aws-yolo/service/detector.py"
    spec = importlib.util.spec_from_file_location(
        "pothole_yolo_quality_gate_for_evaluation", runtime_path)
    if spec is None or spec.loader is None:
        raise PipelineError(f"cannot load production quality gate: {runtime_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        import numpy as np
    except Exception as error:
        raise PipelineError(f"cannot load production quality gate: {error}") from error
    return module, np, runtime_path


def _xywh_to_edges(box: Sequence[float]) -> tuple[float, float, float, float]:
    x, y, width, height = box
    return x - width / 2, y - height / 2, x + width / 2, y + height / 2


def box_iou(left: Sequence[float], right: Sequence[float]) -> float:
    lx1, ly1, lx2, ly2 = _xywh_to_edges(left)
    rx1, ry1, rx2, ry2 = _xywh_to_edges(right)
    width = max(0.0, min(lx2, rx2) - max(lx1, rx1))
    height = max(0.0, min(ly2, ry2) - max(ly1, ry1))
    intersection = width * height
    left_area = max(0.0, lx2 - lx1) * max(0.0, ly2 - ly1)
    right_area = max(0.0, rx2 - rx1) * max(0.0, ry2 - ry1)
    union = left_area + right_area - intersection
    return intersection / union if union > 0 else 0.0


def score_rows(rows: Iterable[Mapping[str, Any]], threshold: float,
               iou_threshold: float = 0.5) -> dict[str, Any]:
    if not 0.0 <= threshold <= 1.0 or not 0.0 < iou_threshold <= 1.0:
        raise PipelineError("thresholds must be in their probability/IoU ranges")
    box_tp = box_fp = box_fn = 0
    image_tp = image_fp = image_tn = image_fn = 0
    positive_images = negative_images = 0
    count = 0
    for row_index, row in enumerate(rows):
        count += 1
        truths = [normalise_box(box, f"rows[{row_index}].truth_boxes")
                  for box in row.get("truth_boxes", [])]
        predictions = []
        for prediction_index, prediction in enumerate(row.get("predictions", [])):
            if not isinstance(prediction, dict):
                raise PipelineError(f"rows[{row_index}].predictions[{prediction_index}] is invalid")
            try:
                confidence = float(prediction["confidence"])
            except (KeyError, TypeError, ValueError) as error:
                raise PipelineError("prediction confidence is missing or malformed") from error
            if not 0.0 <= confidence <= 1.0:
                raise PipelineError("prediction confidence is outside [0,1]")
            box = normalise_box(prediction.get("box"),
                                f"rows[{row_index}].predictions[{prediction_index}].box")
            if confidence >= threshold:
                predictions.append((confidence, box))
        predictions.sort(key=lambda item: item[0], reverse=True)
        matched_truth: set[int] = set()
        local_tp = 0
        for _, prediction_box in predictions:
            possible = [(box_iou(prediction_box, truth), index)
                        for index, truth in enumerate(truths) if index not in matched_truth]
            best_iou, best_index = max(possible, default=(0.0, -1))
            if best_iou >= iou_threshold:
                matched_truth.add(best_index)
                local_tp += 1
            else:
                box_fp += 1
        box_tp += local_tp
        box_fn += len(truths) - len(matched_truth)
        truth_positive = bool(truths)
        if truth_positive:
            positive_images += 1
            # A box elsewhere in a positive frame is still a localization miss.
            # Count an image hit only when a human pothole box has an IoU match.
            if local_tp > 0:
                image_tp += 1
            else:
                image_fn += 1
        else:
            negative_images += 1
            if predictions:
                image_fp += 1
            else:
                image_tn += 1
    precision = box_tp / (box_tp + box_fp) if box_tp + box_fp else 0.0
    recall = box_tp / (box_tp + box_fn) if box_tp + box_fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    image_precision = image_tp / (image_tp + image_fp) if image_tp + image_fp else 0.0
    image_recall = image_tp / (image_tp + image_fn) if image_tp + image_fn else 0.0
    image_specificity = image_tn / (image_tn + image_fp) if image_tn + image_fp else 0.0
    image_f1 = (
        2 * image_precision * image_recall / (image_precision + image_recall)
        if image_precision + image_recall else 0.0
    )
    return {
        "threshold": threshold,
        "iou_threshold": iou_threshold,
        "images": count,
        "positive_images": positive_images,
        "negative_images": negative_images,
        "box": {
            "true_positive": box_tp,
            "false_positive": box_fp,
            "false_negative": box_fn,
            "precision": precision,
            "recall": recall,
            "f1": f1,
        },
        "image": {
            "true_positive": image_tp,
            "false_positive": image_fp,
            "true_negative": image_tn,
            "false_negative": image_fn,
            "precision": image_precision,
            "recall": image_recall,
            "f1": image_f1,
            "specificity": image_specificity,
            "positive_recall": image_recall,
            "negative_false_positive_rate": image_fp / negative_images if negative_images else 0.0,
        },
    }


def choose_threshold(rows: list[Mapping[str, Any]], min_box_recall: float,
                     min_positive_image_recall: float,
                     max_negative_image_fp_rate: float,
                     iou_threshold: float = 0.5) -> tuple[float, dict[str, Any], bool]:
    validate_release_constraints(
        min_box_recall=min_box_recall,
        min_positive_image_recall=min_positive_image_recall,
        max_negative_image_fp_rate=max_negative_image_fp_rate,
        iou_threshold=iou_threshold,
    )
    if not rows:
        raise PipelineError("cannot select a threshold from an empty validation set")
    if not any(row.get("truth_boxes") for row in rows):
        raise PipelineError("validation set has no positive boxes")
    if not any(not row.get("truth_boxes") for row in rows):
        raise PipelineError("validation set has no negative images")
    scores = {MIN_DETECTION_CONFIDENCE, 1.0}
    for row in rows:
        for prediction in row.get("predictions", []):
            confidence = float(prediction["confidence"])
            if confidence >= MIN_DETECTION_CONFIDENCE:
                scores.add(confidence)
    candidates = [score_rows(rows, value, iou_threshold) for value in sorted(scores)]

    def feasible(metrics: Mapping[str, Any]) -> bool:
        return (metrics["box"]["recall"] >= min_box_recall
                and metrics["image"]["positive_recall"] >= min_positive_image_recall
                and metrics["image"]["negative_false_positive_rate"]
                <= max_negative_image_fp_rate)

    passing = [metrics for metrics in candidates if feasible(metrics)]
    pool = passing if passing else candidates
    # Prefer fewer false positives, then more true positives/F1, then the stricter threshold.
    best = min(pool, key=lambda metrics: (
        metrics["box"]["false_positive"],
        metrics["image"]["false_positive"],
        -metrics["box"]["true_positive"],
        -metrics["box"]["f1"],
        -metrics["threshold"],
    ))
    return float(best["threshold"]), best, bool(passing)


def _load_predictions(path: Path, expected_split: str) -> dict[str, Any]:
    value = load_json(path)
    if not isinstance(value, dict) or value.get("schema_version") != "pothole-yolo-predictions-v1":
        raise PipelineError(f"{path}: unsupported predictions schema")
    verify_seal(value, "prediction_sha256")
    if value.get("split") != expected_split:
        raise PipelineError(f"{path}: expected {expected_split} predictions")
    if not isinstance(value.get("rows"), list):
        raise PipelineError(f"{path}: rows must be an array")
    if value.get("quality_gate") != quality_gate_contract():
        raise PipelineError(f"{path}: production quality-gate receipt is missing or stale")
    if value.get("detection_contract") != detection_api_contract():
        raise PipelineError(f"{path}: detection API contract is missing or stale")
    expected_inference = {
        "minimum_confidence": MIN_DETECTION_CONFIDENCE,
        "nms_iou": 0.7,
        "agnostic_nms": True,
        "max_detections": 100,
        "rect": False,
        "device": "cpu",
    }
    if value.get("inference") != expected_inference:
        raise PipelineError(f"{path}: prediction inference contract is missing or stale")
    environment = value.get("engine_environment")
    if (not isinstance(environment, dict) or not environment
            or not all(isinstance(key, str) and isinstance(version, str) and version
                       for key, version in environment.items())):
        raise PipelineError(f"{path}: prediction engine environment is missing")
    for index, row in enumerate(value["rows"]):
        if not isinstance(row, dict) or row.get("runtime_image_quality") != "acceptable":
            raise PipelineError(
                f"{path}: rows[{index}] is not an acceptable held-out road image")
    return value


def command_predict(args: argparse.Namespace) -> None:
    if str(args.device).strip().lower() != "cpu":
        raise PipelineError("held-out prediction generation is fixed to CPU")
    dataset_root = Path(args.dataset).resolve()
    manifest = load_json(dataset_root / "manifest.json")
    validate_prepared_dataset(dataset_root, manifest)
    weights = Path(args.weights).resolve()
    if not weights.is_file():
        raise PipelineError(f"weights do not exist: {weights}")
    try:
        from ultralytics import YOLO
    except ImportError as error:
        raise PipelineError("Ultralytics is required for prediction") from error
    model = YOLO(str(weights))
    quality_runtime, np, quality_runtime_path = _production_quality_runtime()
    rows = []
    selected = [record for record in manifest.get("records", [])
                if record.get("split") == args.split]
    for record in selected:
        image = dataset_root / record["image"]
        try:
            decoded = quality_runtime.decode_image_bytes(
                image.read_bytes(), max_decoded_pixels=MAX_DECODED_PIXELS)
            runtime_quality = quality_runtime.image_quality(decoded, np)
        except Exception as error:
            raise PipelineError(
                f"production quality gate failed for {record['source_item_id']}: {error}") from error
        if runtime_quality != "acceptable":
            raise PipelineError(
                f"held-out {args.split} image is rejected by the production quality gate: "
                f"{record['source_item_id']}")
        results = model.predict(
            source=str(image), imgsz=640, conf=MIN_DETECTION_CONFIDENCE, iou=0.7,
            agnostic_nms=True, max_det=100, device=args.device,
            rect=False, verbose=False, save=False,
        )
        if len(results) != 1:
            raise PipelineError(f"model returned {len(results)} results for one image")
        result = results[0]
        boxes = getattr(result, "boxes", None)
        predictions = []
        if boxes is not None:
            xywhn = boxes.xywhn.cpu().tolist()
            confidences = boxes.conf.cpu().tolist()
            class_ids = boxes.cls.cpu().tolist()
            for box, confidence, class_id in zip(xywhn, confidences, class_ids):
                if int(class_id) != 0:
                    raise PipelineError("trained model emitted a class other than class 0")
                predictions.append({
                    "box": list(normalise_box(box, f"prediction {record['source_item_id']}")),
                    "confidence": float(confidence),
                })
        rows.append({
            "source_item_id": record["source_item_id"],
            "image_sha256": record["image_sha256"],
            "capture_mode": record["capture_mode"],
            "runtime_image_quality": runtime_quality,
            "truth_boxes": record["boxes"],
            "predictions": predictions,
        })
    prediction_set = sealed({
        "schema_version": "pothole-yolo-predictions-v1",
        "task": "pothole_detection",
        "class_names": {"0": "pothole"},
        "image_size": 640,
        "detection_contract": detection_api_contract(),
        "dataset_manifest_sha256": manifest["manifest_sha256"],
        "weights_sha256": sha256_file(weights),
        "split": args.split,
        "quality_gate": quality_gate_contract(quality_runtime_path),
        "engine_environment": installed_training_engine_versions(),
        "inference": {"minimum_confidence": MIN_DETECTION_CONFIDENCE, "nms_iou": 0.7,
                      "agnostic_nms": True, "max_detections": 100,
                      "rect": False,
                      "device": "cpu"},
        "rows": rows,
    }, field="prediction_sha256")
    write_json(Path(args.output), prediction_set)
    print(json.dumps({"output": args.output,
                      "prediction_sha256": prediction_set["prediction_sha256"],
                      "images": len(rows)}, indent=2, sort_keys=True))


def command_select(args: argparse.Namespace) -> None:
    validate_release_constraints(
        min_box_recall=args.min_box_recall,
        min_positive_image_recall=args.min_positive_image_recall,
        max_negative_image_fp_rate=args.max_negative_image_fp_rate,
        iou_threshold=args.iou_threshold,
    )
    predictions_path = Path(args.predictions)
    predictions = _load_predictions(predictions_path, "validation")
    threshold, metrics, passed = choose_threshold(
        predictions["rows"], args.min_box_recall, args.min_positive_image_recall,
        args.max_negative_image_fp_rate, args.iou_threshold,
    )
    receipt = sealed({
        "schema_version": "pothole-yolo-threshold-v1",
        "task": "pothole_detection",
        "class_names": {"0": "pothole"},
        "detection_contract": predictions["detection_contract"],
        "dataset_manifest_sha256": predictions["dataset_manifest_sha256"],
        "weights_sha256": predictions["weights_sha256"],
        "validation_prediction_sha256": predictions["prediction_sha256"],
        "quality_gate": predictions["quality_gate"],
        "engine_environment": predictions["engine_environment"],
        "prediction_inference": predictions["inference"],
        "selected_threshold": threshold,
        "iou_threshold": args.iou_threshold,
        "constraints": {
            "minimum_box_recall": args.min_box_recall,
            "minimum_positive_image_recall": args.min_positive_image_recall,
            "maximum_negative_image_false_positive_rate": args.max_negative_image_fp_rate,
        },
        "validation_metrics": metrics,
        "gate_passed": passed,
    }, field="threshold_receipt_sha256")
    write_json(Path(args.output), receipt)
    print(json.dumps({"output": args.output, "selected_threshold": threshold,
                      "gate_passed": passed, "metrics": metrics}, indent=2, sort_keys=True))


def command_score_test(args: argparse.Namespace) -> None:
    predictions = _load_predictions(Path(args.predictions), "test")
    threshold = load_json(Path(args.threshold))
    if (not isinstance(threshold, dict)
            or threshold.get("schema_version") != "pothole-yolo-threshold-v1"):
        raise PipelineError("unsupported threshold receipt")
    verify_seal(threshold, "threshold_receipt_sha256")
    if threshold.get("gate_passed") is not True:
        raise PipelineError("validation threshold gate did not pass")
    if threshold.get("quality_gate") != predictions.get("quality_gate"):
        raise PipelineError("test predictions and threshold disagree on quality gate")
    if threshold.get("engine_environment") != predictions.get("engine_environment"):
        raise PipelineError("test predictions and threshold disagree on engine environment")
    if threshold.get("prediction_inference") != predictions.get("inference"):
        raise PipelineError("test predictions and threshold disagree on inference contract")
    for field in ("dataset_manifest_sha256", "weights_sha256"):
        if threshold.get(field) != predictions.get(field):
            raise PipelineError(f"test predictions and threshold disagree on {field}")
    try:
        selected = float(threshold["selected_threshold"])
        iou_threshold = float(threshold["iou_threshold"])
        constraints = threshold["constraints"]
        min_box_recall = float(constraints["minimum_box_recall"])
        min_positive_recall = float(constraints["minimum_positive_image_recall"])
        max_negative_fp = float(
            constraints["maximum_negative_image_false_positive_rate"])
    except (KeyError, TypeError, ValueError) as error:
        raise PipelineError("validation threshold policy is malformed") from error
    if not MIN_DETECTION_CONFIDENCE <= selected <= 1.0:
        raise PipelineError("selected threshold is outside the deployable range")
    validate_release_constraints(
        min_box_recall=min_box_recall,
        min_positive_image_recall=min_positive_recall,
        max_negative_image_fp_rate=max_negative_fp,
        iou_threshold=iou_threshold,
    )
    metrics = score_rows(predictions["rows"], selected, iou_threshold)
    passed = metrics_pass_release_constraints(metrics, constraints)
    receipt = sealed({
        "schema_version": "pothole-yolo-test-evaluation-v1",
        "task": "pothole_detection",
        "class_names": {"0": "pothole"},
        "detection_contract": predictions["detection_contract"],
        "dataset_manifest_sha256": predictions["dataset_manifest_sha256"],
        "weights_sha256": predictions["weights_sha256"],
        "test_prediction_sha256": predictions["prediction_sha256"],
        "threshold_receipt_sha256": threshold["threshold_receipt_sha256"],
        "quality_gate": predictions["quality_gate"],
        "engine_environment": predictions["engine_environment"],
        "prediction_inference": predictions["inference"],
        "selected_threshold": selected,
        "constraints": constraints,
        "test_metrics": metrics,
        "gate_passed": passed,
    }, field="evaluation_receipt_sha256")
    write_json(Path(args.output), receipt)
    print(json.dumps({"output": args.output, "gate_passed": passed,
                      "metrics": metrics}, indent=2, sort_keys=True))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    subparsers = result.add_subparsers(dest="command", required=True)
    predict = subparsers.add_parser("predict")
    predict.add_argument("--dataset", required=True)
    predict.add_argument("--weights", required=True)
    predict.add_argument("--split", choices=("validation", "test"), required=True)
    predict.add_argument("--device", default="cpu")
    predict.add_argument("--output", required=True)
    predict.set_defaults(handler=command_predict)
    select = subparsers.add_parser("select-threshold")
    select.add_argument("--predictions", required=True)
    select.add_argument("--output", required=True)
    select.add_argument("--iou-threshold", type=float, default=0.5)
    select.add_argument("--min-box-recall", type=float, default=0.90)
    select.add_argument("--min-positive-image-recall", type=float, default=0.90)
    select.add_argument("--max-negative-image-fp-rate", type=float, default=0.05)
    select.set_defaults(handler=command_select)
    test = subparsers.add_parser("score-test")
    test.add_argument("--predictions", required=True)
    test.add_argument("--threshold", required=True)
    test.add_argument("--output", required=True)
    test.set_defaults(handler=command_score_test)
    return result


def main() -> None:
    try:
        args = parser().parse_args()
        for name in ("iou_threshold", "min_box_recall", "min_positive_image_recall",
                     "max_negative_image_fp_rate"):
            if hasattr(args, name) and not 0.0 <= getattr(args, name) <= 1.0:
                raise PipelineError(f"--{name.replace('_', '-')} must be in [0,1]")
        args.handler(args)
    except PipelineError as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
