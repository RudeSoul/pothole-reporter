#!/usr/bin/env python3
"""Build a leakage-safe, pothole-only Ultralytics detection dataset.

The app's export is image-classification data and RAD's ``RoadDamages`` box is a
broad anomaly label.  This tool therefore accepts a positive training box only
from a complete, human-verified box audit.  It can consume owner-verified
``undamaged`` app images and pure RAD SpeedBump frames as hard negatives.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import math
import os
import re
import shutil
import tempfile
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping, Optional

try:
    from .common import (
        PipelineError, RELEASE_MINIMUMS, SPLIT_COUNT_FIELDS,
        detection_api_contract,
        ensure_unique,
        format_yolo_box,
        load_json,
        normalise_box,
        sealed,
        sha256_bytes,
        sha256_file,
        split_for_group,
        validate_decodable_image,
        write_json,
    )
except ImportError:  # Allow ``python ml/yolo/prepare_dataset.py``.
    from common import (  # type: ignore
        PipelineError, RELEASE_MINIMUMS, SPLIT_COUNT_FIELDS,
        detection_api_contract,
        ensure_unique,
        format_yolo_box,
        load_json,
        normalise_box,
        sealed,
        sha256_bytes,
        sha256_file,
        split_for_group,
        validate_decodable_image,
        write_json,
    )


RAD_REF = "rohitsuresh15/radroad-anomaly-detection"
RAD_VERSION = 3
RAD_CLASSES = (
    "HMV", "LMV", "Pedestrian", "RoadDamages", "SpeedBump", "UnsurfacedRoad",
)
RAD_EXPECTED = {
    "images": 8394,
    "labels": 8394,
    "boxes": 29941,
    "class_boxes_HMV": 4431,
    "class_boxes_LMV": 14392,
    "class_boxes_Pedestrian": 3563,
    "road_damage_boxes": 6463,
    "speed_bump_boxes": 499,
    "class_boxes_UnsurfacedRoad": 593,
    "road_damage_frames": 3184,
    "speed_bump_frames": 439,
    "pure_speed_bump_frames": 341,
    "road_damage_and_speed_bump_frames": 98,
    "empty_labels": 91,
}
RAD_IMAGE_RE = re.compile(
    r"^(?P<source>.+)_mp4-(?P<frame>[0-9]+)_jpg\.rf\."
    r"(?P<variant>[A-Za-z0-9]+)\.(?:jpg|jpeg)$",
    re.IGNORECASE,
)
RAD_AMBIGUOUS_SOURCES = frozenset(
    f"{number:02d}_13-06-2023_mp4" for number in range(1, 30)
)
POSITIVE_OWNED_LABELS = frozenset({
    "pothole_cavity", "failed_patch", "surface_breakup",
    "rut_or_depression", "other_road_damage", "pothole",
})
NEGATIVE_OWNED_LABELS = frozenset({"undamaged", "not_pothole"})
IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp"})
OWNED_GEO_LEAKAGE_RADIUS_M = 25.0
EARTH_RADIUS_M = 6_371_000.0


@dataclass
class Candidate:
    source: str
    source_item_id: str
    source_group: str
    image_sha256: str
    boxes: tuple[tuple[float, float, float, float], ...]
    read_bytes: Callable[[], bytes]
    capture_mode: str = "manual"
    suffix: str = ".jpg"
    annotation_basis: str = "unspecified"
    source_license: str = "unspecified"
    audit_sha256: Optional[str] = None
    source_corpus_sha256: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None

    @property
    def positive(self) -> bool:
        return bool(self.boxes)


class UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, item: str) -> str:
        self.parent.setdefault(item, item)
        if self.parent[item] != item:
            self.parent[item] = self.find(self.parent[item])
        return self.parent[item]

    def union(self, left: str, right: str) -> None:
        left_root, right_root = self.find(left), self.find(right)
        if left_root == right_root:
            return
        keep, replace = sorted((left_root, right_root))
        self.parent[replace] = keep


def _safe_member(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or any(
            part in ("", ".", "..") for part in path.parts):
        raise PipelineError(f"unsafe archive path: {name!r}")
    return path


def _parse_audit(path: Optional[Path], schema: str, source_sha: Optional[str] = None,
                 frame_key: str = "path") -> dict[str, dict[str, Any]]:
    if path is None:
        return {}
    value = load_json(path)
    if not isinstance(value, dict) or value.get("schema_version") != schema:
        raise PipelineError(f"{path}: expected {schema}")
    review = value.get("review")
    if not isinstance(review, dict) or review.get("human_verified") is not True:
        raise PipelineError(f"{path}: audit must explicitly say human_verified=true")
    reviewer = str(review.get("reviewer", "")).strip()
    reviewed_at = str(review.get("reviewed_at", "")).strip()
    if not reviewer or reviewer.startswith("REPLACE_") or "YYYY" in reviewed_at:
        raise PipelineError(f"{path}: audit needs a real reviewer and reviewed_at value")
    if source_sha is not None and value.get("source_sha256") != source_sha:
        raise PipelineError(f"{path}: source_sha256 does not match the supplied source")
    frames = value.get("frames")
    if not isinstance(frames, list):
        raise PipelineError(f"{path}: frames must be an array")
    result: dict[str, dict[str, Any]] = {}
    for index, frame in enumerate(frames):
        where = f"{path}:frames[{index}]"
        if not isinstance(frame, dict):
            raise PipelineError(f"{where}: expected an object")
        key = frame.get(frame_key)
        if not isinstance(key, str) or not key:
            raise PipelineError(f"{where}: missing {frame_key}")
        if key in result:
            raise PipelineError(f"{where}: duplicate {frame_key} {key!r}")
        if frame.get("complete_frame_review") is not True:
            raise PipelineError(f"{where}: complete_frame_review must be true")
        image_hash = frame.get("image_sha256")
        if not isinstance(image_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", image_hash):
            raise PipelineError(f"{where}: image_sha256 is missing or malformed")
        raw_boxes = frame.get("pothole_boxes")
        if not isinstance(raw_boxes, list):
            raise PipelineError(f"{where}: pothole_boxes must be an array")
        boxes = tuple(normalise_box(box, f"{where}.pothole_boxes[{box_index}]")
                      for box_index, box in enumerate(raw_boxes))
        result[key] = {**frame, "pothole_boxes": boxes}
    return result


def _load_rights(path: Optional[Path]) -> set[str]:
    if path is None:
        return set()
    value = load_json(path)
    if not isinstance(value, dict) or value.get("schema_version") != "owned-training-rights-v1":
        raise PipelineError(f"{path}: expected owned-training-rights-v1")
    sources = value.get("sources")
    if not isinstance(sources, list):
        raise PipelineError(f"{path}: sources must be an array")
    approved: set[str] = set()
    for index, source in enumerate(sources):
        if not isinstance(source, dict):
            raise PipelineError(f"{path}:sources[{index}] must be an object")
        digest = source.get("source_sha256")
        holder = str(source.get("rights_holder", "")).strip()
        if (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or not holder or source.get("authorized_for_model_training") is not True):
            raise PipelineError(
                f"{path}:sources[{index}] needs a hash, rights holder and training authorization")
        approved.add(digest)
    return approved


def _directory_image_path(source_path: Path, name: str) -> Path:
    relative = _safe_member(name)
    root = source_path.resolve()
    candidates = [
        source_path.joinpath(*relative.parts),
        source_path.joinpath("images", *relative.parts),
    ]
    target = next((candidate for candidate in candidates if candidate.is_file()), candidates[0])
    try:
        target.resolve().relative_to(root)
    except (OSError, ValueError) as error:
        raise PipelineError(f"owned image escapes source root: {name}") from error
    cursor = target
    while cursor != source_path.parent and cursor != source_path:
        if cursor.is_symlink():
            raise PipelineError(f"owned source may not contain symlinks: {name}")
        cursor = cursor.parent
    if not target.is_file():
        raise PipelineError(f"owned source is missing referenced image: {name}")
    return target


def _digest_part(digest: Any, value: bytes) -> None:
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


def _stable_owned_identifier(value: Any, field: str, where: str) -> Optional[str]:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise PipelineError(f"{where}: {field} must be a string or integer")
    normalized = str(value).strip()
    if not normalized or len(normalized) > 256:
        raise PipelineError(f"{where}: {field} must contain 1 to 256 characters")
    return sha256_bytes(normalized.encode("utf-8"))


def _owned_location(entry: Mapping[str, Any], where: str) -> tuple[Optional[float], Optional[float]]:
    raw_latitude, raw_longitude = entry.get("lat"), entry.get("lng")
    if raw_latitude is None and raw_longitude is None:
        return None, None
    if (raw_latitude is None or raw_longitude is None
            or isinstance(raw_latitude, bool) or isinstance(raw_longitude, bool)):
        raise PipelineError(f"{where}: lat and lng must be supplied together")
    try:
        latitude, longitude = float(raw_latitude), float(raw_longitude)
    except (TypeError, ValueError) as error:
        raise PipelineError(f"{where}: lat or lng is not numeric") from error
    if (not math.isfinite(latitude) or not math.isfinite(longitude)
            or not -90 <= latitude <= 90 or not -180 <= longitude <= 180):
        raise PipelineError(f"{where}: lat or lng is outside its valid range")
    return latitude, longitude


def _metres_between(left: Candidate, right: Candidate) -> float:
    assert left.latitude is not None and left.longitude is not None
    assert right.latitude is not None and right.longitude is not None
    radians = math.pi / 180.0
    delta_latitude = (right.latitude - left.latitude) * radians
    delta_longitude = (right.longitude - left.longitude) * radians
    value = (
        math.sin(delta_latitude / 2) ** 2
        + math.cos(left.latitude * radians) * math.cos(right.latitude * radians)
        * math.sin(delta_longitude / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(value)))


def _earth_cell(candidate: Candidate) -> tuple[int, int, int]:
    """Index nearby points in 3-D Earth-centred cells without longitude edge cases."""
    assert candidate.latitude is not None and candidate.longitude is not None
    latitude = candidate.latitude * math.pi / 180.0
    longitude = candidate.longitude * math.pi / 180.0
    coordinates = (
        EARTH_RADIUS_M * math.cos(latitude) * math.cos(longitude),
        EARTH_RADIUS_M * math.cos(latitude) * math.sin(longitude),
        EARTH_RADIUS_M * math.sin(latitude),
    )
    return tuple(math.floor(value / OWNED_GEO_LEAKAGE_RADIUS_M)
                 for value in coordinates)  # type: ignore[return-value]


def _owned_directory_sha256(source_path: Path, labels: Mapping[str, Any]) -> str:
    """Bind directory-source identity to labels plus every referenced image byte."""
    if source_path.is_symlink():
        raise PipelineError("owned source directory may not be a symlink")
    images = labels.get("images")
    if not isinstance(images, list):
        raise PipelineError(f"{source_path}: labels.json needs an images array")
    names: list[str] = []
    for index, entry in enumerate(images):
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise PipelineError(f"{source_path}:images[{index}] needs a path")
        name = entry["path"]
        _safe_member(name)
        names.append(name)
    ensure_unique(names, "owned image path")
    labels_path = source_path / "labels.json"
    if labels_path.is_symlink():
        raise PipelineError("owned labels.json may not be a symlink")
    try:
        labels_bytes = labels_path.read_bytes()
    except OSError as error:
        raise PipelineError(f"cannot read {labels_path}: {error}") from error
    digest = hashlib.sha256()
    _digest_part(digest, b"pothole-owned-directory-v1")
    _digest_part(digest, b"labels.json")
    _digest_part(digest, labels_bytes)
    for name in sorted(names):
        _digest_part(digest, name.encode("utf-8"))
        try:
            content = _directory_image_path(source_path, name).read_bytes()
        except OSError as error:
            raise PipelineError(f"cannot read owned image {name}: {error}") from error
        _digest_part(digest, content)
    return digest.hexdigest()


def owned_source_digest(source_path: Path) -> str:
    """Return the source identity used by box audits and rights attestations."""
    if source_path.is_file() and source_path.suffix.lower() == ".zip":
        return sha256_file(source_path)
    labels_path = source_path / "labels.json"
    labels = load_json(labels_path)
    if not isinstance(labels, dict):
        raise PipelineError(f"{labels_path}: expected a JSON object")
    return _owned_directory_sha256(source_path, labels)


def _owned_source(source_path: Path) -> tuple[str, Mapping[str, Any], Callable[[str], bytes]]:
    if source_path.is_file() and source_path.suffix.lower() == ".zip":
        source_sha = sha256_file(source_path)
        try:
            archive = zipfile.ZipFile(source_path)
        except (OSError, zipfile.BadZipFile) as error:
            raise PipelineError(f"cannot open owned export {source_path}: {error}") from error
        names = archive.namelist()
        for name in names:
            _safe_member(name)
        ensure_unique(names, "archive member")
        if "labels.json" not in names:
            archive.close()
            raise PipelineError(f"{source_path}: labels.json is missing")
        try:
            labels = __import__("json").loads(archive.read("labels.json"))
        except Exception as error:
            archive.close()
            raise PipelineError(f"{source_path}: labels.json is invalid") from error

        def reader(name: str, arc: zipfile.ZipFile = archive) -> bytes:
            _safe_member(name)
            try:
                return arc.read(name)
            except KeyError as error:
                raise PipelineError(f"{source_path}: missing {name}") from error

        return source_sha, labels, reader
    labels_path = source_path / "labels.json"
    if not labels_path.is_file():
        raise PipelineError(
            f"owned source must be an app ZIP or directory containing labels.json: {source_path}")
    labels = load_json(labels_path)
    if not isinstance(labels, dict):
        raise PipelineError(f"{labels_path}: expected a JSON object")
    source_sha = _owned_directory_sha256(source_path, labels)

    def reader(name: str) -> bytes:
        target = _directory_image_path(source_path, name)
        try:
            return target.read_bytes()
        except OSError as error:
            raise PipelineError(f"cannot read owned image {target}: {error}") from error

    return source_sha, labels, reader


def collect_owned(source_path: Path, audit_path: Optional[Path],
                  rights_sha: set[str]) -> tuple[list[Candidate], Counter[str], list[str]]:
    source_sha, labels, reader = _owned_source(source_path)
    if not isinstance(labels, dict) or not isinstance(labels.get("images"), list):
        raise PipelineError(f"{source_path}: labels.json needs an images array")
    audit = _parse_audit(audit_path, "owned-pothole-box-audit-v1", source_sha)
    audit_sha = sha256_file(audit_path) if audit_path is not None else None
    candidates: list[Candidate] = []
    counts: Counter[str] = Counter()
    blockers: list[str] = []
    entries_by_path: dict[str, Mapping[str, Any]] = {}
    for index, entry in enumerate(labels["images"]):
        where = f"{source_path}:images[{index}]"
        if not isinstance(entry, dict):
            raise PipelineError(f"{where}: expected an object")
        name = entry.get("path")
        if not isinstance(name, str):
            raise PipelineError(f"{where}: path is missing")
        _safe_member(name)
        if name in entries_by_path:
            raise PipelineError(f"{where}: duplicate image path {name!r}")
        entries_by_path[name] = entry
        counts["entries"] += 1
        if str(entry.get("labelled_by", "")).strip().lower() != "owner":
            counts["skipped_not_owner_verified"] += 1
            continue
        licence = str(entry.get("licence", "")).strip().lower()
        if licence != "owner" and source_sha not in rights_sha:
            raise PipelineError(
                f"{where}: no explicit owner licence or matching training-rights attestation")
        raw = reader(name)
        image_hash = sha256_bytes(raw)
        suffix = Path(name).suffix.lower()
        if suffix not in IMAGE_SUFFIXES:
            raise PipelineError(f"{where}: unsupported image suffix {suffix!r}")
        label = str(entry.get("label", "")).strip().lower()
        pothole_key = _stable_owned_identifier(
            entry.get("server_pothole_id"), "server_pothole_id", where)
        drive_key = _stable_owned_identifier(entry.get("drive_id"), "drive_id", where)
        session_key = _stable_owned_identifier(
            entry.get("capture_session_id"), "capture_session_id", where)
        latitude, longitude = _owned_location(entry, where)
        if pothole_key is not None:
            group = f"owned:pothole:{pothole_key}"
            capture_mode = "drive" if drive_key is not None else "manual"
        elif drive_key is not None:
            # The hash makes a stable ID usable across cumulative exports without
            # writing a potentially identifying raw device/session ID to manifests.
            group = f"owned:drive:{drive_key}"
            capture_mode = "drive"
        elif session_key is not None:
            group = f"owned:session:{session_key}"
            capture_mode = "manual"
        else:
            # Missing capture provenance is not evidence of independence. Keep
            # the entire export together so near-duplicate burst/drive photos
            # cannot inflate validation or test performance.
            group = "owned:ungrouped"
            capture_mode = "manual"
        if label in NEGATIVE_OWNED_LABELS:
            if name in audit and audit[name]["pothole_boxes"]:
                raise PipelineError(
                    f"{audit_path}: positive boxes conflict with negative owner label {name}")
            boxes: tuple[tuple[float, float, float, float], ...] = ()
            annotation_basis = "owner_image_label_undamaged"
            counts["included_negative"] += 1
        elif label in POSITIVE_OWNED_LABELS:
            audited = audit.get(name)
            if audited is None:
                counts["skipped_positive_without_boxes"] += 1
                blockers.append(f"owned positive needs box audit: {source_path}:{name}")
                continue
            if audited["image_sha256"] != image_hash:
                raise PipelineError(f"{audit_path}: image hash does not match {name}")
            boxes = audited["pothole_boxes"]
            if not boxes:
                raise PipelineError(f"{audit_path}: positive label {name} has no pothole boxes")
            annotation_basis = "owner_label_plus_human_full_frame_box_audit"
            counts["included_positive"] += 1
        else:
            counts["skipped_unknown_label"] += 1
            continue
        candidates.append(Candidate(
            source="owned_app_export",
            source_item_id=f"{source_sha}:{name}",
            source_group=group,
            image_sha256=image_hash,
            boxes=boxes,
            read_bytes=lambda data=raw: data,
            capture_mode=capture_mode,
            suffix=suffix,
            annotation_basis=annotation_basis,
            source_license="owner" if licence == "owner" else "rights_attestation",
            audit_sha256=audit_sha if boxes else None,
            source_corpus_sha256=source_sha,
            latitude=latitude,
            longitude=longitude,
        ))
    unknown_audit = sorted(set(audit) - set(entries_by_path))
    if unknown_audit:
        raise PipelineError(f"{audit_path}: audited path is not in labels.json: {unknown_audit[0]}")
    return candidates, counts, blockers


def _rad_dataset_dir(root: Path) -> Path:
    if (root / "data.yaml").is_file():
        return root
    if (root / "images" / "data.yaml").is_file():
        return root / "images"
    raise PipelineError(f"{root}: expected data.yaml or images/data.yaml")


def _parse_rad_yaml(path: Path) -> tuple[str, ...]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise PipelineError(f"cannot read {path}: {error}") from error
    for line_number, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            raise PipelineError(f"{path}:{line_number}: malformed YAML line")
        key, value = (part.strip() for part in line.split(":", 1))
        if key in values or key not in {"train", "val", "test", "nc", "names"}:
            raise PipelineError(f"{path}:{line_number}: unexpected or duplicate key {key!r}")
        values[key] = value
    try:
        classes = tuple(ast.literal_eval(values["names"]))
        class_count = int(values["nc"])
    except (KeyError, TypeError, ValueError, SyntaxError) as error:
        raise PipelineError(f"{path}: malformed class schema") from error
    if classes != RAD_CLASSES or class_count != len(RAD_CLASSES):
        raise PipelineError(f"{path}: RAD class schema changed; review mappings before use")
    return classes


def _parse_rad_label(path: Path, classes: tuple[str, ...]) -> list[tuple[str, tuple[float, ...]]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise PipelineError(f"cannot read RAD label {path}: {error}") from error
    boxes: list[tuple[str, tuple[float, ...]]] = []
    for line_number, raw in enumerate(lines, 1):
        parts = raw.strip().split()
        if not parts:
            continue
        if len(parts) != 5 or not parts[0].isdigit():
            raise PipelineError(f"{path}:{line_number}: malformed YOLO label")
        class_id = int(parts[0])
        if not 0 <= class_id < len(classes):
            raise PipelineError(f"{path}:{line_number}: class id is outside RAD schema")
        box = normalise_box(parts[1:], f"{path}:{line_number}")
        boxes.append((classes[class_id], box))
    return boxes


def collect_rad(root: Path, audit_path: Optional[Path], require_complete: bool,
                include_speed_bump_negatives: bool = True,
                expected_source_sha256: Optional[str] = None,
                ) -> tuple[list[Candidate], Counter[str], list[str]]:
    dataset = _rad_dataset_dir(root)
    classes = _parse_rad_yaml(dataset / "data.yaml")
    audit_value: Optional[Mapping[str, Any]] = None
    if audit_path is not None:
        audit_value = load_json(audit_path)
        if (not isinstance(audit_value, dict) or audit_value.get("dataset_ref") != RAD_REF
                or audit_value.get("dataset_version") != RAD_VERSION):
            raise PipelineError(f"{audit_path}: RAD dataset identity/version mismatch")
    audit = _parse_audit(audit_path, "rad-pothole-box-audit-v1", frame_key="frame_id")
    audit_sha = sha256_file(audit_path) if audit_path is not None else None
    variants: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    raw_counts: Counter[str] = Counter()
    source_digest = hashlib.sha256()
    _digest_part(source_digest, b"pothole-rad-v3-consumed-tree-v1")
    _digest_part(source_digest, b"data.yaml")
    _digest_part(source_digest, (dataset / "data.yaml").read_bytes())
    for source_split in ("train", "valid", "test"):
        image_dir = dataset / source_split / "images"
        label_dir = dataset / source_split / "labels"
        if not image_dir.is_dir() or not label_dir.is_dir():
            raise PipelineError(f"{dataset}: missing {source_split} image/label directory")
        images = sorted(path for path in image_dir.iterdir()
                        if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg"})
        labels = sorted(path for path in label_dir.iterdir() if path.is_file())
        if any(path.is_symlink() for path in [*images, *labels]):
            raise PipelineError(f"{dataset}: RAD image/label symlinks are not allowed")
        expected = {image.with_suffix(".txt").name for image in images}
        actual = {label.name for label in labels}
        if expected != actual:
            raise PipelineError(
                f"{source_split}: image/label pairing failed "
                f"({len(expected - actual)} missing, {len(actual - expected)} orphaned)")
        for image in images:
            match = RAD_IMAGE_RE.fullmatch(image.name)
            if not match:
                raise PipelineError(f"unrecognised RAD image name: {image.name}")
            source_video = match.group("source") + "_mp4"
            frame_number = int(match.group("frame"))
            label_path = label_dir / image.with_suffix(".txt").name
            image_bytes = image.read_bytes()
            label_bytes = label_path.read_bytes()
            _digest_part(source_digest, image.relative_to(dataset).as_posix().encode("utf-8"))
            _digest_part(source_digest, image_bytes)
            _digest_part(source_digest, label_path.relative_to(dataset).as_posix().encode("utf-8"))
            _digest_part(source_digest, label_bytes)
            boxes = _parse_rad_label(label_path, classes)
            raw_counts["images"] += 1
            raw_counts["labels"] += 1
            raw_counts["boxes"] += len(boxes)
            for class_name, _ in boxes:
                if class_name == "RoadDamages":
                    raw_counts["road_damage_boxes"] += 1
                elif class_name == "SpeedBump":
                    raw_counts["speed_bump_boxes"] += 1
                else:
                    raw_counts[f"class_boxes_{class_name}"] += 1
            names = {name for name, _ in boxes}
            raw_counts["road_damage_frames"] += int("RoadDamages" in names)
            raw_counts["speed_bump_frames"] += int("SpeedBump" in names)
            raw_counts["pure_speed_bump_frames"] += int(
                "SpeedBump" in names and "RoadDamages" not in names)
            raw_counts["road_damage_and_speed_bump_frames"] += int(
                "SpeedBump" in names and "RoadDamages" in names)
            raw_counts["empty_labels"] += int(not boxes)
            variants[(source_video, frame_number)].append({
                "path": image,
                "boxes": boxes,
                "classes": names,
            })
    source_sha = source_digest.hexdigest()
    audit_source_sha = audit_value.get("source_sha256") if audit_value is not None else None
    if expected_source_sha256 is not None and audit_source_sha is not None \
            and expected_source_sha256 != audit_source_sha:
        raise PipelineError("RAD CLI and human-audit source SHA-256 values disagree")
    pinned_source_sha = expected_source_sha256 or audit_source_sha
    if pinned_source_sha is not None and (
            not isinstance(pinned_source_sha, str)
            or not re.fullmatch(r"[0-9a-f]{64}", pinned_source_sha)):
        raise PipelineError("RAD source SHA-256 is malformed")
    if require_complete and pinned_source_sha is None:
        raise PipelineError(
            "RAD release input needs --rad-source-sha256 or a source_sha256 in its audit")
    if pinned_source_sha is not None and pinned_source_sha != source_sha:
        raise PipelineError("RAD consumed file-tree SHA-256 differs from the pinned source")
    if require_complete and dict(raw_counts) != RAD_EXPECTED:
        raise PipelineError(
            f"RAD v3 image annotations are incomplete or changed; expected {RAD_EXPECTED}, "
            f"got {dict(raw_counts)}")
    candidates: list[Candidate] = []
    counts = Counter(raw_counts)
    blockers: list[str] = []
    frame_ids: set[str] = set()
    for (source_video, frame_number), frame_variants in sorted(variants.items()):
        if source_video in RAD_AMBIGUOUS_SOURCES:
            counts["skipped_ambiguous_source"] += 1
            continue
        frame_variants.sort(key=lambda item: item["path"].as_posix())
        selected = frame_variants[0]
        image_path: Path = selected["path"]
        image_hash = sha256_file(image_path)
        union_classes = {name for variant in frame_variants for name in variant["classes"]}
        frame_id = f"{source_video}:{frame_number}"
        frame_ids.add(frame_id)
        audited = audit.get(frame_id)
        if audited is not None:
            if audited["image_sha256"] != image_hash:
                raise PipelineError(f"{audit_path}: image hash does not match {frame_id}")
            boxes = audited["pothole_boxes"]
            annotation_basis = "human_full_frame_box_audit"
            count_key = (
                "included_audited_positive" if boxes else "included_audited_negative")
            counts[count_key] += 1
        elif (include_speed_bump_negatives and "SpeedBump" in union_classes
              and "RoadDamages" not in union_classes):
            boxes = ()
            annotation_basis = "rad_pure_speed_bump_hard_negative"
            counts["included_speed_bump_negative"] += 1
        else:
            if "RoadDamages" in union_classes:
                counts["skipped_broad_road_damage_without_audit"] += 1
            else:
                counts["skipped_non_target_public_frame"] += 1
            continue
        candidates.append(Candidate(
            source="rad_v3",
            source_item_id=frame_id,
            source_group=f"rad:{source_video}",
            image_sha256=image_hash,
            boxes=tuple(boxes),
            read_bytes=image_path.read_bytes,
            capture_mode="drive",
            suffix=image_path.suffix.lower(),
            annotation_basis=annotation_basis,
            source_license="MIT (reported by Kaggle metadata)",
            audit_sha256=audit_sha if audited is not None else None,
            source_corpus_sha256=source_sha,
        ))
    unknown_audit = sorted(set(audit) - frame_ids)
    if unknown_audit:
        raise PipelineError(f"{audit_path}: audited RAD frame was not found: {unknown_audit[0]}")
    if counts["included_audited_positive"] == 0:
        blockers.append("RAD contributes no human-audited pothole boxes")
    return candidates, counts, blockers


def rad_source_digest(root: Path) -> str:
    """Return the exact consumed RAD tree identity expected by release preparation."""
    candidates, _, _ = collect_rad(
        root, None, require_complete=False, include_speed_bump_negatives=True)
    digests = {candidate.source_corpus_sha256 for candidate in candidates}
    if len(digests) != 1 or None in digests:
        raise PipelineError("RAD source produced no unique consumed file-tree identity")
    return next(iter(digests))  # type: ignore[return-value]


def _coalesce_groups(candidates: Iterable[Candidate]) -> dict[str, str]:
    candidates = list(candidates)
    union = UnionFind()
    by_hash: dict[str, str] = {}
    for candidate in candidates:
        union.find(candidate.source_group)
        previous = by_hash.get(candidate.image_sha256)
        if previous is None:
            by_hash[candidate.image_sha256] = candidate.source_group
        else:
            union.union(previous, candidate.source_group)
    positioned = [candidate for candidate in candidates
                  if candidate.source == "owned_app_export"
                  and candidate.latitude is not None and candidate.longitude is not None]
    # Location is a conservative leakage guard, not a semantic dedupe label. It
    # deliberately joins nearby captures even when they came from another drive
    # or export, preventing alternate views of one road defect entering heldout.
    spatial_cells: dict[tuple[int, int, int], list[Candidate]] = defaultdict(list)
    for candidate in positioned:
        cell = _earth_cell(candidate)
        for x_offset in (-1, 0, 1):
            for y_offset in (-1, 0, 1):
                for z_offset in (-1, 0, 1):
                    neighbour = (
                        cell[0] + x_offset, cell[1] + y_offset, cell[2] + z_offset)
                    for prior in spatial_cells.get(neighbour, ()):
                        if _metres_between(candidate, prior) <= OWNED_GEO_LEAKAGE_RADIUS_M:
                            union.union(candidate.source_group, prior.source_group)
        spatial_cells[cell].append(candidate)
    return {group: union.find(group) for group in union.parent}


def _write_candidate(output: Path, candidate: Candidate, split: str, ordinal: int) -> dict[str, Any]:
    disk_split = "val" if split == "validation" else split
    stem = f"{candidate.source}-{candidate.image_sha256[:20]}-{ordinal:05d}"
    image_relative = Path("images") / disk_split / f"{stem}{candidate.suffix}"
    label_relative = Path("labels") / disk_split / f"{stem}.txt"
    image_target = output / image_relative
    label_target = output / label_relative
    image_target.parent.mkdir(parents=True, exist_ok=True)
    label_target.parent.mkdir(parents=True, exist_ok=True)
    image_content = candidate.read_bytes()
    if sha256_bytes(image_content) != candidate.image_sha256:
        raise PipelineError(
            f"source image changed while preparing {candidate.source_item_id}")
    validate_decodable_image(image_content, candidate.source_item_id)
    image_target.write_bytes(image_content)
    label_lines = [format_yolo_box(box) for box in candidate.boxes]
    label_target.write_text("\n".join(label_lines) + ("\n" if label_lines else ""),
                            encoding="utf-8")
    return {
        "source": candidate.source,
        "source_item_id": candidate.source_item_id,
        "source_group": candidate.source_group,
        "image_sha256": candidate.image_sha256,
        "split": split,
        "positive": candidate.positive,
        "capture_mode": candidate.capture_mode,
        "boxes": [list(box) for box in candidate.boxes],
        "annotation_basis": candidate.annotation_basis,
        "source_license": candidate.source_license,
        "audit_sha256": candidate.audit_sha256,
        "source_corpus_sha256": candidate.source_corpus_sha256,
        "image": image_relative.as_posix(),
        "label": label_relative.as_posix(),
    }


def build_dataset(output: Path, candidates: list[Candidate], source_counts: Mapping[str, Any],
                  blockers: list[str], allow_incomplete: bool) -> dict[str, Any]:
    if output.exists():
        raise PipelineError(f"output already exists; choose a fresh directory: {output}")
    output.mkdir(parents=True)
    by_image: dict[str, Candidate] = {}
    for candidate in candidates:
        previous = by_image.get(candidate.image_sha256)
        if previous is not None and previous.boxes != candidate.boxes:
            raise PipelineError(
                f"conflicting annotations for identical image bytes {candidate.image_sha256}")
        by_image.setdefault(candidate.image_sha256, candidate)
    # Build the connected group components before dropping duplicate pixels. If
    # the same bytes occur in two drives, every neighbouring frame in both drives
    # must follow that shared image into the same split.
    group_components = _coalesce_groups(candidates)
    deduplicated = sorted(by_image.values(), key=lambda value: (
        value.source, value.source_group, value.source_item_id, value.image_sha256))
    records: list[dict[str, Any]] = []
    for ordinal, candidate in enumerate(deduplicated):
        component = group_components[candidate.source_group]
        split = split_for_group(component)
        record = _write_candidate(output, candidate, split, ordinal)
        record["leakage_group"] = component
        records.append(record)
    split_counts: dict[str, Counter[str]] = {
        split: Counter() for split in ("train", "validation", "test")
    }
    all_groups: dict[str, set[str]] = {
        split: set() for split in ("train", "validation", "test")
    }
    positive_groups: dict[str, set[str]] = {
        split: set() for split in ("train", "validation", "test")
    }
    negative_groups: dict[str, set[str]] = {
        split: set() for split in ("train", "validation", "test")
    }
    for record in records:
        split = record["split"]
        group = record["leakage_group"]
        split_counts[split]["images"] += 1
        split_counts[split]["positive_images" if record["positive"]
                                             else "negative_images"] += 1
        split_counts[split]["boxes"] += len(record["boxes"])
        all_groups[split].add(group)
        (positive_groups if record["positive"] else negative_groups)[split].add(group)
    for split, counts in split_counts.items():
        counts["groups"] = len(all_groups[split])
        counts["positive_groups"] = len(positive_groups[split])
        counts["negative_groups"] = len(negative_groups[split])
        for field in SPLIT_COUNT_FIELDS:
            counts.setdefault(field, 0)
    release_requirements = []
    for split, required in RELEASE_MINIMUMS.items():
        for field, minimum in required.items():
            actual = split_counts[split][field]
            if actual < minimum:
                release_requirements.append(
                    f"{split} has {actual} {field}; requires at least {minimum}")
    all_blockers = list(dict.fromkeys([*blockers, *release_requirements]))
    release_ready = not all_blockers
    if not allow_incomplete and not release_ready:
        shutil.rmtree(output)
        raise PipelineError("dataset is not release-ready: " + "; ".join(all_blockers))
    yaml = (
        "train: images/train\n"
        "val: images/val\n"
        "test: images/test\n"
        "names:\n"
        "  0: pothole\n"
    )
    (output / "dataset.yaml").write_text(yaml, encoding="utf-8")
    manifest = sealed({
        "schema_version": "pothole-yolo-dataset-v1",
        "model_task": "single_class_object_detection",
        "class_names": {"0": "pothole"},
        "image_size": 640,
        "detection_contract": detection_api_contract(),
        "release_minimums": RELEASE_MINIMUMS,
        "split_policy": {
            "unit": "stable pothole/capture group, nearby owned location, or source video; coalesced across exact duplicate pixels",
            "owned_location_coalescing_metres": OWNED_GEO_LEAKAGE_RADIUS_M,
            "salt": "pothole-yolo-source-group-v1",
            "buckets": {"train": 8000, "validation": 1000, "test": 1000},
            "published_rad_splits_ignored": True,
            "release_minimums_are_safety_floors_not_quality_claims": True,
        },
        "provenance_policy": {
            "owned_positive_boxes": "complete human box audit required",
            "owned_negative_images": "owner-labelled undamaged only",
            "rad_positive_boxes": "complete human box audit required",
            "rad_road_damages_mapping": None,
            "rad_hard_negatives": "pure SpeedBump frames only",
            "source_corpus_identity": "exact consumed source bytes SHA-256",
        },
        "source_corpora": {
            source: sorted({
                candidate.source_corpus_sha256 for candidate in deduplicated
                if candidate.source == source and candidate.source_corpus_sha256 is not None
            })
            for source in sorted({candidate.source for candidate in deduplicated})
        },
        "source_registry_sha256": sha256_file(Path(__file__).with_name("source_registry.json")),
        "source_counts": source_counts,
        "counts": {
            "input_candidates": len(candidates),
            "exact_pixel_duplicates_removed": len(candidates) - len(deduplicated),
            "splits": {key: dict(value) for key, value in split_counts.items()},
        },
        "release_ready": release_ready,
        "blockers": all_blockers,
        "records": records,
    })
    write_json(output / "manifest.json", manifest)
    return manifest


def command_prepare(args: argparse.Namespace) -> None:
    candidates: list[Candidate] = []
    summaries: dict[str, Any] = {}
    blockers: list[str] = []
    rights = _load_rights(Path(args.owned_rights) if args.owned_rights else None)
    audits = [Path(item) for item in args.owned_audit]
    if audits and len(audits) != len(args.owned):
        raise PipelineError("provide either zero owned audits or one --owned-audit per --owned")
    for index, source in enumerate(args.owned):
        audit = audits[index] if audits else None
        rows, counts, source_blockers = collect_owned(Path(source), audit, rights)
        candidates.extend(rows)
        summaries[f"owned_{index}"] = dict(counts)
        blockers.extend(source_blockers)
    if args.rad_root:
        rows, counts, source_blockers = collect_rad(
            Path(args.rad_root), Path(args.rad_audit) if args.rad_audit else None,
            require_complete=not args.allow_incomplete_rad,
            expected_source_sha256=args.rad_source_sha256,
        )
        candidates.extend(rows)
        summaries["rad_v3"] = dict(counts)
        blockers.extend(source_blockers)
        if args.allow_incomplete_rad:
            blockers.append("RAD completeness verification was disabled for this audit build")
    if not args.owned and not args.rad_root:
        raise PipelineError("at least one --owned source or --rad-root is required")
    if args.allow_incomplete:
        blockers.append("dataset was created in --allow-incomplete audit mode")
    manifest = build_dataset(Path(args.output), candidates, summaries, blockers,
                             allow_incomplete=args.allow_incomplete)
    print(__import__("json").dumps({
        "output": str(Path(args.output)),
        "manifest_sha256": manifest["manifest_sha256"],
        "release_ready": manifest["release_ready"],
        "counts": manifest["counts"],
        "blockers": manifest["blockers"],
    }, indent=2, sort_keys=True))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--owned", action="append", default=[],
                        help="app dataset ZIP or extracted directory; repeatable")
    result.add_argument("--owned-audit", action="append", default=[],
                        help="matching human box audit; one per --owned when used")
    result.add_argument("--owned-rights",
                        help="training-rights attestation for exports lacking licence fields")
    result.add_argument("--rad-root", help="extracted RAD v3 root")
    result.add_argument("--rad-audit", help="human pothole box audit for RAD frames")
    result.add_argument(
        "--rad-source-sha256",
        help="pinned SHA-256 of the exact consumed RAD v3 file tree",
    )
    result.add_argument("--allow-incomplete-rad", action="store_true",
                        help="fixture/development only: do not require audited v3 source counts")
    result.add_argument("--allow-incomplete", action="store_true",
                        help="write an explicitly non-release-ready audit build")
    result.add_argument("--output", required=True, help="fresh output directory")
    return result


def main() -> None:
    try:
        command_prepare(parser().parse_args())
    except PipelineError as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
