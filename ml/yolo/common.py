"""Small integrity helpers shared by the YOLO data and release tools."""

from __future__ import annotations

import hashlib
import importlib.metadata
import io
import json
import math
import re
import warnings
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence


HEX64 = re.compile(r"^[0-9a-f]{64}$")
SPLIT_SALT = "pothole-yolo-source-group-v1"
SPLIT_BUCKETS = (("train", 8000), ("validation", 9000), ("test", 10000))
CAPTURE_MODES = frozenset({"manual", "drive"})
CAPTURE_MODE_ORDER = ("manual", "drive")
DETECTION_PROMPT_VERSION = "road-damage-v5"
DETECTION_SCHEMA_VERSION = 4
DETECTION_OUTPUT_FIELDS = (
    "image_quality", "assessment", "damage_type", "size", "description",
)
DETECTION_IMAGE_QUALITIES = frozenset({"acceptable", "rejected"})
DETECTION_ASSESSMENTS = frozenset({"damaged", "undamaged"})
DETECTION_DAMAGE_TYPES = frozenset({
    "pothole_cavity", "failed_patch", "surface_breakup",
    "rut_or_depression", "other_road_damage",
})
DETECTION_SIZES = frozenset({"small", "medium", "large"})
MIN_DETECTION_CONFIDENCE = 0.01
MAX_DECODED_PIXELS = 12_000_000
# These are conservative release floors, not a claim of statistical sufficiency.
# They prevent a technically passing one-image/one-drive split from being shipped.
RELEASE_MINIMUMS = {
    "train": {
        "positive_images": 200,
        "negative_images": 200,
        "positive_groups": 20,
        "negative_groups": 20,
    },
    "validation": {
        "positive_images": 50,
        "negative_images": 50,
        "positive_groups": 10,
        "negative_groups": 10,
    },
    "test": {
        "positive_images": 50,
        "negative_images": 50,
        "positive_groups": 10,
        "negative_groups": 10,
    },
}
SPLIT_COUNT_FIELDS = (
    "images", "positive_images", "negative_images", "boxes",
    "groups", "positive_groups", "negative_groups",
)
TRAINING_ENGINE_DISTRIBUTIONS = frozenset({
    "ultralytics", "torch", "torchvision", "opencv-python", "numpy",
    "onnxruntime", "Pillow", "onnx", "onnxslim",
})


class PipelineError(ValueError):
    """Raised when data provenance or a release invariant is not satisfied."""


def detection_api_contract() -> dict[str, Any]:
    """Application contract that each released fallback model must implement."""
    return {
        "prompt_version": DETECTION_PROMPT_VERSION,
        "schema_version": DETECTION_SCHEMA_VERSION,
        "input_images_per_request": 1,
        "capture_modes": list(CAPTURE_MODE_ORDER),
        "output_fields": list(DETECTION_OUTPUT_FIELDS),
        "image_quality": ["acceptable", "rejected"],
        "assessment": ["damaged", "undamaged"],
        "damage_type": [
            "pothole_cavity", "failed_patch", "surface_breakup",
            "rut_or_depression", "other_road_damage", None,
        ],
        "size": ["small", "medium", "large", None],
    }


def canonical_json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def required_training_engine_versions() -> dict[str, str]:
    """Read exact execution-engine pins from the reviewed training requirements."""
    requirements = Path(__file__).with_name("requirements-train.txt")
    versions: dict[str, str] = {}
    try:
        lines = requirements.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise PipelineError(f"cannot read training requirements: {error}") from error
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^=\s]+)", line)
        if not match:
            raise PipelineError("training requirements must use exact version pins")
        distribution, version = match.groups()
        if distribution in versions:
            raise PipelineError(f"duplicate training dependency pin: {distribution}")
        versions[distribution] = version
    missing = TRAINING_ENGINE_DISTRIBUTIONS - set(versions)
    if missing:
        raise PipelineError(
            "training engine pins are incomplete: " + ", ".join(sorted(missing)))
    return {name: versions[name] for name in sorted(TRAINING_ENGINE_DISTRIBUTIONS)}


def installed_training_engine_versions() -> dict[str, str]:
    required = required_training_engine_versions()
    installed: dict[str, str] = {}
    for distribution, expected in required.items():
        try:
            actual = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError as error:
            raise PipelineError(f"training engine dependency is missing: {distribution}") from error
        if actual != expected:
            raise PipelineError(
                f"training engine requires {distribution}=={expected}, found {actual}")
        installed[distribution] = actual
    return installed


def sealed(value: Mapping[str, Any], field: str = "manifest_sha256") -> dict[str, Any]:
    result = dict(value)
    result.pop(field, None)
    result[field] = sha256_bytes(canonical_json_bytes(result))
    return result


def verify_seal(value: Mapping[str, Any], field: str = "manifest_sha256") -> None:
    expected = value.get(field)
    if not isinstance(expected, str) or not HEX64.fullmatch(expected):
        raise PipelineError(f"missing or malformed {field}")
    payload = dict(value)
    payload.pop(field, None)
    actual = sha256_bytes(canonical_json_bytes(payload))
    if actual != expected:
        raise PipelineError(f"{field} does not match content")


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise PipelineError(f"cannot read {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise PipelineError(f"invalid JSON in {path}: {error}") from error


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value))


def split_for_group(group_id: str) -> str:
    digest = hashlib.sha256(f"{SPLIT_SALT}\0{group_id}".encode("utf-8")).digest()
    bucket = int.from_bytes(digest[:8], "big") % 10000
    for name, upper in SPLIT_BUCKETS:
        if bucket < upper:
            return name
    raise AssertionError("split buckets do not cover the hash space")


def normalise_box(values: Sequence[Any], where: str) -> tuple[float, float, float, float]:
    if not isinstance(values, (list, tuple)) or len(values) != 4:
        raise PipelineError(f"{where}: expected [x_center,y_center,width,height]")
    try:
        x_center, y_center, width, height = (float(value) for value in values)
    except (TypeError, ValueError) as error:
        raise PipelineError(f"{where}: box contains a non-number") from error
    coordinates = (x_center, y_center, width, height)
    if not all(math.isfinite(value) for value in coordinates):
        raise PipelineError(f"{where}: box contains a non-finite value")
    if not all(0.0 <= value <= 1.0 for value in coordinates):
        raise PipelineError(f"{where}: box coordinates must be normalised to [0,1]")
    if width <= 0.0 or height <= 0.0:
        raise PipelineError(f"{where}: box must have positive width and height")
    tolerance = 1e-6
    if (x_center - width / 2 < -tolerance
            or x_center + width / 2 > 1 + tolerance
            or y_center - height / 2 < -tolerance
            or y_center + height / 2 > 1 + tolerance):
        raise PipelineError(f"{where}: box extends outside the image")
    return coordinates


def format_yolo_box(box: Sequence[float]) -> str:
    return "0 " + " ".join(f"{value:.8f}".rstrip("0").rstrip(".") for value in box)


def ensure_unique(values: Iterable[str], what: str) -> None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            raise PipelineError(f"duplicate {what}: {value}")
        seen.add(value)


def validate_decodable_image(
    content: bytes,
    where: str,
    *,
    max_decoded_pixels: int = MAX_DECODED_PIXELS,
) -> tuple[int, int]:
    """Fully decode one bounded image so corrupt files cannot count toward floors."""
    try:
        from PIL import Image, ImageOps
    except ImportError as error:
        raise PipelineError(
            "Pillow is required to validate training images; install requirements-train.txt"
        ) from error
    previous_limit = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = max_decoded_pixels
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as source:
                width, height = source.size
                if width < 1 or height < 1 or width * height > max_decoded_pixels:
                    raise PipelineError(f"{where}: decoded image exceeds the pixel limit")
                source.load()
                decoded = ImageOps.exif_transpose(source).convert("RGB")
                decoded.load()
    except PipelineError:
        raise
    except (OSError, SyntaxError, Image.DecompressionBombError,
            Image.DecompressionBombWarning) as error:
        raise PipelineError(f"{where}: image cannot be decoded safely") from error
    finally:
        Image.MAX_IMAGE_PIXELS = previous_limit
    return width, height


def _prepared_path(root: Path, value: Any, where: str) -> tuple[str, Path]:
    if not isinstance(value, str) or not value:
        raise PipelineError(f"{where}: path is missing")
    relative = PurePosixPath(value)
    if relative.is_absolute() or any(part in ("", ".", "..") for part in relative.parts):
        raise PipelineError(f"{where}: unsafe path {value!r}")
    target = root.joinpath(*relative.parts)
    try:
        target.resolve().relative_to(root)
    except (OSError, ValueError) as error:
        raise PipelineError(f"{where}: path escapes the dataset") from error
    if target.is_symlink():
        raise PipelineError(f"{where}: symlinks are not allowed in a sealed dataset")
    return relative.as_posix(), target


def validate_prepared_dataset(
    root: Path,
    manifest: Mapping[str, Any],
    *,
    require_release_ready: bool = True,
) -> None:
    """Verify that Ultralytics will consume exactly the bytes sealed by the manifest."""
    root = root.resolve()
    if not isinstance(manifest, dict) or manifest.get("schema_version") != "pothole-yolo-dataset-v1":
        raise PipelineError("unsupported dataset manifest")
    verify_seal(manifest)
    release_ready = manifest.get("release_ready")
    blockers = manifest.get("blockers")
    if not isinstance(release_ready, bool) or not isinstance(blockers, list) or not all(
            isinstance(item, str) and item for item in blockers):
        raise PipelineError("dataset release state is malformed")
    if len(blockers) != len(set(blockers)):
        raise PipelineError("dataset blockers are duplicated")
    if manifest.get("class_names") != {"0": "pothole"}:
        raise PipelineError("dataset is not the required single-class pothole contract")
    if manifest.get("image_size") != 640:
        raise PipelineError("dataset image-size contract is not 640")
    if manifest.get("detection_contract") != detection_api_contract():
        raise PipelineError("dataset detection API contract is missing or stale")
    if manifest.get("release_minimums") != RELEASE_MINIMUMS:
        raise PipelineError("dataset release minimums differ from the safety policy")
    registry_path = Path(__file__).with_name("source_registry.json")
    if manifest.get("source_registry_sha256") != sha256_file(registry_path):
        raise PipelineError("dataset source-registry hash differs from the reviewed registry")
    if require_release_ready and not release_ready:
        raise PipelineError("dataset is not release-ready: " + "; ".join(blockers))
    records = manifest.get("records")
    if not isinstance(records, list) or not records:
        raise PipelineError("dataset manifest has no records")

    expected_images: set[str] = set()
    expected_labels: set[str] = set()
    source_ids: set[str] = set()
    image_hashes: set[str] = set()
    split_counts = {name: Counter() for name in ("train", "validation", "test")}
    all_groups = {name: set() for name in split_counts}
    positive_groups = {name: set() for name in split_counts}
    negative_groups = {name: set() for name in split_counts}
    group_splits: dict[str, str] = {}
    source_group_components: dict[str, str] = {}
    source_corpora: dict[str, set[str]] = {}
    for index, record in enumerate(records):
        where = f"manifest.records[{index}]"
        if not isinstance(record, dict):
            raise PipelineError(f"{where}: expected an object")
        source_id = record.get("source_item_id")
        if not isinstance(source_id, str) or not source_id or source_id in source_ids:
            raise PipelineError(f"{where}: source_item_id is missing or duplicated")
        source_ids.add(source_id)
        split = record.get("split")
        if split not in split_counts:
            raise PipelineError(f"{where}: invalid split")
        disk_split = "val" if split == "validation" else split
        image_relative, image_path = _prepared_path(root, record.get("image"), f"{where}.image")
        label_relative, label_path = _prepared_path(root, record.get("label"), f"{where}.label")
        if not image_relative.startswith(f"images/{disk_split}/"):
            raise PipelineError(f"{where}: image path does not match its split")
        if not label_relative.startswith(f"labels/{disk_split}/"):
            raise PipelineError(f"{where}: label path does not match its split")
        image_parts = PurePosixPath(image_relative).parts
        derived_label = PurePosixPath(
            "labels", *image_parts[1:]).with_suffix(".txt").as_posix()
        if label_relative != derived_label:
            raise PipelineError(
                f"{where}: label path does not match the path Ultralytics derives "
                "from the image")
        if image_relative in expected_images or label_relative in expected_labels:
            raise PipelineError(f"{where}: prepared path is duplicated")
        expected_images.add(image_relative)
        expected_labels.add(label_relative)
        if not image_path.is_file() or not label_path.is_file():
            raise PipelineError(f"{where}: prepared image or label is missing")
        expected_hash = record.get("image_sha256")
        if not isinstance(expected_hash, str) or not HEX64.fullmatch(expected_hash):
            raise PipelineError(f"{where}: image SHA-256 is malformed")
        if expected_hash in image_hashes:
            raise PipelineError(f"{where}: exact duplicate image bytes were not removed")
        image_hashes.add(expected_hash)
        if sha256_file(image_path) != expected_hash:
            raise PipelineError(f"{where}: image bytes do not match the sealed manifest")
        source_corpus_sha256 = record.get("source_corpus_sha256")
        if (not isinstance(source_corpus_sha256, str)
                or not HEX64.fullmatch(source_corpus_sha256)):
            raise PipelineError(f"{where}: source corpus SHA-256 is missing or malformed")
        source = record.get("source")
        if not isinstance(source, str) or not source:
            raise PipelineError(f"{where}: source is missing")
        source_corpora.setdefault(source, set()).add(source_corpus_sha256)
        try:
            image_content = image_path.read_bytes()
        except OSError as error:
            raise PipelineError(f"{where}: image bytes are unreadable") from error
        validate_decodable_image(image_content, where)
        source_group = record.get("source_group")
        if not isinstance(source_group, str) or not source_group:
            raise PipelineError(f"{where}: source_group is missing")
        leakage_group = record.get("leakage_group")
        if not isinstance(leakage_group, str) or not leakage_group:
            raise PipelineError(f"{where}: leakage_group is missing")
        prior_component = source_group_components.setdefault(
            source_group, leakage_group)
        if prior_component != leakage_group:
            raise PipelineError(
                f"{where}: one source group maps to multiple leakage groups")
        prior_split = group_splits.setdefault(leakage_group, split)
        if prior_split != split or split_for_group(leakage_group) != split:
            raise PipelineError(f"{where}: leakage group does not match its deterministic split")
        capture_mode = record.get("capture_mode")
        if capture_mode not in CAPTURE_MODES:
            raise PipelineError(f"{where}: capture_mode is invalid")
        raw_boxes = record.get("boxes")
        if not isinstance(raw_boxes, list):
            raise PipelineError(f"{where}: boxes must be an array")
        boxes = [normalise_box(box, f"{where}.boxes[{box_index}]")
                 for box_index, box in enumerate(raw_boxes)]
        if record.get("positive") is not bool(boxes):
            raise PipelineError(f"{where}: positive flag contradicts boxes")
        expected_label = "\n".join(format_yolo_box(box) for box in boxes)
        if expected_label:
            expected_label += "\n"
        try:
            actual_label = label_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise PipelineError(f"{where}: label is unreadable") from error
        if actual_label != expected_label:
            raise PipelineError(f"{where}: label bytes do not match the sealed boxes")
        split_counts[split]["images"] += 1
        split_counts[split]["positive_images" if boxes else "negative_images"] += 1
        split_counts[split]["boxes"] += len(boxes)
        all_groups[split].add(leakage_group)
        (positive_groups if boxes else negative_groups)[split].add(leakage_group)

    for split, counts in split_counts.items():
        counts["groups"] = len(all_groups[split])
        counts["positive_groups"] = len(positive_groups[split])
        counts["negative_groups"] = len(negative_groups[split])
        for field in SPLIT_COUNT_FIELDS:
            counts.setdefault(field, 0)

    for directory_name, expected in (
        ("images", expected_images), ("labels", expected_labels)
    ):
        directory = root / directory_name
        if not directory.is_dir() or directory.is_symlink():
            raise PipelineError(f"prepared dataset {directory_name}/ directory is missing or unsafe")
        actual: set[str] = set()
        for path in directory.rglob("*"):
            if path.is_symlink():
                raise PipelineError("symlinks are not allowed in a sealed dataset")
            if path.is_file():
                actual.add(path.relative_to(root).as_posix())
        if actual != expected:
            raise PipelineError(
                f"prepared {directory_name}/ files differ from the sealed manifest")

    expected_yaml = (
        "train: images/train\n"
        "val: images/val\n"
        "test: images/test\n"
        "names:\n"
        "  0: pothole\n"
    )
    yaml_path = root / "dataset.yaml"
    try:
        actual_yaml = yaml_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise PipelineError("dataset.yaml is missing or unreadable") from error
    if yaml_path.is_symlink() or actual_yaml != expected_yaml:
        raise PipelineError("dataset.yaml does not match the sealed single-class paths")
    recorded_counts = manifest.get("counts", {}).get("splits")
    if recorded_counts != {name: dict(count) for name, count in split_counts.items()}:
        raise PipelineError("dataset split counts do not match the sealed records")
    expected_corpora = {
        source: sorted(digests) for source, digests in sorted(source_corpora.items())
    }
    if manifest.get("source_corpora") != expected_corpora:
        raise PipelineError("dataset source corpus identities do not match its records")
    minimum_failures = []
    for split, required in RELEASE_MINIMUMS.items():
        for field, minimum in required.items():
            actual = split_counts[split][field]
            if actual < minimum:
                minimum_failures.append(
                    f"{split} has {actual} {field}; requires at least {minimum}")
    if any(item not in blockers for item in minimum_failures):
        raise PipelineError("dataset blockers omit a release-minimum failure")
    if release_ready != (not blockers):
        raise PipelineError("dataset release_ready flag contradicts its blockers")
    if release_ready and minimum_failures:
        raise PipelineError("dataset is marked release-ready below the safety minimums")
