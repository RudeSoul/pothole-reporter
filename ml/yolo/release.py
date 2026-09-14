#!/usr/bin/env python3
"""Export fixed-shape raw ONNX and build the fail-closed AWS model bundle."""

from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path
from typing import Any, Mapping

try:
    from .common import (
        MIN_DETECTION_CONFIDENCE, PipelineError, RELEASE_MINIMUMS,
        detection_api_contract, installed_training_engine_versions, load_json,
        sealed, sha256_file, validate_prepared_dataset, verify_seal, write_json,
    )
    from .evaluate import (
        choose_threshold, metrics_pass_release_constraints, quality_gate_contract,
        score_rows, validate_release_constraints,
    )
    from .parity import (
        DEFAULT_CONFIDENCE_DELTA, LAMBDA_BASE_IMAGE_DIGEST, DEFAULT_MATCH_IOU,
        MAX_DECODED_PIXELS,
        build_parity_receipt,
        run_production_onnx,
        runtime_dependency_versions,
        runtime_execution_environment,
    )
except ImportError:
    from common import (  # type: ignore
        MIN_DETECTION_CONFIDENCE, PipelineError, RELEASE_MINIMUMS,
        detection_api_contract, installed_training_engine_versions, load_json,
        sealed, sha256_file, validate_prepared_dataset, verify_seal, write_json,
    )
    from evaluate import (  # type: ignore
        choose_threshold, metrics_pass_release_constraints, quality_gate_contract,
        score_rows, validate_release_constraints,
    )
    from parity import (  # type: ignore
        DEFAULT_CONFIDENCE_DELTA, LAMBDA_BASE_IMAGE_DIGEST, DEFAULT_MATCH_IOU,
        MAX_DECODED_PIXELS,
        build_parity_receipt,
        run_production_onnx,
        runtime_dependency_versions,
        runtime_execution_environment,
    )


def _load_receipt(path: Path, schema: str, seal_field: str) -> dict[str, Any]:
    value = load_json(path)
    if not isinstance(value, dict) or value.get("schema_version") != schema:
        raise PipelineError(f"{path}: expected {schema}")
    verify_seal(value, seal_field)
    return value


def verify_evaluation_receipts(
    *,
    validation_predictions: Mapping[str, Any],
    test_predictions: Mapping[str, Any],
    threshold: Mapping[str, Any],
    evaluation: Mapping[str, Any],
) -> None:
    """Recompute both gates so self-sealed but fabricated metrics cannot release."""
    expected_contract = detection_api_contract()
    if any(receipt.get("detection_contract") != expected_contract for receipt in (
            validation_predictions, test_predictions, threshold, evaluation)):
        raise PipelineError("evaluation detection contracts are missing or stale")
    constraints = threshold.get("constraints")
    if not isinstance(constraints, dict):
        raise PipelineError("validation threshold constraints are malformed")
    try:
        minimum_box_recall = float(constraints["minimum_box_recall"])
        minimum_positive_recall = float(
            constraints["minimum_positive_image_recall"])
        maximum_negative_fp = float(
            constraints["maximum_negative_image_false_positive_rate"])
        iou_threshold = float(threshold["iou_threshold"])
    except (KeyError, TypeError, ValueError) as error:
        raise PipelineError("validation threshold constraints are malformed") from error
    selected, validation_metrics, validation_passed = choose_threshold(
        validation_predictions.get("rows", []),
        minimum_box_recall,
        minimum_positive_recall,
        maximum_negative_fp,
        iou_threshold,
    )
    if (not validation_passed
            or threshold.get("gate_passed") is not True
            or threshold.get("selected_threshold") != selected
            or threshold.get("validation_metrics") != validation_metrics
            or threshold.get("validation_prediction_sha256")
            != validation_predictions.get("prediction_sha256")):
        raise PipelineError("validation threshold receipt does not match recomputed predictions")
    test_metrics = score_rows(
        test_predictions.get("rows", []), selected, iou_threshold)
    test_passed = metrics_pass_release_constraints(test_metrics, constraints)
    if (not test_passed
            or evaluation.get("gate_passed") is not True
            or evaluation.get("selected_threshold") != selected
            or evaluation.get("constraints") != constraints
            or evaluation.get("test_metrics") != test_metrics
            or evaluation.get("test_prediction_sha256")
            != test_predictions.get("prediction_sha256")
            or evaluation.get("threshold_receipt_sha256")
            != threshold.get("threshold_receipt_sha256")):
        raise PipelineError("test evaluation receipt does not match recomputed predictions")


def verify_prediction_split_coverage(
    predictions: Mapping[str, Any],
    dataset: Mapping[str, Any],
    split: str,
) -> None:
    """Bind receipt rows to every exact image and human truth in the sealed split."""
    expected: dict[str, Mapping[str, Any]] = {}
    for record in dataset.get("records", []):
        if isinstance(record, dict) and record.get("split") == split:
            item_id = record.get("source_item_id")
            if not isinstance(item_id, str) or not item_id or item_id in expected:
                raise PipelineError(f"sealed dataset has invalid {split} source IDs")
            expected[item_id] = record
    rows = predictions.get("rows")
    if not isinstance(rows, list):
        raise PipelineError(f"{split} predictions have no rows")
    observed: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise PipelineError(f"{split} prediction row is malformed")
        item_id = row.get("source_item_id")
        if not isinstance(item_id, str) or not item_id or item_id in observed:
            raise PipelineError(f"{split} predictions have invalid or duplicate source IDs")
        observed[item_id] = row
    if set(observed) != set(expected):
        raise PipelineError(f"{split} predictions do not cover the exact sealed split")
    for item_id, row in observed.items():
        record = expected[item_id]
        if row.get("image_sha256") != record.get("image_sha256"):
            raise PipelineError(f"{split} prediction image hash differs for {item_id}")
        if row.get("truth_boxes") != record.get("boxes"):
            raise PipelineError(f"{split} prediction truth differs for {item_id}")
        if row.get("capture_mode") != record.get("capture_mode"):
            raise PipelineError(f"{split} prediction capture mode differs for {item_id}")
        if row.get("leakage_group") != record.get("leakage_group"):
            raise PipelineError(f"{split} prediction leakage group differs for {item_id}")


def build_model_manifest(
    *, model_version: str, model_sha256: str, model_bytes: int,
    dataset: Mapping[str, Any], training: Mapping[str, Any],
    threshold: Mapping[str, Any], evaluation: Mapping[str, Any],
    parity: Mapping[str, Any],
) -> dict[str, Any]:
    if not model_version or len(model_version) > 80:
        raise PipelineError("model version must contain 1 to 80 characters")
    if not re.fullmatch(r"[0-9a-f]{64}", model_sha256) or model_bytes <= 0:
        raise PipelineError("model artifact needs a valid SHA-256 and positive byte count")
    if dataset.get("class_names") != {"0": "pothole"}:
        raise PipelineError("dataset class 0 is not explicitly pothole")
    if dataset.get("release_ready") is not True:
        raise PipelineError("dataset was not release-ready")
    if dataset.get("release_minimums") != RELEASE_MINIMUMS:
        raise PipelineError("dataset release minimums differ from the safety policy")
    if (dataset.get("detection_contract") != detection_api_contract()
            or training.get("detection_contract") != detection_api_contract()):
        raise PipelineError("dataset or training detection contract is missing or stale")
    if threshold.get("gate_passed") is not True:
        raise PipelineError("validation threshold gate did not pass")
    if evaluation.get("gate_passed") is not True:
        raise PipelineError("sealed test evaluation gate did not pass")
    try:
        selected_threshold = float(threshold["selected_threshold"])
    except (KeyError, TypeError, ValueError) as error:
        raise PipelineError("selected detection threshold is malformed") from error
    if not MIN_DETECTION_CONFIDENCE <= selected_threshold <= 1.0:
        raise PipelineError("selected detection threshold cannot be deployed on AWS")
    constraints = threshold.get("constraints")
    if not isinstance(constraints, dict):
        raise PipelineError("validation threshold constraints are malformed")
    try:
        min_box_recall = float(constraints["minimum_box_recall"])
        min_positive_recall = float(constraints["minimum_positive_image_recall"])
        max_negative_fp = float(
            constraints["maximum_negative_image_false_positive_rate"])
        validation_iou = float(threshold["iou_threshold"])
    except (KeyError, TypeError, ValueError) as error:
        raise PipelineError("validation threshold constraints are malformed") from error
    validate_release_constraints(
        min_box_recall=min_box_recall,
        min_positive_image_recall=min_positive_recall,
        max_negative_image_fp_rate=max_negative_fp,
        iou_threshold=validation_iou,
    )
    expected_quality_gate = quality_gate_contract()
    if (threshold.get("quality_gate") != expected_quality_gate
            or evaluation.get("quality_gate") != expected_quality_gate):
        raise PipelineError("evaluation quality-gate receipts are missing or stale")
    expected_inference = {
        "minimum_confidence": MIN_DETECTION_CONFIDENCE,
        "nms_iou": 0.7,
        "agnostic_nms": True,
        "max_detections": 100,
        "rect": False,
        "device": "cpu",
    }
    if (threshold.get("prediction_inference") != expected_inference
            or evaluation.get("prediction_inference") != expected_inference):
        raise PipelineError("evaluation inference receipts are missing or stale")
    engine_environment = threshold.get("engine_environment")
    if (not isinstance(engine_environment, dict) or not engine_environment
            or evaluation.get("engine_environment") != engine_environment):
        raise PipelineError("validation and test engine environments differ")
    if evaluation.get("selected_threshold") != selected_threshold:
        raise PipelineError("test evaluation selected threshold differs from validation")
    if evaluation.get("constraints") != constraints:
        raise PipelineError("test evaluation constraints differ from validation")
    if (threshold.get("detection_contract") != detection_api_contract()
            or evaluation.get("detection_contract") != detection_api_contract()):
        raise PipelineError("evaluation detection-contract receipts are missing or stale")
    if not metrics_pass_release_constraints(
            threshold.get("validation_metrics", {}), constraints):
        raise PipelineError("validation metrics do not meet the release constraints")
    if not metrics_pass_release_constraints(
            evaluation.get("test_metrics", {}), constraints):
        raise PipelineError("test metrics do not meet the release constraints")
    if parity.get("gate_passed") is not True:
        raise PipelineError("production ONNX runtime parity gate did not pass")
    if (parity.get("schema_version") != "pothole-yolo-runtime-parity-v1"
            or parity.get("task") != "pothole_detection"
            or parity.get("split") != "test"
            or not isinstance(parity.get("records_compared"), int)
            or parity["records_compared"] < 1):
        raise PipelineError("production ONNX runtime parity receipt is malformed")
    if parity.get("detection_contract") != detection_api_contract():
        raise PipelineError("production runtime parity used a stale detection contract")
    hashes = {
        dataset.get("manifest_sha256"),
        training.get("dataset_manifest_sha256"),
        threshold.get("dataset_manifest_sha256"),
        evaluation.get("dataset_manifest_sha256"),
    }
    if len(hashes) != 1:
        raise PipelineError("dataset provenance hashes do not agree")
    weights_hashes = {
        training.get("result", {}).get("best_weights_sha256"),
        threshold.get("weights_sha256"),
        evaluation.get("weights_sha256"),
    }
    if len(weights_hashes) != 1:
        raise PipelineError("weights provenance hashes do not agree")
    if evaluation.get("threshold_receipt_sha256") != threshold.get("threshold_receipt_sha256"):
        raise PipelineError("test evaluation did not use the selected validation threshold")
    if parity.get("onnx_sha256") != model_sha256:
        raise PipelineError("runtime parity did not exercise this ONNX artifact")
    if parity.get("weights_sha256") != training["result"]["best_weights_sha256"]:
        raise PipelineError("runtime parity and training weights hashes differ")
    if parity.get("dataset_manifest_sha256") != dataset["manifest_sha256"]:
        raise PipelineError("runtime parity and dataset hashes differ")
    if parity.get("reference_prediction_sha256") != evaluation.get("test_prediction_sha256"):
        raise PipelineError("runtime parity did not use the sealed test predictions")
    expected_decision = {
        "confidence_threshold": selected_threshold,
        "nms_iou_threshold": 0.7,
        "maximum_detections": 100,
    }
    if parity.get("decision") != expected_decision:
        raise PipelineError("runtime parity did not use the release decision settings")
    if parity.get("runtime_limits") != {
        "maximum_decoded_pixels": MAX_DECODED_PIXELS,
        "input_images_per_request": 1,
    }:
        raise PipelineError("runtime parity did not use the production decode limit")
    if parity.get("tolerances") != {
        "minimum_matched_box_iou": DEFAULT_MATCH_IOU,
        "maximum_absolute_confidence_delta": DEFAULT_CONFIDENCE_DELTA,
        "maximum_unmatched_reference_detections": 0,
        "maximum_unmatched_runtime_detections": 0,
        "maximum_canonical_decision_mismatches": 0,
    }:
        raise PipelineError("runtime parity tolerances are not the release tolerances")
    metrics = parity.get("metrics")
    if (not isinstance(metrics, dict)
            or metrics.get("gate_passed") is not True
            or metrics.get("violations") != 0
            or metrics.get("canonical_decision_mismatches") != 0
            or metrics.get("canonical_decisions_compared") != parity["records_compared"]
            or metrics.get("records_compared") != parity["records_compared"]):
        raise PipelineError("runtime parity metrics did not pass")
    runtime_environment = parity.get("runtime_environment")
    if (not isinstance(runtime_environment, dict)
            or set(runtime_environment) != {"numpy", "onnxruntime", "Pillow"}
            or not all(isinstance(value, str) and value
                       for value in runtime_environment.values())):
        raise PipelineError("runtime parity dependency versions are incomplete")
    runtime_execution = parity.get("runtime_execution")
    if (not isinstance(runtime_execution, dict)
            or runtime_execution.get("python_major_minor") != "3.12"
            or runtime_execution.get("python_implementation") != "CPython"
            or runtime_execution.get("system") != "Linux"
            or runtime_execution.get("lambda_architecture") not in {"x86_64", "arm64"}
            or runtime_execution.get("onnx_provider") != "CPUExecutionProvider"
            or runtime_execution.get("lambda_base_image_digest")
            != LAMBDA_BASE_IMAGE_DIGEST
            or len(runtime_execution) != 6):
        raise PipelineError("runtime parity execution target is not Lambda-compatible")
    if not re.fullmatch(r"[0-9a-f]{64}", str(parity.get("runtime_module_sha256", ""))):
        raise PipelineError("runtime parity production-module hash is malformed")
    return sealed({
        "schema_version": "pothole-yolo-model-manifest-v1",
        "task": "pothole_detection",
        "model_version": model_version,
        "detection_contract": detection_api_contract(),
        "class_names": {"0": "pothole"},
        "image_size": 640,
        "export": {
            "format": "onnx",
            "opset": 17,
            "dynamic": False,
            "simplify": True,
            "nms": False,
            "raw_ultralytics_output": True,
        },
        "input_contract": {
            "shape": [1, 3, 640, 640],
            "layout": "NCHW",
            "dtype": "float32",
            "colour": "RGB",
            "normalization": "uint8 / 255.0",
            "resize": "aspect-preserving letterbox",
            "letterbox_fill_rgb": [114, 114, 114],
        },
        "output_contract": {
            "layout": "batch,channels,predictions",
            "channels": ["center_x", "center_y", "width", "height", "class_0_score"],
            "coordinates": "640x640 letterboxed input pixels",
            "postprocess": "confidence filter, class-agnostic NMS, then undo letterbox",
        },
        "decision": {
            "confidence_threshold": selected_threshold,
            "nms_iou_threshold": 0.7,
            "maximum_detections": 100,
            "validation_iou_threshold": threshold["iou_threshold"],
        },
        "artifact": {
            "filename": "model.onnx",
            "sha256": model_sha256,
            "bytes": model_bytes,
        },
        "dataset_provenance": {
            "manifest_sha256": dataset["manifest_sha256"],
            "source_registry_sha256": dataset.get("source_registry_sha256"),
            "source_corpora": dataset.get("source_corpora", {}),
            "source_counts": dataset.get("source_counts", {}),
            "split_counts": dataset.get("counts", {}).get("splits", {}),
            "release_minimums": dataset["release_minimums"],
            "split_policy": dataset.get("split_policy", {}),
            "provenance_policy": dataset.get("provenance_policy", {}),
        },
        "training_provenance": {
            "receipt_sha256": training["receipt_sha256"],
            "base_model": training["base_model"],
            "best_weights_sha256": training["result"]["best_weights_sha256"],
            "settings": training["training"],
            "environment": training["environment"],
        },
        "evaluation_provenance": {
            "quality_gate": expected_quality_gate,
            "prediction_inference": expected_inference,
            "engine_environment": engine_environment,
            "validation": {
                "threshold_receipt_sha256": threshold["threshold_receipt_sha256"],
                "prediction_sha256": threshold["validation_prediction_sha256"],
                "constraints": threshold["constraints"],
                "metrics": threshold["validation_metrics"],
            },
            "test": {
                "evaluation_receipt_sha256": evaluation["evaluation_receipt_sha256"],
                "prediction_sha256": evaluation["test_prediction_sha256"],
                "metrics": evaluation["test_metrics"],
            },
        },
        "runtime_parity": {
            "receipt_filename": "runtime-parity.json",
            "receipt_sha256": parity["parity_receipt_sha256"],
            "gate_passed": True,
            "onnx_sha256": parity["onnx_sha256"],
            "weights_sha256": parity["weights_sha256"],
            "dataset_manifest_sha256": parity["dataset_manifest_sha256"],
            "reference_prediction_sha256": parity["reference_prediction_sha256"],
            "runtime_module_sha256": parity["runtime_module_sha256"],
            "runtime_environment": parity["runtime_environment"],
            "runtime_execution": parity["runtime_execution"],
            "runtime_limits": parity["runtime_limits"],
            "detection_contract": parity["detection_contract"],
            "split": parity["split"],
            "records_compared": parity["records_compared"],
            "tolerances": parity["tolerances"],
            "metrics": parity["metrics"],
        },
    }, field="model_manifest_sha256")


def _validate_onnx(path: Path) -> None:
    try:
        import onnx
    except ImportError as error:
        raise PipelineError("onnx is required to validate the release artifact") from error
    try:
        model = onnx.load(str(path))
        onnx.checker.check_model(model)
    except Exception as error:
        raise PipelineError(f"exported ONNX did not validate: {error}") from error
    if len(model.graph.input) != 1 or len(model.graph.output) != 1:
        raise PipelineError("ONNX must expose exactly one tensor input and one tensor output")
    dimensions = model.graph.input[0].type.tensor_type.shape.dim
    shape = [dimension.dim_value if dimension.HasField("dim_value") else None
             for dimension in dimensions]
    if shape != [1, 3, 640, 640]:
        raise PipelineError(f"ONNX input must be statically [1,3,640,640], got {shape}")
    if any(node.op_type == "NonMaxSuppression" for node in model.graph.node):
        raise PipelineError("ONNX unexpectedly contains NonMaxSuppression")
    output_dimensions = model.graph.output[0].type.tensor_type.shape.dim
    output_shape = [dimension.dim_value if dimension.HasField("dim_value") else None
                    for dimension in output_dimensions]
    if 5 not in output_shape:
        raise PipelineError(
            f"raw single-class output must have a five-channel dimension, got {output_shape}")


def command_release(args: argparse.Namespace) -> None:
    weights = Path(args.weights).resolve()
    if not weights.is_file():
        raise PipelineError(f"weights do not exist: {weights}")
    dataset = _load_receipt(Path(args.dataset_manifest), "pothole-yolo-dataset-v1",
                            "manifest_sha256")
    validate_prepared_dataset(Path(args.dataset_manifest).resolve().parent, dataset)
    training = _load_receipt(Path(args.training_receipt),
                             "pothole-yolo-training-receipt-v1", "receipt_sha256")
    threshold = _load_receipt(Path(args.threshold_receipt),
                              "pothole-yolo-threshold-v2", "threshold_receipt_sha256")
    evaluation = _load_receipt(Path(args.test_evaluation),
                               "pothole-yolo-test-evaluation-v2",
                               "evaluation_receipt_sha256")
    validation_predictions = _load_receipt(
        Path(args.validation_predictions), "pothole-yolo-predictions-v2",
        "prediction_sha256")
    test_predictions = _load_receipt(Path(args.test_predictions),
                                     "pothole-yolo-predictions-v2",
                                     "prediction_sha256")
    if test_predictions.get("split") != "test":
        raise PipelineError("runtime parity requires sealed test predictions")
    if validation_predictions.get("split") != "validation":
        raise PipelineError("release requires sealed validation predictions")
    for predictions in (validation_predictions, test_predictions):
        if predictions.get("dataset_manifest_sha256") != dataset.get("manifest_sha256"):
            raise PipelineError("prediction and dataset manifest hashes differ")
        if predictions.get("weights_sha256") != training["result"]["best_weights_sha256"]:
            raise PipelineError("prediction and training weights hashes differ")
        if predictions.get("quality_gate") != quality_gate_contract():
            raise PipelineError("prediction quality-gate receipt is stale")
    verify_prediction_split_coverage(validation_predictions, dataset, "validation")
    verify_prediction_split_coverage(test_predictions, dataset, "test")
    if test_predictions.get("prediction_sha256") != evaluation.get("test_prediction_sha256"):
        raise PipelineError("test evaluation and parity prediction receipts differ")
    if test_predictions.get("dataset_manifest_sha256") != dataset.get("manifest_sha256"):
        raise PipelineError("test predictions and dataset manifest hashes differ")
    if test_predictions.get("weights_sha256") != training["result"]["best_weights_sha256"]:
        raise PipelineError("test predictions and training weights hashes differ")
    if test_predictions.get("quality_gate") != quality_gate_contract():
        raise PipelineError("test prediction quality-gate receipt is stale")
    if test_predictions.get("quality_gate") != evaluation.get("quality_gate"):
        raise PipelineError("test prediction and evaluation quality gates differ")
    if test_predictions.get("inference") != evaluation.get("prediction_inference"):
        raise PipelineError("test prediction and evaluation inference contracts differ")
    engine_environment = installed_training_engine_versions()
    if test_predictions.get("engine_environment") != engine_environment:
        raise PipelineError("test predictions used a different execution engine")
    if evaluation.get("engine_environment") != engine_environment:
        raise PipelineError("test evaluation used a different execution engine")
    if training.get("environment", {}).get("engine_versions") != engine_environment:
        raise PipelineError("training used a different execution engine")
    if validation_predictions.get("engine_environment") != engine_environment:
        raise PipelineError("validation predictions used a different execution engine")
    if validation_predictions.get("inference") != threshold.get("prediction_inference"):
        raise PipelineError("validation prediction and threshold inference contracts differ")
    if validation_predictions.get("quality_gate") != threshold.get("quality_gate"):
        raise PipelineError("validation prediction and threshold quality gates differ")
    verify_evaluation_receipts(
        validation_predictions=validation_predictions,
        test_predictions=test_predictions,
        threshold=threshold,
        evaluation=evaluation,
    )
    if sha256_file(weights) != training["result"]["best_weights_sha256"]:
        raise PipelineError("weights bytes do not match the training receipt")
    # Fail before ONNX export when release is attempted from a developer host or
    # environment that cannot represent the target Lambda native runtime.
    runtime_environment = runtime_dependency_versions()
    runtime_execution = runtime_execution_environment()
    output = Path(args.output).resolve()
    if output.exists():
        raise PipelineError(f"release output already exists: {output}")
    try:
        from ultralytics import YOLO
    except ImportError as error:
        raise PipelineError("Ultralytics is required for ONNX export") from error
    model = YOLO(str(weights))
    names = {str(key): value for key, value in dict(model.names).items()}
    if names != {"0": "pothole"}:
        raise PipelineError(f"weights class map must be 0=pothole, got {names}")
    exported = Path(str(model.export(
        format="onnx",
        imgsz=640,
        batch=1,
        dynamic=False,
        simplify=True,
        nms=False,
        opset=17,
        half=False,
        int8=False,
        device="cpu",
    ))).resolve()
    if not exported.is_file():
        raise PipelineError("Ultralytics did not produce an ONNX file")
    _validate_onnx(exported)
    runtime_module = Path(__file__).resolve().parents[2] / "infra/aws-yolo/service/detector.py"
    decision = {
        "confidence_threshold": float(threshold["selected_threshold"]),
        "nms_iou_threshold": 0.7,
        "maximum_detections": 100,
    }
    runtime_rows = run_production_onnx(
        model_path=exported,
        dataset_root=Path(args.dataset_manifest).resolve().parent,
        reference_rows=test_predictions["rows"],
        dataset_records=dataset["records"],
        runtime_module_path=runtime_module,
        **decision,
    )
    parity = build_parity_receipt(
        onnx_path=exported,
        weights_sha256=training["result"]["best_weights_sha256"],
        dataset_manifest_sha256=dataset["manifest_sha256"],
        reference_prediction_sha256=test_predictions["prediction_sha256"],
        runtime_module_path=runtime_module,
        reference_rows=test_predictions["rows"],
        runtime_rows=runtime_rows,
        runtime_environment=runtime_environment,
        runtime_execution=runtime_execution,
        **decision,
    )
    if parity["gate_passed"] is not True:
        metrics = parity["metrics"]
        raise PipelineError(
            "production ONNX runtime parity failed: "
            f"{metrics['violations']} violation(s), "
            f"{metrics['unmatched_reference_detections']} unmatched PT detection(s), "
            f"{metrics['unmatched_runtime_detections']} unmatched ONNX detection(s)")
    output.mkdir(parents=True)
    target = output / "model.onnx"
    shutil.copy2(exported, target)
    manifest = build_model_manifest(
        model_version=args.model_version,
        model_sha256=sha256_file(target),
        model_bytes=target.stat().st_size,
        dataset=dataset,
        training=training,
        threshold=threshold,
        evaluation=evaluation,
        parity=parity,
    )
    write_json(output / "runtime-parity.json", parity)
    write_json(output / "model-manifest.json", manifest)
    print(json.dumps({
        "artifact": str(target),
        "artifact_sha256": manifest["artifact"]["sha256"],
        "manifest": str(output / "model-manifest.json"),
        "model_manifest_sha256": manifest["model_manifest_sha256"],
        "runtime_parity": str(output / "runtime-parity.json"),
        "runtime_parity_sha256": parity["parity_receipt_sha256"],
    }, indent=2, sort_keys=True))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--weights", required=True)
    result.add_argument("--dataset-manifest", required=True)
    result.add_argument("--training-receipt", required=True)
    result.add_argument("--threshold-receipt", required=True)
    result.add_argument("--test-evaluation", required=True)
    result.add_argument(
        "--validation-predictions", required=True,
        help="sealed .pt predictions for every held-out validation record",
    )
    result.add_argument("--test-predictions", required=True,
                        help="sealed .pt predictions for every held-out test record")
    result.add_argument("--model-version", required=True)
    result.add_argument("--output", default="ml/yolo/artifacts/pothole-yolo-v1")
    return result


def main() -> None:
    try:
        command_release(parser().parse_args())
    except PipelineError as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
