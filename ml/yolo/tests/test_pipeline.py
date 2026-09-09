#!/usr/bin/env python3

from __future__ import annotations

import json
import io
import shutil
import tempfile
import unittest
from pathlib import Path

from ml.yolo.common import (
    PipelineError, RELEASE_MINIMUMS, detection_api_contract, sealed,
    sha256_bytes, sha256_file, required_training_engine_versions,
    validate_prepared_dataset, verify_seal,
)
from ml.yolo.evaluate import (
    box_iou, choose_threshold, quality_gate_contract, score_rows,
)
from ml.yolo.parity import (
    MAX_DECODED_PIXELS, compare_prediction_sets,
    expected_runtime_dependency_versions, validate_runtime_verdict,
)
from ml.yolo.prepare_dataset import (
    Candidate, build_dataset, collect_owned, collect_rad, owned_source_digest,
)
from ml.yolo.release import (
    build_model_manifest, verify_evaluation_receipts,
    verify_prediction_split_coverage,
)

from PIL import Image


RAD_YAML = """train: ../train/images
val: ../valid/images
test: ../test/images

nc: 6
names: ['HMV', 'LMV', 'Pedestrian', 'RoadDamages', 'SpeedBump', 'UnsurfacedRoad']
"""


class PipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def image_bytes(colour: tuple[int, int, int], *, image_format: str = "JPEG") -> bytes:
        buffer = io.BytesIO()
        Image.new("RGB", (320, 240), colour).save(buffer, format=image_format)
        return buffer.getvalue()

    def make_owned(self) -> tuple[Path, str, dict[str, bytes]]:
        source = self.root / "owned"
        (source / "images").mkdir(parents=True)
        images = {
            "images/positive.jpg": self.image_bytes((90, 80, 70)),
            "images/negative.jpg": self.image_bytes((140, 130, 120)),
        }
        for name, data in images.items():
            (source / name).write_bytes(data)
        labels = {
            "exported_at": "2026-09-05T00:00:00Z",
            "count": 2,
            "images": [
                {"path": "images/positive.jpg", "label": "pothole_cavity",
                 "labelled_by": "owner", "licence": "owner", "drive_id": "drive-a"},
                {"path": "images/negative.jpg", "label": "undamaged",
                 "labelled_by": "owner", "licence": "owner", "drive_id": "drive-b"},
            ],
        }
        (source / "labels.json").write_text(json.dumps(labels), encoding="utf-8")
        return source, owned_source_digest(source), images

    def write_owned_audit(self, source_sha: str, images: dict[str, bytes]) -> Path:
        audit = self.root / "owned-audit.json"
        audit.write_text(json.dumps({
            "schema_version": "owned-pothole-box-audit-v1",
            "source_sha256": source_sha,
            "review": {"human_verified": True, "reviewer": "Test reviewer",
                       "reviewed_at": "2026-09-05T00:00:00Z"},
            "frames": [{
                "path": "images/positive.jpg",
                "image_sha256": sha256_bytes(images["images/positive.jpg"]),
                "complete_frame_review": True,
                "pothole_boxes": [[0.5, 0.6, 0.2, 0.25]],
            }],
        }), encoding="utf-8")
        return audit

    def make_rad(self) -> tuple[Path, dict[str, Path]]:
        root = self.root / "rad"
        dataset = root / "images"
        dataset.mkdir(parents=True)
        (dataset / "data.yaml").write_text(RAD_YAML, encoding="utf-8")
        for split in ("train", "valid", "test"):
            (dataset / split / "images").mkdir(parents=True)
            (dataset / split / "labels").mkdir(parents=True)
        paths: dict[str, Path] = {}
        fixtures = [
            ("train", "100_10-07-2023", 1, "3 0.5 0.6 0.2 0.25\n", "damage"),
            ("valid", "200_10-07-2023", 2, "4 0.5 0.6 0.4 0.15\n", "speed"),
            ("test", "300_10-07-2023", 3, "5 0.5 0.6 0.8 0.7\n", "unsurfaced"),
        ]
        for split, source, frame, label, key in fixtures:
            stem = f"{source}_mp4-{frame}_jpg.rf.{frame:032x}"
            image = dataset / split / "images" / f"{stem}.jpg"
            colour = (80, 70, 60) if key == "damage" else (170, 160, 150)
            image.write_bytes(self.image_bytes(colour))
            (dataset / split / "labels" / f"{stem}.txt").write_text(label, encoding="utf-8")
            paths[key] = image
        return root, paths

    def test_owned_image_labels_do_not_become_detection_boxes(self) -> None:
        source, source_sha, images = self.make_owned()
        candidates, counts, blockers = collect_owned(source, None, set())
        self.assertEqual(len(candidates), 1)
        self.assertFalse(candidates[0].positive)
        self.assertEqual(counts["skipped_positive_without_boxes"], 1)
        self.assertIn("needs box audit", blockers[0])

        audit = self.write_owned_audit(source_sha, images)
        candidates, counts, blockers = collect_owned(source, audit, set())
        self.assertEqual(len(candidates), 2)
        self.assertEqual(sum(candidate.positive for candidate in candidates), 1)
        self.assertEqual(counts["included_positive"], 1)
        self.assertEqual(blockers, [])

    def test_source_registry_pins_public_identity_and_label_limit(self) -> None:
        registry = json.loads((Path(__file__).parents[1] / "source_registry.json").read_text())
        rad = registry["sources"]["rad_v3"]
        self.assertEqual(rad["kaggle_ref"],
                         "rohitsuresh15/radroad-anomaly-detection")
        self.assertEqual(rad["dataset_version"], 3)
        self.assertEqual(rad["license_reported_by_kaggle"], "MIT")
        self.assertIn("not pothole ground truth", rad["label_limit"])

    def test_owned_export_without_rights_fails_closed(self) -> None:
        source, _, _ = self.make_owned()
        labels = json.loads((source / "labels.json").read_text())
        for entry in labels["images"]:
            entry.pop("licence")
        (source / "labels.json").write_text(json.dumps(labels), encoding="utf-8")
        with self.assertRaisesRegex(PipelineError, "training-rights"):
            collect_owned(source, None, set())

    def test_directory_source_digest_binds_referenced_image_bytes(self) -> None:
        source, original_digest, images = self.make_owned()
        self.assertEqual(original_digest, owned_source_digest(source))
        (source / "images/negative.jpg").write_bytes(self.image_bytes((20, 30, 40)))
        self.assertNotEqual(original_digest, owned_source_digest(source))
        (source / "images/negative.jpg").write_bytes(images["images/negative.jpg"])
        self.assertEqual(original_digest, owned_source_digest(source))

    def test_owned_images_without_capture_ids_share_one_leakage_group(self) -> None:
        source, _, _ = self.make_owned()
        labels = json.loads((source / "labels.json").read_text())
        for entry in labels["images"]:
            entry.pop("drive_id")
            entry["label"] = "undamaged"
        (source / "labels.json").write_text(json.dumps(labels), encoding="utf-8")
        candidates, _, blockers = collect_owned(source, None, set())
        self.assertEqual(blockers, [])
        self.assertEqual(len(candidates), 2)
        self.assertEqual(len({candidate.source_group for candidate in candidates}), 1)
        self.assertTrue(candidates[0].source_group.endswith(":ungrouped"))

    def test_owned_groups_are_stable_across_exports_and_nearby_views_coalesce(self) -> None:
        first, _, _ = self.make_owned()
        first_labels = json.loads((first / "labels.json").read_text())
        for entry in first_labels["images"]:
            entry["label"] = "undamaged"
            entry["lat"], entry["lng"] = 12.971600, 77.594600
        (first / "labels.json").write_text(json.dumps(first_labels), encoding="utf-8")

        second = self.root / "owned-second"
        (second / "images").mkdir(parents=True)
        second_image = self.image_bytes((40, 100, 160))
        (second / "images/view.jpg").write_bytes(second_image)
        (second / "labels.json").write_text(json.dumps({
            "images": [{
                "path": "images/view.jpg", "label": "undamaged",
                "labelled_by": "owner", "licence": "owner",
                "drive_id": "another-drive", "lat": 12.971650, "lng": 77.594650,
            }],
        }), encoding="utf-8")
        first_candidates, _, _ = collect_owned(first, None, set())
        second_candidates, _, _ = collect_owned(second, None, set())
        # A stable drive identifier is independent of export ZIP/directory bytes.
        duplicate = self.root / "owned-duplicate-export"
        shutil.copytree(first, duplicate)
        duplicate_labels = json.loads((duplicate / "labels.json").read_text())
        duplicate_labels["exported_at"] = "2026-09-06T00:00:00Z"
        (duplicate / "labels.json").write_text(
            json.dumps(duplicate_labels), encoding="utf-8")
        duplicate_candidates, _, _ = collect_owned(duplicate, None, set())
        self.assertEqual(first_candidates[0].source_group,
                         duplicate_candidates[0].source_group)

        output = self.root / "nearby-built"
        manifest = build_dataset(
            output, [*first_candidates, *second_candidates], {}, [],
            allow_incomplete=True)
        self.assertEqual(len({row["leakage_group"] for row in manifest["records"]}), 1)

    def test_rad_consumed_tree_can_be_pinned(self) -> None:
        root, paths = self.make_rad()
        candidates, _, _ = collect_rad(root, None, require_complete=False)
        pinned = candidates[0].source_corpus_sha256
        self.assertRegex(str(pinned), r"^[0-9a-f]{64}$")
        paths["speed"].write_bytes(self.image_bytes((10, 100, 210)))
        with self.assertRaisesRegex(PipelineError, "file-tree SHA-256"):
            collect_rad(root, None, require_complete=False,
                        expected_source_sha256=pinned)

    def test_rad_broad_damage_is_excluded_but_speed_bump_is_negative(self) -> None:
        root, paths = self.make_rad()
        candidates, counts, blockers = collect_rad(root, None, require_complete=False)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].source_item_id, "200_10-07-2023_mp4:2")
        self.assertFalse(candidates[0].positive)
        self.assertEqual(counts["skipped_broad_road_damage_without_audit"], 1)
        self.assertEqual(counts["included_speed_bump_negative"], 1)
        self.assertEqual(counts["skipped_non_target_public_frame"], 1)
        self.assertIn("no human-audited pothole boxes", blockers[0])

        audit = self.root / "rad-audit.json"
        audit.write_text(json.dumps({
            "schema_version": "rad-pothole-box-audit-v1",
            "dataset_ref": "rohitsuresh15/radroad-anomaly-detection",
            "dataset_version": 3,
            "review": {"human_verified": True, "reviewer": "Test reviewer",
                       "reviewed_at": "2026-09-05T00:00:00Z"},
            "frames": [{
                "frame_id": "100_10-07-2023_mp4:1",
                "image_sha256": sha256_file(paths["damage"]),
                "complete_frame_review": True,
                "pothole_boxes": [[0.5, 0.6, 0.2, 0.25]],
            }],
        }), encoding="utf-8")
        candidates, counts, blockers = collect_rad(root, audit, require_complete=False)
        self.assertEqual(len(candidates), 2)
        self.assertEqual(sum(candidate.positive for candidate in candidates), 1)
        self.assertEqual(counts["included_audited_positive"], 1)
        self.assertEqual(blockers, [])

    def test_split_groups_and_exact_duplicates_never_leak(self) -> None:
        same = self.image_bytes((30, 80, 130))
        different = self.image_bytes((130, 80, 30))
        positive_box = ((0.5, 0.5, 0.2, 0.2),)
        candidates = [
            Candidate("owned_app_export", "owned-a", "owned:drive-a",
                      sha256_bytes(same), positive_box, lambda: same,
                      source_corpus_sha256="a" * 64),
            Candidate("rad_v3", "rad-a", "rad:video-a",
                      sha256_bytes(same), positive_box, lambda: same,
                      source_corpus_sha256="b" * 64),
            Candidate("rad_v3", "rad-b", "rad:video-a",
                      sha256_bytes(different), (), lambda: different,
                      source_corpus_sha256="b" * 64),
        ]
        output = self.root / "built"
        manifest = build_dataset(output, candidates, {}, [], allow_incomplete=True)
        self.assertEqual(manifest["counts"]["exact_pixel_duplicates_removed"], 1)
        video_records = [row for row in manifest["records"]
                         if row["source_group"] == "rad:video-a"]
        self.assertEqual(len({row["split"] for row in video_records}), 1)
        self.assertEqual(len({row["split"] for row in manifest["records"]}), 1)
        verify_seal(manifest)
        self.assertFalse(manifest["release_ready"])
        self.assertEqual(manifest["release_minimums"], RELEASE_MINIMUMS)
        validate_prepared_dataset(output, manifest, require_release_ready=False)
        with self.assertRaisesRegex(PipelineError, "not release-ready"):
            validate_prepared_dataset(output, manifest)
        label = output / manifest["records"][0]["label"]
        self.assertTrue(label.read_text().startswith("0 "))
        image = output / manifest["records"][0]["image"]
        image.write_bytes(b"tampered-after-manifest")
        with self.assertRaisesRegex(PipelineError, "image bytes"):
            validate_prepared_dataset(output, manifest, require_release_ready=False)

    def test_prepared_dataset_is_relocatable(self) -> None:
        image = self.image_bytes((60, 90, 120))
        candidate = Candidate(
            "owned_app_export", "owned-relocatable", "owned:drive-relocatable",
            sha256_bytes(image), (), lambda: image,
            source_corpus_sha256="c" * 64,
        )
        original = self.root / "original-dataset"
        manifest = build_dataset(original, [candidate], {}, [], allow_incomplete=True)
        moved = self.root / "moved-dataset"
        original.rename(moved)
        validate_prepared_dataset(moved, manifest, require_release_ready=False)
        self.assertNotIn("path:", (moved / "dataset.yaml").read_text())

    def test_manifest_label_must_be_the_path_ultralytics_derives(self) -> None:
        first = self.image_bytes((20, 40, 60))
        second = self.image_bytes((80, 100, 120))
        candidates = [
            Candidate("owned_app_export", "first", "owned:shared-drive",
                      sha256_bytes(first), (), lambda: first,
                      source_corpus_sha256="a" * 64),
            Candidate("owned_app_export", "second", "owned:shared-drive",
                      sha256_bytes(second), (), lambda: second,
                      source_corpus_sha256="a" * 64),
        ]
        output = self.root / "label-path-built"
        manifest = build_dataset(output, candidates, {}, [], allow_incomplete=True)
        tampered = json.loads(json.dumps(manifest))
        tampered["records"][0]["label"], tampered["records"][1]["label"] = (
            tampered["records"][1]["label"], tampered["records"][0]["label"])
        tampered = sealed(tampered)
        with self.assertRaisesRegex(PipelineError, "Ultralytics derives"):
            validate_prepared_dataset(output, tampered, require_release_ready=False)

    def test_one_source_group_cannot_be_split_into_invented_components(self) -> None:
        first = self.image_bytes((30, 50, 70))
        second = self.image_bytes((90, 110, 130))
        candidates = [
            Candidate("owned_app_export", "first", "owned:drive-one",
                      sha256_bytes(first), (), lambda: first,
                      source_corpus_sha256="b" * 64),
            Candidate("owned_app_export", "second", "owned:drive-two",
                      sha256_bytes(second), (), lambda: second,
                      source_corpus_sha256="b" * 64),
        ]
        output = self.root / "source-group-built"
        manifest = build_dataset(output, candidates, {}, [], allow_incomplete=True)
        tampered = json.loads(json.dumps(manifest))
        tampered["records"][1]["source_group"] = tampered["records"][0]["source_group"]
        tampered = sealed(tampered)
        with self.assertRaisesRegex(PipelineError, "multiple leakage groups"):
            validate_prepared_dataset(output, tampered, require_release_ready=False)

    def test_corrupt_image_cannot_count_toward_dataset_floor(self) -> None:
        content = b"not-an-image"
        candidate = Candidate(
            "owned_app_export", "owned-corrupt", "owned:drive-corrupt",
            sha256_bytes(content), (), lambda: content,
            source_corpus_sha256="d" * 64,
        )
        with self.assertRaisesRegex(PipelineError, "decoded safely"):
            build_dataset(self.root / "corrupt-built", [candidate], {}, [],
                          allow_incomplete=True)

    def test_threshold_selection_uses_box_and_negative_image_constraints(self) -> None:
        rows = [
            {"truth_boxes": [[0.5, 0.5, 0.2, 0.2]], "predictions": [
                {"box": [0.5, 0.5, 0.2, 0.2], "confidence": 0.8},
            ]},
            {"truth_boxes": [], "predictions": [
                {"box": [0.1, 0.1, 0.1, 0.1], "confidence": 0.3},
            ]},
        ]
        threshold, metrics, passed = choose_threshold(rows, 1.0, 1.0, 0.0)
        self.assertTrue(passed)
        self.assertEqual(threshold, 0.8)
        self.assertEqual(metrics["box"]["true_positive"], 1)
        self.assertEqual(metrics["box"]["false_positive"], 0)
        self.assertAlmostEqual(box_iou([0.5, 0.5, 0.2, 0.2],
                                       [0.5, 0.5, 0.2, 0.2]), 1.0)
        scored = score_rows(rows, threshold)
        self.assertEqual(scored["image"]["negative_false_positive_rate"], 0.0)
        self.assertEqual(scored["image"]["precision"], 1.0)
        self.assertEqual(scored["image"]["recall"], 1.0)
        low_only = [
            {"truth_boxes": [[0.5, 0.5, 0.2, 0.2]], "predictions": [
                {"box": [0.5, 0.5, 0.2, 0.2], "confidence": 0.005},
            ]},
            {"truth_boxes": [], "predictions": []},
        ]
        selected, _, _ = choose_threshold(low_only, 1.0, 1.0, 0.0)
        self.assertGreaterEqual(selected, 0.01)

    def test_positive_image_recall_requires_a_localized_match(self) -> None:
        scored = score_rows([{
            "truth_boxes": [[0.2, 0.2, 0.1, 0.1]],
            "predictions": [{
                "box": [0.8, 0.8, 0.1, 0.1], "confidence": 0.95,
            }],
        }], threshold=0.5, iou_threshold=0.5)
        self.assertEqual(scored["box"]["true_positive"], 0)
        self.assertEqual(scored["box"]["false_positive"], 1)
        self.assertEqual(scored["box"]["false_negative"], 1)
        self.assertEqual(scored["image"]["true_positive"], 0)
        self.assertEqual(scored["image"]["false_negative"], 1)
        self.assertEqual(scored["image"]["positive_recall"], 0.0)

    def test_release_recomputes_validation_and_test_metrics_from_rows(self) -> None:
        rows = [
            {"truth_boxes": [[0.5, 0.5, 0.2, 0.2]], "predictions": [
                {"box": [0.5, 0.5, 0.2, 0.2], "confidence": 0.8},
            ]},
            {"truth_boxes": [], "predictions": [
                {"box": [0.2, 0.2, 0.1, 0.1], "confidence": 0.2},
            ]},
        ]
        constraints = {
            "minimum_box_recall": 0.9,
            "minimum_positive_image_recall": 0.9,
            "maximum_negative_image_false_positive_rate": 0.05,
        }
        selected, metrics, passed = choose_threshold(rows, 0.9, 0.9, 0.05, 0.5)
        self.assertTrue(passed)
        validation = {
            "rows": rows, "prediction_sha256": "v" * 64,
            "detection_contract": detection_api_contract(),
        }
        test = {
            "rows": rows, "prediction_sha256": "t" * 64,
            "detection_contract": detection_api_contract(),
        }
        threshold = {
            "constraints": constraints, "iou_threshold": 0.5,
            "selected_threshold": selected, "validation_metrics": metrics,
            "validation_prediction_sha256": "v" * 64,
            "threshold_receipt_sha256": "h" * 64, "gate_passed": True,
            "detection_contract": detection_api_contract(),
        }
        evaluation = {
            "selected_threshold": selected, "constraints": constraints,
            "test_metrics": score_rows(rows, selected, 0.5),
            "test_prediction_sha256": "t" * 64,
            "threshold_receipt_sha256": "h" * 64, "gate_passed": True,
            "detection_contract": detection_api_contract(),
        }
        verify_evaluation_receipts(
            validation_predictions=validation, test_predictions=test,
            threshold=threshold, evaluation=evaluation)
        evaluation["test_metrics"] = {
            **evaluation["test_metrics"],
            "box": {**evaluation["test_metrics"]["box"], "recall": 1.0},
        }
        # The fabricated value happens to equal the real value here; alter a count too.
        evaluation["test_metrics"]["box"]["true_positive"] = 999
        with self.assertRaisesRegex(PipelineError, "recomputed predictions"):
            verify_evaluation_receipts(
                validation_predictions=validation, test_predictions=test,
                threshold=threshold, evaluation=evaluation)

    def test_release_binds_prediction_rows_to_exact_sealed_split(self) -> None:
        dataset = {"records": [{
            "split": "validation", "source_item_id": "frame-1",
            "image_sha256": "a" * 64, "boxes": [[0.5, 0.5, 0.2, 0.2]],
            "capture_mode": "manual",
        }]}
        predictions = {"rows": [{
            "source_item_id": "frame-1", "image_sha256": "a" * 64,
            "truth_boxes": [[0.5, 0.5, 0.2, 0.2]], "capture_mode": "manual",
        }]}
        verify_prediction_split_coverage(predictions, dataset, "validation")
        predictions["rows"][0]["truth_boxes"] = []
        with self.assertRaisesRegex(PipelineError, "truth differs"):
            verify_prediction_split_coverage(predictions, dataset, "validation")

    def test_runtime_parity_compares_boxes_confidence_and_detection_sets(self) -> None:
        reference = [
            {"source_item_id": "a", "image_sha256": "1" * 64,
             "predictions": [{"box": [0.5, 0.5, 0.2, 0.2], "confidence": 0.80}]},
            {"source_item_id": "b", "image_sha256": "2" * 64, "predictions": []},
        ]
        runtime = [
            {"source_item_id": "a", "image_sha256": "1" * 64,
             "predictions": [{"box": [0.5, 0.5, 0.2, 0.2], "confidence": 0.81}]},
            {"source_item_id": "b", "image_sha256": "2" * 64, "predictions": []},
        ]
        metrics = compare_prediction_sets(
            reference, runtime, confidence_threshold=0.5,
            minimum_match_iou=0.98, maximum_confidence_delta=0.03)
        self.assertTrue(metrics["gate_passed"])
        self.assertEqual(metrics["matched_detections"], 1)
        self.assertAlmostEqual(metrics["minimum_observed_iou"], 1.0)

        runtime[0]["predictions"][0]["confidence"] = 0.86
        mismatch = compare_prediction_sets(
            reference, runtime, confidence_threshold=0.5,
            minimum_match_iou=0.98, maximum_confidence_delta=0.03)
        self.assertFalse(mismatch["gate_passed"])
        self.assertEqual(mismatch["violations"], 1)

        runtime[0]["predictions"] = []
        missing = compare_prediction_sets(
            reference, runtime, confidence_threshold=0.5,
            minimum_match_iou=0.98, maximum_confidence_delta=0.03)
        self.assertFalse(missing["gate_passed"])
        self.assertEqual(missing["unmatched_reference_detections"], 1)

    def test_runtime_parity_does_not_suppress_road_edge_boxes(self) -> None:
        edge_box = [0.04, 0.7, 0.08, 0.2]
        reference = [{
            "source_item_id": "edge", "image_sha256": "3" * 64,
            "truth_boxes": [edge_box],
            "predictions": [{"box": edge_box, "confidence": 0.9}],
        }]
        runtime = [{
            "source_item_id": "edge", "image_sha256": "3" * 64,
            "capture_mode": "drive",
            "runtime_verdict": {
                "image_quality": "acceptable", "assessment": "damaged",
                "damage_type": "pothole_cavity", "size": None,
                "description": "A cavity is visible at the road edge.",
            },
            "predictions": [{"box": edge_box, "confidence": 0.9}],
        }]
        metrics = compare_prediction_sets(
            reference, runtime, confidence_threshold=0.5,
            require_canonical_verdict=True,
        )
        self.assertTrue(metrics["gate_passed"])

    def test_ml_contract_matches_generated_canonical_detection_contract(self) -> None:
        repository = Path(__file__).resolve().parents[3]
        generated = json.loads(
            (repository / "llm/generated/contract.json").read_text(encoding="utf-8"))
        detection = generated["prompts"]["detection"]
        contract = detection_api_contract()
        self.assertEqual(contract["prompt_version"], detection["version"])
        self.assertEqual(contract["schema_version"], detection["schemaVersion"])
        self.assertEqual(
            contract["output_fields"], detection["schema"]["required"])
        self.assertEqual(
            generated["config"]["imaging"]["maxDetectionImages"], 1)
        self.assertEqual(set(detection["captureLayouts"]), {"manual", "drive"})

    def test_runtime_parity_tolerances_cannot_be_loosened_past_safe_bounds(self) -> None:
        rows = [{"source_item_id": "a", "image_sha256": "1" * 64,
                 "predictions": []}]
        with self.assertRaisesRegex(PipelineError, "may not be lower"):
            compare_prediction_sets(rows, rows, confidence_threshold=0.5,
                                    minimum_match_iou=0.90)
        with self.assertRaisesRegex(PipelineError, "may not exceed"):
            compare_prediction_sets(rows, rows, confidence_threshold=0.5,
                                    maximum_confidence_delta=0.10)

    def test_release_parity_gates_the_final_five_field_assessment(self) -> None:
        reference = [{
            "source_item_id": "positive",
            "image_sha256": "1" * 64,
            "truth_boxes": [[0.5, 0.5, 0.2, 0.2]],
            "predictions": [{"box": [0.5, 0.5, 0.2, 0.2], "confidence": 0.8}],
        }]
        runtime = [{
            "source_item_id": "positive",
            "image_sha256": "1" * 64,
            "capture_mode": "drive",
            "runtime_verdict": {
                "image_quality": "acceptable",
                "assessment": "damaged",
                "damage_type": "pothole_cavity",
                "size": None,
                "description": "A pothole cavity was detected.",
            },
            "predictions": [{"box": [0.5, 0.5, 0.2, 0.2], "confidence": 0.8}],
        }]
        passed = compare_prediction_sets(
            reference, runtime, confidence_threshold=0.5,
            require_canonical_verdict=True,
        )
        self.assertTrue(passed["gate_passed"])
        self.assertEqual(passed["canonical_decisions_compared"], 1)
        runtime[0]["runtime_verdict"] = {
            "image_quality": "acceptable",
            "assessment": "undamaged",
            "damage_type": None,
            "size": None,
            "description": "No pothole was detected.",
        }
        failed = compare_prediction_sets(
            reference, runtime, confidence_threshold=0.5,
            require_canonical_verdict=True,
        )
        self.assertFalse(failed["gate_passed"])
        self.assertEqual(failed["canonical_decision_mismatches"], 1)

        reference[0]["truth_boxes"] = []
        reference[0]["predictions"] = []
        runtime[0]["predictions"] = []
        runtime[0]["runtime_verdict"] = {
            "image_quality": "rejected",
            "assessment": "undamaged",
            "damage_type": None,
            "size": None,
            "description": "The image quality is insufficient.",
        }
        rejected_negative = compare_prediction_sets(
            reference, runtime, confidence_threshold=0.5,
            require_canonical_verdict=True,
        )
        self.assertFalse(rejected_negative["gate_passed"])
        self.assertEqual(rejected_negative["canonical_decision_mismatches"], 1)

    def test_runtime_verdict_is_exact_schema_v4_shape(self) -> None:
        valid = {
            "image_quality": "acceptable",
            "assessment": "damaged",
            "damage_type": "pothole_cavity",
            "size": None,
            "description": "A cavity is visible at the road edge.",
        }
        self.assertEqual(validate_runtime_verdict(valid, "test"), valid)
        invalid = {**valid, "assessment": "undamaged"}
        with self.assertRaisesRegex(PipelineError, "undamaged assessment"):
            validate_runtime_verdict(invalid, "test")
        self.assertEqual(detection_api_contract()["prompt_version"], "road-damage-v5")
        self.assertEqual(detection_api_contract()["schema_version"], 4)
        self.assertEqual(detection_api_contract()["input_images_per_request"], 1)

    def test_parity_stack_and_production_decode_limit_are_pinned(self) -> None:
        requirements = set(
            (Path(__file__).parents[1] / "requirements-train.txt")
            .read_text(encoding="utf-8").splitlines()
        )
        self.assertEqual(MAX_DECODED_PIXELS, 12_000_000)
        self.assertIn("numpy==1.26.4", requirements)
        self.assertIn("onnxruntime==1.19.2", requirements)
        self.assertIn("Pillow==11.3.0", requirements)
        self.assertEqual(expected_runtime_dependency_versions(), {
            "numpy": "1.26.4",
            "onnxruntime": "1.19.2",
            "Pillow": "11.3.0",
        })
        engines = required_training_engine_versions()
        self.assertEqual(engines["torch"], "2.8.0")
        self.assertEqual(engines["torchvision"], "0.23.0")
        self.assertEqual(engines["opencv-python"], "4.10.0.84")
        repository = Path(__file__).resolve().parents[3]
        release_dockerfile = (repository / "ml/yolo/Dockerfile.release").read_text()
        runtime_dockerfile = (repository / "infra/aws-yolo/Dockerfile").read_text()
        release_from = release_dockerfile.splitlines()[0]
        runtime_from = runtime_dockerfile.splitlines()[0]
        self.assertEqual(release_from, runtime_from)
        self.assertRegex(release_from, r"python:3\.12@sha256:[0-9a-f]{64}$")
        digest = release_from.rsplit("@", 1)[1]
        self.assertIn(f"POTHOLE_LAMBDA_BASE_IMAGE_DIGEST={digest}", release_dockerfile)
        self.assertIn(f"POTHOLE_LAMBDA_BASE_IMAGE_DIGEST={digest}", runtime_dockerfile)
        self.assertIn("mesa-libGL", release_dockerfile)
        self.assertIn("import cv2, ultralytics", release_dockerfile)

    def test_release_manifest_is_explicit_and_provenance_bound(self) -> None:
        dataset = {
            "class_names": {"0": "pothole"}, "release_ready": True,
            "manifest_sha256": "d" * 64, "source_counts": {}, "counts": {"splits": {}},
            "split_policy": {}, "provenance_policy": {},
            "release_minimums": RELEASE_MINIMUMS,
            "detection_contract": detection_api_contract(),
        }
        training = {
            "dataset_manifest_sha256": "d" * 64,
            "detection_contract": detection_api_contract(),
            "receipt_sha256": "r" * 64,
            "base_model": {"filename": "base.pt", "sha256": "b" * 64},
            "result": {"best_weights_sha256": "w" * 64},
            "training": {"image_size": 640},
            "environment": {"engine_versions": {"engine": "1.0"}},
        }
        quality_gate = quality_gate_contract()
        inference = {
            "minimum_confidence": 0.01, "nms_iou": 0.7,
            "agnostic_nms": True, "max_detections": 100, "rect": False,
            "device": "cpu",
        }
        constraints = {
            "minimum_box_recall": 0.90,
            "minimum_positive_image_recall": 0.90,
            "maximum_negative_image_false_positive_rate": 0.05,
        }
        passing_metrics = {
            "box": {"recall": 0.95},
            "image": {
                "positive_recall": 0.95,
                "negative_false_positive_rate": 0.04,
            },
        }
        threshold = {
            "gate_passed": True, "dataset_manifest_sha256": "d" * 64,
            "weights_sha256": "w" * 64, "threshold_receipt_sha256": "h" * 64,
            "validation_prediction_sha256": "v" * 64, "selected_threshold": 0.6,
            "iou_threshold": 0.5, "constraints": constraints,
            "validation_metrics": passing_metrics,
            "quality_gate": quality_gate,
            "prediction_inference": inference,
            "engine_environment": {"engine": "1.0"},
            "detection_contract": detection_api_contract(),
        }
        evaluation = {
            "gate_passed": True, "dataset_manifest_sha256": "d" * 64,
            "weights_sha256": "w" * 64, "threshold_receipt_sha256": "h" * 64,
            "evaluation_receipt_sha256": "e" * 64,
            "test_prediction_sha256": "t" * 64, "test_metrics": passing_metrics,
            "selected_threshold": 0.6, "constraints": constraints,
            "quality_gate": quality_gate,
            "prediction_inference": inference,
            "engine_environment": {"engine": "1.0"},
            "detection_contract": detection_api_contract(),
        }
        parity = {
            "schema_version": "pothole-yolo-runtime-parity-v1",
            "task": "pothole_detection",
            "gate_passed": True,
            "parity_receipt_sha256": "p" * 64,
            "onnx_sha256": "a" * 64,
            "weights_sha256": "w" * 64,
            "dataset_manifest_sha256": "d" * 64,
            "reference_prediction_sha256": "t" * 64,
            "runtime_module_sha256": "c" * 64,
            "runtime_environment": {
                "numpy": "1.26.4", "onnxruntime": "1.19.2", "Pillow": "11.3.0",
            },
            "runtime_execution": {
                "python_major_minor": "3.12",
                "python_implementation": "CPython",
                "system": "Linux",
                "lambda_architecture": "x86_64",
                "onnx_provider": "CPUExecutionProvider",
                "lambda_base_image_digest": "sha256:ab6df78b68b50723c93741bb7f9ea9f68c7cf3433359b33e4f37db5266567f9c",
            },
            "runtime_limits": {
                "maximum_decoded_pixels": 12_000_000,
                "input_images_per_request": 1,
            },
            "detection_contract": detection_api_contract(),
            "split": "test",
            "records_compared": 2,
            "decision": {
                "confidence_threshold": 0.6,
                "nms_iou_threshold": 0.7,
                "maximum_detections": 100,
            },
            "tolerances": {
                "minimum_matched_box_iou": 0.98,
                "maximum_absolute_confidence_delta": 0.03,
                "maximum_unmatched_reference_detections": 0,
                "maximum_unmatched_runtime_detections": 0,
                "maximum_canonical_decision_mismatches": 0,
            },
            "metrics": {
                "gate_passed": True, "violations": 0, "records_compared": 2,
                "canonical_decisions_compared": 2,
                "canonical_decision_mismatches": 0,
            },
        }
        manifest = build_model_manifest(
            model_version="pothole-yolo-v1", model_sha256="a" * 64,
            model_bytes=123, dataset=dataset, training=training,
            threshold=threshold, evaluation=evaluation, parity=parity,
        )
        self.assertEqual(manifest["task"], "pothole_detection")
        self.assertEqual(manifest["class_names"], {"0": "pothole"})
        self.assertEqual(manifest["image_size"], 640)
        self.assertEqual(manifest["export"], {
            "format": "onnx", "opset": 17, "dynamic": False, "simplify": True,
            "nms": False, "raw_ultralytics_output": True,
        })
        self.assertTrue(manifest["runtime_parity"]["gate_passed"])
        self.assertEqual(manifest["detection_contract"], detection_api_contract())
        self.assertEqual(manifest["runtime_parity"]["receipt_sha256"], "p" * 64)
        self.assertEqual(
            manifest["runtime_parity"]["runtime_limits"]["maximum_decoded_pixels"],
            12_000_000,
        )
        verify_seal(manifest, "model_manifest_sha256")
        bad = dict(dataset)
        bad["class_names"] = {"0": "road_damage"}
        with self.assertRaisesRegex(PipelineError, "class 0"):
            build_model_manifest(
                model_version="pothole-yolo-v1", model_sha256="a" * 64,
                model_bytes=123, dataset=bad, training=training,
                threshold=threshold, evaluation=evaluation, parity=parity,
            )
        failed_parity = dict(parity)
        failed_parity["gate_passed"] = False
        with self.assertRaisesRegex(PipelineError, "parity gate"):
            build_model_manifest(
                model_version="pothole-yolo-v1", model_sha256="a" * 64,
                model_bytes=123, dataset=dataset, training=training,
                threshold=threshold, evaluation=evaluation, parity=failed_parity,
            )
        undeployable_threshold = dict(threshold)
        undeployable_threshold["selected_threshold"] = 0.001
        with self.assertRaisesRegex(PipelineError, "cannot be deployed"):
            build_model_manifest(
                model_version="pothole-yolo-v1", model_sha256="a" * 64,
                model_bytes=123, dataset=dataset, training=training,
                threshold=undeployable_threshold, evaluation=evaluation, parity=parity,
            )
        weak_policy = dict(threshold)
        weak_policy["constraints"] = {
            **constraints, "minimum_box_recall": 0.0,
        }
        with self.assertRaisesRegex(PipelineError, "may not be weaker"):
            build_model_manifest(
                model_version="pothole-yolo-v1", model_sha256="a" * 64,
                model_bytes=123, dataset=dataset, training=training,
                threshold=weak_policy, evaluation=evaluation, parity=parity,
            )
        stale_quality = dict(threshold)
        stale_quality["quality_gate"] = {
            **quality_gate, "runtime_module_sha256": "0" * 64,
        }
        with self.assertRaisesRegex(PipelineError, "quality-gate"):
            build_model_manifest(
                model_version="pothole-yolo-v1", model_sha256="a" * 64,
                model_bytes=123, dataset=dataset, training=training,
                threshold=stale_quality, evaluation=evaluation, parity=parity,
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
