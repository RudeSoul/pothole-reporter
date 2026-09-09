from __future__ import annotations

import json
import io
import hashlib
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


SERVICE = pathlib.Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from detector import (  # noqa: E402
    AnalysedImage,
    DETECTION_CONTRACT,
    Detection,
    RUNTIME_LIMITS,
    canonical_verdict,
    decode_image_bytes,
    load_and_validate_manifest,
    non_max_suppression,
    validate_release_bundle,
    YoloOnnxDetector,
)


class CanonicalVerdictTests(unittest.TestCase):
    def test_detection_is_damaged(self):
        result = canonical_verdict(
            AnalysedImage(
                1000, 800, "acceptable",
                (Detection(100, 400, 300, 650, 0.8),),
            ),
            confidence_threshold=0.5,
            language="en",
        )
        self.assertEqual(result["assessment"], "damaged")
        self.assertEqual(result["image_quality"], "acceptable")
        self.assertEqual(result["damage_type"], "pothole_cavity")
        self.assertIsNone(result["size"])

    def test_acceptable_frame_without_detection_is_undamaged(self):
        result = canonical_verdict(
            AnalysedImage(1000, 800, "acceptable", ()),
            confidence_threshold=0.5,
            language="en",
        )
        self.assertEqual(result["assessment"], "undamaged")
        self.assertIsNone(result["damage_type"])

    def test_detection_at_top_edge_is_not_suppressed(self):
        result = canonical_verdict(
            AnalysedImage(
                1000, 800, "acceptable",
                (Detection(100, 0, 300, 50, 0.9),),
            ),
            confidence_threshold=0.5,
            language="en",
        )
        self.assertEqual(result["assessment"], "damaged")
        self.assertEqual(result["damage_type"], "pothole_cavity")

    def test_rejected_image_is_undamaged_without_inventing_a_detection(self):
        result = canonical_verdict(
            AnalysedImage(100, 80, "rejected", ()),
            confidence_threshold=0.5,
            language="en",
        )
        self.assertEqual(result["assessment"], "undamaged")
        self.assertEqual(result["image_quality"], "rejected")
        self.assertIsNone(result["damage_type"])

    def test_nms_keeps_best_overlapping_box(self):
        kept = non_max_suppression(
            [
                Detection(0, 0, 100, 100, 0.9),
                Detection(5, 5, 95, 95, 0.8),
                Detection(200, 200, 250, 250, 0.7),
            ],
            0.45,
            10,
        )
        self.assertEqual([item.confidence for item in kept], [0.9, 0.7])


class ManifestTests(unittest.TestCase):
    def write_manifest(self, value):
        value = dict(value)
        canonical = (
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode()
        value["model_manifest_sha256"] = hashlib.sha256(canonical).hexdigest()
        directory = tempfile.TemporaryDirectory()
        path = pathlib.Path(directory.name) / "model-manifest.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return directory, path

    def test_accepts_explicit_single_pothole_raw_onnx(self):
        digest = "a" * 64
        directory, path = self.write_manifest(
            {
                "schema_version": "pothole-yolo-model-manifest-v1",
                "task": "pothole_detection",
                "model_version": "v1",
                "detection_contract": DETECTION_CONTRACT,
                "class_names": {"0": "pothole"},
                "image_size": 640,
                "export": {
                    "format": "onnx", "nms": False, "dynamic": False, "simplify": True,
                    "opset": 17,
                    "raw_ultralytics_output": True,
                },
                "output_contract": {
                    "layout": "batch,channels,predictions",
                    "channels": [
                        "center_x", "center_y", "width", "height", "class_0_score"
                    ],
                },
                "decision": {
                    "confidence_threshold": 0.5,
                    "nms_iou_threshold": 0.7,
                    "maximum_detections": 100,
                },
                "dataset_provenance": {"manifest_sha256": digest},
                "training_provenance": {
                    "receipt_sha256": digest, "best_weights_sha256": digest
                },
                "evaluation_provenance": {
                    "validation": {"threshold_receipt_sha256": digest},
                    "test": {
                        "evaluation_receipt_sha256": digest,
                        "prediction_sha256": digest,
                    },
                },
                "artifact": {"sha256": digest, "bytes": 1},
                "runtime_parity": {
                    "receipt_sha256": digest,
                    "gate_passed": True,
                    "onnx_sha256": digest,
                    "weights_sha256": digest,
                    "dataset_manifest_sha256": digest,
                    "reference_prediction_sha256": digest,
                    "runtime_module_sha256": digest,
                    "runtime_environment": {
                        "numpy": "1.26.4",
                        "onnxruntime": "1.19.2",
                        "Pillow": "11.3.0",
                    },
                    "runtime_limits": RUNTIME_LIMITS,
                    "detection_contract": DETECTION_CONTRACT,
                    "runtime_execution": {
                        "python_major_minor": "3.12",
                        "python_implementation": "CPython",
                        "system": "Linux",
                        "lambda_architecture": "x86_64",
                        "onnx_provider": "CPUExecutionProvider",
                        "lambda_base_image_digest": "sha256:ab6df78b68b50723c93741bb7f9ea9f68c7cf3433359b33e4f37db5266567f9c",
                    },
                    "split": "test",
                    "records_compared": 1,
                },
            }
        )
        self.addCleanup(directory.cleanup)
        self.assertEqual(load_and_validate_manifest(str(path), "v1")["image_size"], 640)

    def test_rejects_mixed_road_damage_class(self):
        digest = "a" * 64
        directory, path = self.write_manifest(
            {
                "schema_version": "pothole-yolo-model-manifest-v1",
                "task": "pothole_detection",
                "model_version": "v1",
                "detection_contract": DETECTION_CONTRACT,
                "class_names": {"0": "RoadDamages"},
                "image_size": 640,
                "export": {
                    "format": "onnx", "nms": False, "dynamic": False, "simplify": True,
                    "opset": 17,
                    "raw_ultralytics_output": True,
                },
                "dataset_provenance": {"manifest_sha256": digest},
                "training_provenance": {
                    "receipt_sha256": digest, "best_weights_sha256": digest
                },
                "evaluation_provenance": {
                    "validation": {"threshold_receipt_sha256": digest},
                    "test": {"evaluation_receipt_sha256": digest},
                },
            }
        )
        self.addCleanup(directory.cleanup)
        with self.assertRaises(RuntimeError):
            load_and_validate_manifest(str(path), "v1")

    def test_release_bundle_binds_model_parity_code_and_dependency_versions(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = pathlib.Path(directory.name)
        model_path = root / "model.onnx"
        model_path.write_bytes(b"evaluated-onnx-placeholder-for-contract-test")
        model_hash = hashlib.sha256(model_path.read_bytes()).hexdigest()
        detector_hash = hashlib.sha256((SERVICE / "detector.py").read_bytes()).hexdigest()
        receipt_hash = "b" * 64
        dataset_hash = "c" * 64
        weights_hash = "d" * 64
        prediction_hash = "e" * 64
        environment = {
            "numpy": "1.26.4",
            "onnxruntime": "1.19.2",
            "Pillow": "11.3.0",
        }
        runtime_execution = {
            "python_major_minor": "3.12",
            "python_implementation": "CPython",
            "system": "Linux",
            "lambda_architecture": "x86_64",
            "onnx_provider": "CPUExecutionProvider",
            "lambda_base_image_digest": "sha256:ab6df78b68b50723c93741bb7f9ea9f68c7cf3433359b33e4f37db5266567f9c",
        }
        decision = {
            "confidence_threshold": 0.5,
            "nms_iou_threshold": 0.7,
            "maximum_detections": 100,
        }
        metrics = {
            "gate_passed": True,
            "violations": 0,
            "canonical_decisions_compared": 1,
            "canonical_decision_mismatches": 0,
        }
        tolerances = {
            "minimum_matched_box_iou": 0.98,
            "maximum_absolute_confidence_delta": 0.03,
        }
        parity = {
            "schema_version": "pothole-yolo-runtime-parity-v1",
            "task": "pothole_detection",
            "gate_passed": True,
            "metrics": metrics,
            "onnx_sha256": model_hash,
            "weights_sha256": weights_hash,
            "dataset_manifest_sha256": dataset_hash,
            "reference_prediction_sha256": prediction_hash,
            "runtime_module_sha256": detector_hash,
            "runtime_environment": environment,
            "runtime_limits": RUNTIME_LIMITS,
            "runtime_execution": runtime_execution,
            "detection_contract": DETECTION_CONTRACT,
            "split": "test",
            "records_compared": 1,
            "decision": decision,
            "tolerances": tolerances,
        }
        parity_canonical = (
            json.dumps(parity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode()
        parity["parity_receipt_sha256"] = hashlib.sha256(parity_canonical).hexdigest()
        parity_path = root / "runtime-parity.json"
        parity_path.write_text(json.dumps(parity), encoding="utf-8")
        manifest = {
            "schema_version": "pothole-yolo-model-manifest-v1",
            "task": "pothole_detection",
            "model_version": "v1",
            "detection_contract": DETECTION_CONTRACT,
            "class_names": {"0": "pothole"},
            "image_size": 640,
            "export": {
                "format": "onnx", "nms": False, "dynamic": False,
                "simplify": True, "opset": 17, "raw_ultralytics_output": True,
            },
            "output_contract": {
                "layout": "batch,channels,predictions",
                "channels": [
                    "center_x", "center_y", "width", "height", "class_0_score"
                ],
            },
            "decision": {**decision, "validation_iou_threshold": 0.5},
            "dataset_provenance": {"manifest_sha256": dataset_hash},
            "training_provenance": {
                "receipt_sha256": receipt_hash,
                "best_weights_sha256": weights_hash,
            },
            "evaluation_provenance": {
                "validation": {"threshold_receipt_sha256": receipt_hash},
                "test": {
                    "evaluation_receipt_sha256": receipt_hash,
                    "prediction_sha256": prediction_hash,
                },
            },
            "artifact": {"sha256": model_hash, "bytes": model_path.stat().st_size},
            "runtime_parity": {
                "receipt_sha256": parity["parity_receipt_sha256"],
                "gate_passed": True,
                "onnx_sha256": model_hash,
                "weights_sha256": weights_hash,
                "dataset_manifest_sha256": dataset_hash,
                "reference_prediction_sha256": prediction_hash,
                "runtime_module_sha256": detector_hash,
                "runtime_environment": environment,
                "runtime_limits": RUNTIME_LIMITS,
                "runtime_execution": runtime_execution,
                "detection_contract": DETECTION_CONTRACT,
                "split": "test",
                "records_compared": 1,
                "tolerances": tolerances,
                "metrics": metrics,
            },
        }
        manifest_canonical = (
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode()
        manifest["model_manifest_sha256"] = hashlib.sha256(manifest_canonical).hexdigest()
        manifest_path = root / "model-manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with mock.patch(
            "detector.importlib.metadata.version", side_effect=lambda name: environment[name]
        ), mock.patch(
            "detector.current_runtime_execution", return_value=runtime_execution
        ):
            loaded = validate_release_bundle(
                str(model_path), str(manifest_path), str(parity_path),
                "v1", 0.5, 12_000_000,
            )
            with self.assertRaisesRegex(RuntimeError, "DETECTION_CONFIDENCE_THRESHOLD"):
                validate_release_bundle(
                    str(model_path), str(manifest_path), str(parity_path),
                    "v1", 0.60, 12_000_000,
                )
            with self.assertRaisesRegex(RuntimeError, "MAX_DECODED_PIXELS"):
                validate_release_bundle(
                    str(model_path), str(manifest_path), str(parity_path),
                    "v1", 0.5, 10_000_000,
                )
        self.assertEqual(loaded["runtime_parity"]["records_compared"], 1)


class DecodeSafetyTests(unittest.TestCase):
    def test_compressed_image_over_pixel_limit_is_rejected_before_use(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow is installed in the Lambda image")
        encoded = io.BytesIO()
        Image.new("RGB", (20, 20), (128, 128, 128)).save(encoded, format="PNG")
        with self.assertRaises(ValueError):
            decode_image_bytes(encoded.getvalue(), max_decoded_pixels=100)

    def test_detector_requires_exactly_one_image(self):
        detector = object.__new__(YoloOnnxDetector)
        for images in ([], [object(), object()]):
            with self.subTest(count=len(images)):
                with self.assertRaisesRegex(RuntimeError, "exactly one image"):
                    detector.analyse(images, capture_mode="manual", language="en")


class RawOutputTests(unittest.TestCase):
    def test_non_finite_or_invalid_raw_values_fail_closed(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest("NumPy is installed in the Lambda image")
        detector = object.__new__(YoloOnnxDetector)
        detector.np = np
        detector.confidence_threshold = 0.5
        detector.nms_iou_threshold = 0.7
        detector.max_detections = 100
        for invalid in (float("nan"), float("inf"), -1.0):
            with self.subTest(invalid=invalid):
                output = np.array([[[320.0], [320.0], [100.0], [100.0], [invalid]]])
                with self.assertRaisesRegex(RuntimeError, "unsafe numeric"):
                    detector._postprocess(
                        output, width=640, height=480,
                        ratio=1.0, pad_x=0.0, pad_y=80.0,
                    )

        invalid_width = np.array([[[320.0], [320.0], [0.0], [100.0], [0.9]]])
        with self.assertRaisesRegex(RuntimeError, "unsafe numeric"):
            detector._postprocess(
                invalid_width, width=640, height=480,
                ratio=1.0, pad_x=0.0, pad_y=80.0,
            )

    def test_ultralytics_channel_first_output_reverses_letterbox_and_nms(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest("NumPy is installed in the Lambda image")
        detector = object.__new__(YoloOnnxDetector)
        detector.np = np
        detector.confidence_threshold = 0.5
        detector.nms_iou_threshold = 0.7
        detector.max_detections = 100
        # [batch, x/y/w/h/class score, predictions]. The first two overlap;
        # the lower-confidence one must be removed by the evaluated NMS setting.
        output = np.array(
            [[
                [320.0, 322.0, 80.0, 20.0, 30.0, 40.0],
                [400.0, 402.0, 180.0, 20.0, 30.0, 40.0],
                [100.0, 100.0, 40.0, 10.0, 10.0, 10.0],
                [100.0, 100.0, 40.0, 10.0, 10.0, 10.0],
                [0.90, 0.80, 0.70, 0.01, 0.01, 0.01],
            ]],
            dtype=np.float32,
        )
        results = detector._postprocess(
            output,
            width=640,
            height=480,
            ratio=1.0,
            pad_x=0.0,
            pad_y=80.0,
        )
        self.assertEqual(len(results), 2)
        self.assertAlmostEqual(results[0].x1, 270.0)
        self.assertAlmostEqual(results[0].y1, 270.0)
        self.assertAlmostEqual(results[0].x2, 370.0)
        self.assertAlmostEqual(results[0].y2, 370.0)

    def test_nms_runs_before_source_boundary_clipping(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest("NumPy is installed in the Lambda image")
        detector = object.__new__(YoloOnnxDetector)
        detector.np = np
        detector.confidence_threshold = 0.5
        detector.nms_iou_threshold = 0.7
        detector.max_detections = 100
        # These boxes have low IoU in the model's letterboxed coordinates but
        # become identical only after the top padding is removed and clipped.
        # Ultralytics performs NMS before that clipping, so both must survive.
        output = np.array(
            [[
                [320.0, 320.0],
                [10.0, 80.0],
                [100.0, 100.0],
                [220.0, 80.0],
                [0.90, 0.80],
            ]],
            dtype=np.float32,
        )
        results = detector._postprocess(
            output,
            width=640,
            height=480,
            ratio=1.0,
            pad_x=0.0,
            pad_y=80.0,
        )
        self.assertEqual(len(results), 2)
        self.assertEqual(
            [(item.x1, item.y1, item.x2, item.y2) for item in results],
            [(270.0, 0.0, 370.0, 40.0), (270.0, 0.0, 370.0, 40.0)],
        )


if __name__ == "__main__":
    unittest.main()
