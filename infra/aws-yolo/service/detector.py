"""Ultralytics raw-ONNX pothole detector and simplified verdict adapter."""

from __future__ import annotations

import io
import hashlib
import hmac
import importlib.metadata
import json
import math
import os
import platform
import sys
import warnings
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Iterable, Sequence


LAMBDA_BASE_IMAGE_DIGEST = (
    "sha256:ab6df78b68b50723c93741bb7f9ea9f68c7cf3433359b33e4f37db5266567f9c")
DETECTION_CONTRACT = {
    "prompt_version": "road-damage-v5",
    "schema_version": 4,
    "input_images_per_request": 1,
    "capture_modes": ["manual", "drive"],
    "output_fields": [
        "image_quality", "assessment", "damage_type", "size", "description",
    ],
    "image_quality": ["acceptable", "rejected"],
    "assessment": ["damaged", "undamaged"],
    "damage_type": [
        "pothole_cavity", "failed_patch", "surface_breakup",
        "rut_or_depression", "other_road_damage", None,
    ],
    "size": ["small", "medium", "large", None],
}
RUNTIME_LIMITS = {
    "maximum_decoded_pixels": 12_000_000,
    "input_images_per_request": 1,
}


@dataclass(frozen=True)
class Detection:
    x1: float
    y1: float
    x2: float
    y2: float
    confidence: float
    class_id: int = 0


@dataclass(frozen=True)
class AnalysedImage:
    width: int
    height: int
    quality: str
    detections: tuple[Detection, ...]


def required_runtime_versions() -> dict[str, str]:
    """Read the exact dependency contract installed by the Lambda image."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "requirements.txt")
    expected_names = {"numpy", "onnxruntime", "Pillow"}
    versions: dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8") as source:
            lines = source.read().splitlines()
    except OSError as error:
        raise RuntimeError("The AWS runtime requirements are unavailable.") from error
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("==")
        if (len(parts) != 2 or parts[0] not in expected_names or not parts[1]
                or parts[0] in versions):
            raise RuntimeError("The AWS runtime dependency pins are malformed.")
        versions[parts[0]] = parts[1]
    if set(versions) != expected_names:
        raise RuntimeError("The AWS runtime dependency pins are incomplete.")
    return versions


def current_runtime_execution() -> dict[str, str]:
    machine = platform.machine().strip().lower()
    if machine in {"x86_64", "amd64"}:
        architecture = "x86_64"
    elif machine in {"aarch64", "arm64"}:
        architecture = "arm64"
    else:
        raise RuntimeError("The runtime architecture is not supported by Lambda.")
    try:
        import onnxruntime as ort  # type: ignore
    except ImportError as error:
        raise RuntimeError("ONNX Runtime is unavailable.") from error
    if "CPUExecutionProvider" not in ort.get_available_providers():
        raise RuntimeError("ONNX Runtime CPUExecutionProvider is unavailable.")
    return {
        "python_major_minor": f"{sys.version_info.major}.{sys.version_info.minor}",
        "python_implementation": platform.python_implementation(),
        "system": platform.system(),
        "lambda_architecture": architecture,
        "onnx_provider": "CPUExecutionProvider",
        "lambda_base_image_digest": os.environ.get(
            "POTHOLE_LAMBDA_BASE_IMAGE_DIGEST", ""),
    }


def load_and_validate_manifest(path: str, expected_version: str) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as source:
            manifest = json.load(source)
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("A readable model-manifest.json is required.") from error
    if not isinstance(manifest, dict):
        raise RuntimeError("The model manifest must be a JSON object.")
    expected_seal = manifest.get("model_manifest_sha256")
    if not isinstance(expected_seal, str) or not re_full_hex(expected_seal):
        raise RuntimeError("The model manifest seal is missing or malformed.")
    unsealed = dict(manifest)
    unsealed.pop("model_manifest_sha256", None)
    canonical = (
        json.dumps(
            unsealed, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        + "\n"
    ).encode("utf-8")
    if not hmac.compare_digest(hashlib.sha256(canonical).hexdigest(), expected_seal):
        raise RuntimeError("The model manifest seal does not match its content.")
    if manifest.get("schema_version") != "pothole-yolo-model-manifest-v1":
        raise RuntimeError("The model manifest schema is unsupported.")
    if manifest.get("task") != "pothole_detection":
        raise RuntimeError("The model manifest is not for pothole detection.")
    if manifest.get("detection_contract") != DETECTION_CONTRACT:
        raise RuntimeError("The model manifest detection contract is unsupported.")
    if manifest.get("class_names") != {"0": "pothole"}:
        raise RuntimeError("The model must contain exactly class 0 = pothole.")
    if str(manifest.get("model_version", "")) != expected_version:
        raise RuntimeError("Configured and packaged model versions differ.")
    if manifest.get("image_size") != 640:
        raise RuntimeError("The runtime requires the evaluated 640-pixel model.")
    export = manifest.get("export")
    if not isinstance(export, dict) or export.get("format") != "onnx":
        raise RuntimeError("The packaged model must be an ONNX export.")
    if (
        export.get("nms") is not False
        or export.get("dynamic") is not False
        or export.get("simplify") is not True
        or export.get("opset") != 17
    ):
        raise RuntimeError("The runtime requires a raw ONNX export with nms=false.")
    if export.get("raw_ultralytics_output") is not True:
        raise RuntimeError("The runtime requires raw Ultralytics ONNX output.")
    output_contract = manifest.get("output_contract")
    if not isinstance(output_contract, dict):
        raise RuntimeError("The model manifest has no output contract.")
    if output_contract.get("layout") != "batch,channels,predictions":
        raise RuntimeError("The ONNX output layout is unsupported.")
    if output_contract.get("channels") != [
        "center_x", "center_y", "width", "height", "class_0_score"
    ]:
        raise RuntimeError("The ONNX output channels are unsupported.")
    decision = manifest.get("decision")
    if not isinstance(decision, dict):
        raise RuntimeError("The model manifest has no evaluated decision settings.")
    try:
        confidence = float(decision["confidence_threshold"])
        nms_iou = float(decision["nms_iou_threshold"])
        maximum = int(decision["maximum_detections"])
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError("The model decision settings are invalid.") from error
    if (not 0 < confidence <= 1 or not 0 < nms_iou <= 1
            or not 1 <= maximum <= 1000):
        raise RuntimeError("The model decision settings are outside safe bounds.")
    dataset_provenance = manifest.get("dataset_provenance")
    training_provenance = manifest.get("training_provenance")
    evaluation_provenance = manifest.get("evaluation_provenance")
    if not all(
        isinstance(value, dict)
        for value in (dataset_provenance, training_provenance, evaluation_provenance)
    ):
        raise RuntimeError("The model provenance receipts are incomplete.")
    validation = evaluation_provenance.get("validation")
    test = evaluation_provenance.get("test")
    if not isinstance(validation, dict) or not isinstance(test, dict):
        raise RuntimeError("The model evaluation provenance is incomplete.")
    required_hashes = (
        dataset_provenance.get("manifest_sha256"),
        training_provenance.get("receipt_sha256"),
        training_provenance.get("best_weights_sha256"),
        validation.get("threshold_receipt_sha256"),
        test.get("evaluation_receipt_sha256"),
    )
    if not all(isinstance(value, str) and re_full_hex(value) for value in required_hashes):
        raise RuntimeError("The model provenance receipts are incomplete.")
    artifact = manifest.get("artifact")
    runtime_parity = manifest.get("runtime_parity")
    if not isinstance(artifact, dict) or not isinstance(runtime_parity, dict):
        raise RuntimeError("The model artifact or runtime parity receipt is missing.")
    runtime_environment = runtime_parity.get("runtime_environment")
    runtime_limits = runtime_parity.get("runtime_limits")
    runtime_execution = runtime_parity.get("runtime_execution")
    if (
        runtime_parity.get("gate_passed") is not True
        or runtime_parity.get("split") != "test"
        or not isinstance(runtime_parity.get("records_compared"), int)
        or runtime_parity["records_compared"] < 1
        or runtime_parity.get("onnx_sha256") != artifact.get("sha256")
        or runtime_parity.get("dataset_manifest_sha256")
        != dataset_provenance.get("manifest_sha256")
        or runtime_parity.get("weights_sha256")
        != training_provenance.get("best_weights_sha256")
        or runtime_parity.get("reference_prediction_sha256")
        != test.get("prediction_sha256")
        or runtime_parity.get("detection_contract") != DETECTION_CONTRACT
        or not isinstance(runtime_parity.get("receipt_sha256"), str)
        or not re_full_hex(runtime_parity["receipt_sha256"])
        or not isinstance(runtime_parity.get("runtime_module_sha256"), str)
        or not re_full_hex(runtime_parity["runtime_module_sha256"])
        or not isinstance(runtime_environment, dict)
        or set(runtime_environment) != {"numpy", "onnxruntime", "Pillow"}
        or not all(isinstance(value, str) and value for value in runtime_environment.values())
        or runtime_environment != required_runtime_versions()
        or not isinstance(runtime_execution, dict)
        or runtime_execution.get("python_major_minor") != "3.12"
        or runtime_execution.get("python_implementation") != "CPython"
        or runtime_execution.get("system") != "Linux"
        or runtime_execution.get("lambda_architecture") not in {"x86_64", "arm64"}
        or runtime_execution.get("onnx_provider") != "CPUExecutionProvider"
        or runtime_execution.get("lambda_base_image_digest")
        != LAMBDA_BASE_IMAGE_DIGEST
        or len(runtime_execution) != 6
        or runtime_limits != RUNTIME_LIMITS
    ):
        raise RuntimeError("The production-runtime parity receipt is inconsistent.")
    return manifest


def re_full_hex(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@lru_cache(maxsize=4)
def validate_release_bundle(
    model_path: str,
    manifest_path: str,
    parity_path: str,
    expected_version: str,
    expected_confidence_threshold: float,
    expected_max_decoded_pixels: int,
) -> dict[str, Any]:
    manifest = load_and_validate_manifest(manifest_path, expected_version)
    try:
        with open(parity_path, "r", encoding="utf-8") as source:
            parity = json.load(source)
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("A readable runtime-parity.json is required.") from error
    if not isinstance(parity, dict):
        raise RuntimeError("The runtime parity receipt must be a JSON object.")
    parity_seal = parity.get("parity_receipt_sha256")
    if not isinstance(parity_seal, str) or not re_full_hex(parity_seal):
        raise RuntimeError("The runtime parity receipt seal is missing or malformed.")
    parity_unsealed = dict(parity)
    parity_unsealed.pop("parity_receipt_sha256", None)
    parity_canonical = (
        json.dumps(
            parity_unsealed,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    if not hmac.compare_digest(
        hashlib.sha256(parity_canonical).hexdigest(), parity_seal
    ):
        raise RuntimeError("The runtime parity receipt seal does not match its content.")
    embedded_parity = manifest["runtime_parity"]
    parity_metrics = parity.get("metrics")
    if not isinstance(parity_metrics, dict):
        raise RuntimeError("The runtime parity metrics are missing.")
    if (
        parity.get("schema_version") != "pothole-yolo-runtime-parity-v1"
        or parity.get("task") != "pothole_detection"
        or parity.get("gate_passed") is not True
        or parity_metrics.get("gate_passed") is not True
        or parity_seal != embedded_parity["receipt_sha256"]
        or parity.get("onnx_sha256") != embedded_parity["onnx_sha256"]
        or parity.get("weights_sha256") != embedded_parity["weights_sha256"]
        or parity.get("dataset_manifest_sha256")
        != embedded_parity["dataset_manifest_sha256"]
        or parity.get("reference_prediction_sha256")
        != embedded_parity["reference_prediction_sha256"]
        or parity.get("runtime_module_sha256")
        != embedded_parity["runtime_module_sha256"]
        or parity.get("runtime_environment")
        != embedded_parity.get("runtime_environment")
        or parity.get("runtime_limits") != embedded_parity.get("runtime_limits")
        or parity.get("runtime_execution") != embedded_parity.get("runtime_execution")
        or parity.get("detection_contract") != DETECTION_CONTRACT
        or parity.get("detection_contract")
        != embedded_parity.get("detection_contract")
        or parity.get("split") != embedded_parity["split"]
        or parity.get("records_compared") != embedded_parity["records_compared"]
        or parity.get("decision") != {
            "confidence_threshold": manifest["decision"]["confidence_threshold"],
            "nms_iou_threshold": manifest["decision"]["nms_iou_threshold"],
            "maximum_detections": manifest["decision"]["maximum_detections"],
        }
        or parity.get("tolerances") != embedded_parity.get("tolerances")
        or parity.get("metrics") != embedded_parity.get("metrics")
        or parity_metrics.get("canonical_decision_mismatches") != 0
        or parity_metrics.get("canonical_decisions_compared")
        != parity.get("records_compared")
    ):
        raise RuntimeError("The runtime parity receipt does not match the model manifest.")
    artifact = manifest.get("artifact")
    if not isinstance(artifact, dict) or not isinstance(artifact.get("sha256"), str):
        raise RuntimeError("The model manifest has no artifact hash.")
    try:
        model_bytes = os.path.getsize(model_path)
        model_sha256 = sha256_file(model_path)
    except OSError as error:
        raise RuntimeError("The packaged ONNX model is unavailable.") from error
    if model_sha256 != artifact["sha256"] or model_bytes != int(artifact.get("bytes", -1)):
        raise RuntimeError("The packaged ONNX model does not match its manifest.")
    runtime_sha256 = sha256_file(os.path.abspath(__file__))
    if runtime_sha256 != manifest["runtime_parity"]["runtime_module_sha256"]:
        raise RuntimeError("The deployed detector code did not pass this model's parity gate.")
    expected_environment = manifest["runtime_parity"].get("runtime_environment")
    if not isinstance(expected_environment, dict) or set(expected_environment) != {
        "numpy", "onnxruntime", "Pillow"
    }:
        raise RuntimeError("The runtime dependency receipt is incomplete.")
    for distribution, expected in expected_environment.items():
        try:
            installed = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError as error:
            raise RuntimeError(f"Runtime dependency {distribution} is missing.") from error
        if installed != expected:
            raise RuntimeError(
                f"Runtime dependency {distribution} differs from the parity environment."
            )
    if current_runtime_execution() != manifest["runtime_parity"]["runtime_execution"]:
        raise RuntimeError(
            "The Lambda Python/platform/architecture differs from the parity runtime."
        )
    evaluated_confidence = float(manifest["decision"]["confidence_threshold"])
    if abs(expected_confidence_threshold - evaluated_confidence) > 1e-9:
        raise RuntimeError(
            "DETECTION_CONFIDENCE_THRESHOLD differs from the evaluated release."
        )
    evaluated_pixels = int(
        manifest["runtime_parity"]["runtime_limits"]["maximum_decoded_pixels"]
    )
    if expected_max_decoded_pixels != evaluated_pixels:
        raise RuntimeError(
            "MAX_DECODED_PIXELS differs from the evaluated release."
        )
    return manifest


def decode_image_bytes(content: bytes, *, max_decoded_pixels: int) -> Any:
    """Open and fully decode an image only after checking its declared dimensions."""
    try:
        from PIL import Image, ImageOps  # type: ignore
    except ImportError:
        raise
    # Make Pillow's decompression-bomb warning a hard failure. The explicit size
    # check catches the same boundary and keeps behavior stable across versions.
    previous_limit = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = max_decoded_pixels
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as source:
                width, height = source.size
                if width < 1 or height < 1 or width * height > max_decoded_pixels:
                    raise ValueError("The decoded image exceeds the pixel limit.")
                source.load()
                image = ImageOps.exif_transpose(source).convert("RGB")
    except (OSError, SyntaxError, Image.DecompressionBombError,
            Image.DecompressionBombWarning) as error:
        raise ValueError("The image could not be decoded safely.") from error
    finally:
        Image.MAX_IMAGE_PIXELS = previous_limit
    return image


def validate_image_payloads(
    images: Sequence[Any], *, max_decoded_pixels: int
) -> None:
    for item in images:
        decode_image_bytes(item.content, max_decoded_pixels=max_decoded_pixels)


def image_quality(rgb: Any, np: Any) -> str:
    """Return whether one frame contains enough visual signal for inference."""
    width, height = rgb.size
    if width < 160 or height < 120 or width * height > 30_000_000:
        return "rejected"
    sample = rgb.convert("L")
    sample.thumbnail((256, 256))
    pixels = np.asarray(sample, dtype=np.float32)
    mean = float(pixels.mean())
    contrast = float(pixels.std())
    horizontal = float(np.abs(np.diff(pixels, axis=1)).mean()) if pixels.shape[1] > 1 else 0.0
    vertical = float(np.abs(np.diff(pixels, axis=0)).mean()) if pixels.shape[0] > 1 else 0.0
    sharpness = (horizontal + vertical) / 2
    if mean < 12 or mean > 248 or contrast < 6 or sharpness < 1.5:
        return "rejected"
    return "acceptable"


def _intersection_over_union(one: Detection, two: Detection) -> float:
    left = max(one.x1, two.x1)
    top = max(one.y1, two.y1)
    right = min(one.x2, two.x2)
    bottom = min(one.y2, two.y2)
    intersection = max(0.0, right - left) * max(0.0, bottom - top)
    one_area = max(0.0, one.x2 - one.x1) * max(0.0, one.y2 - one.y1)
    two_area = max(0.0, two.x2 - two.x1) * max(0.0, two.y2 - two.y1)
    union = one_area + two_area - intersection
    return intersection / union if union > 0 else 0.0


def non_max_suppression(
    detections: Iterable[Detection], iou_threshold: float, limit: int
) -> tuple[Detection, ...]:
    pending = sorted(detections, key=lambda item: item.confidence, reverse=True)
    kept: list[Detection] = []
    while pending and len(kept) < limit:
        chosen = pending.pop(0)
        kept.append(chosen)
        pending = [
            item
            for item in pending
            if item.class_id != chosen.class_id
            or _intersection_over_union(chosen, item) < iou_threshold
        ]
    return tuple(kept)


def canonical_verdict(
    image: AnalysedImage,
    *,
    confidence_threshold: float,
    language: str,
) -> dict[str, Any]:
    if image.quality == "rejected":
        if language == "kn":
            description = "ಚಿತ್ರದ ಗುಣಮಟ್ಟ ಪಥೋಲ್ ಪರಿಶೀಲನೆಗೆ ಸಾಕಾಗಿಲ್ಲ."
        else:
            description = "The image quality is insufficient for pothole detection."
        return {
            "image_quality": "rejected",
            "assessment": "undamaged",
            "damage_type": None,
            "size": None,
            "description": description,
        }

    candidates = tuple(
        detection
        for detection in image.detections
        if detection.class_id == 0 and detection.confidence >= confidence_threshold
    )
    if not candidates:
        description = (
            "ಬಳಸಬಹುದಾದ ರಸ್ತೆ ಚಿತ್ರದಲ್ಲಿ ಪಥೋಲ್ ಪತ್ತೆಯಾಗಿಲ್ಲ."
            if language == "kn"
            else "No pothole was detected in the acceptable road image."
        )
        return {
            "image_quality": "acceptable",
            "assessment": "undamaged",
            "damage_type": None,
            "size": None,
            "description": description,
        }

    if language == "kn":
        description = (
            "ಪಥೋಲ್ ಮಾದರಿಯು ರಸ್ತೆ ಮೇಲ್ಮೈಯಲ್ಲಿ ಗುಂಡಿಯನ್ನು ಪತ್ತೆಹಚ್ಚಿದೆ. "
            "ಅಳತೆ ಮಾನದಂಡ ಇಲ್ಲದ ಕಾರಣ ಭೌತಿಕ ಗಾತ್ರವನ್ನು ನಿರ್ಧರಿಸಲಾಗಿಲ್ಲ."
        )
    else:
        description = (
            "The pothole model detected a cavity on the road surface. "
            "Physical size cannot be estimated without a scale reference."
        )
    return {
        "image_quality": "acceptable",
        "assessment": "damaged",
        "damage_type": "pothole_cavity",
        "size": None,
        "description": description,
    }


class YoloOnnxDetector:
    def __init__(
        self,
        *,
        model_path: str,
        parity_path: str,
        model_version: str,
        confidence_threshold: float,
        max_decoded_pixels: int,
    ) -> None:
        manifest_path = os.environ.get(
            "MODEL_MANIFEST_PATH", "/opt/model/model-manifest.json"
        )
        self.manifest = validate_release_bundle(
            model_path,
            manifest_path,
            parity_path,
            model_version,
            confidence_threshold,
            max_decoded_pixels,
        )
        decision = self.manifest["decision"]
        try:
            import numpy as np  # type: ignore
            import onnxruntime as ort  # type: ignore
            from PIL import Image  # type: ignore
        except ImportError as error:
            raise RuntimeError("The ONNX image runtime is unavailable.") from error
        self.np = np
        self.Image = Image
        options = ort.SessionOptions()
        options.intra_op_num_threads = int(os.environ.get("ORT_INTRA_OP_THREADS", "1"))
        options.inter_op_num_threads = 1
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.session = ort.InferenceSession(
            model_path, sess_options=options, providers=["CPUExecutionProvider"]
        )
        model_input = self.session.get_inputs()[0]
        if model_input.type != "tensor(float)":
            raise RuntimeError("The ONNX model input must be float32.")
        self.input_name = model_input.name
        shape = model_input.shape
        manifest_size = int(self.manifest.get("image_size", 640))
        self.input_height = int(shape[-2]) if isinstance(shape[-2], int) else manifest_size
        self.input_width = int(shape[-1]) if isinstance(shape[-1], int) else manifest_size
        if self.input_height != manifest_size or self.input_width != manifest_size:
            raise RuntimeError("The ONNX input dimensions differ from the model manifest.")
        self.model_version = model_version
        self.confidence_threshold = confidence_threshold
        self.nms_iou_threshold = float(decision["nms_iou_threshold"])
        self.max_detections = int(decision["maximum_detections"])
        self.max_decoded_pixels = max_decoded_pixels

    def _decode(self, content: bytes) -> Any:
        return decode_image_bytes(
            content, max_decoded_pixels=self.max_decoded_pixels
        )

    def _tensor(self, image: Any) -> tuple[Any, float, float, float]:
        ratio = min(self.input_width / image.width, self.input_height / image.height)
        resized_width = max(1, round(image.width * ratio))
        resized_height = max(1, round(image.height * ratio))
        resized = image.resize((resized_width, resized_height), self.Image.Resampling.BILINEAR)
        canvas = self.Image.new("RGB", (self.input_width, self.input_height), (114, 114, 114))
        pad_x = (self.input_width - resized_width) / 2
        pad_y = (self.input_height - resized_height) / 2
        canvas.paste(resized, (round(pad_x), round(pad_y)))
        array = self.np.asarray(canvas, dtype=self.np.float32) / 255.0
        tensor = self.np.transpose(array, (2, 0, 1))[None, ...]
        return tensor, ratio, pad_x, pad_y

    def _postprocess(
        self,
        output: Any,
        *,
        width: int,
        height: int,
        ratio: float,
        pad_x: float,
        pad_y: float,
    ) -> tuple[Detection, ...]:
        predictions = self.np.asarray(output)
        if predictions.ndim == 3 and predictions.shape[0] == 1:
            predictions = predictions[0]
        if predictions.ndim != 2:
            raise RuntimeError("Unexpected ONNX detection output rank.")
        # The sealed one-class contract has exactly five channels. Raw Ultralytics
        # normally returns [5, anchors]; tolerate [anchors, 5] without guessing.
        if predictions.shape[0] == 5 and predictions.shape[1] != 5:
            predictions = predictions.T
        if predictions.shape[1] != 5:
            raise RuntimeError("Unexpected ONNX detection output shape.")
        # Ultralytics applies NMS in the 640x640 letterboxed coordinate system,
        # then scales/clips the retained boxes back to the source image. Keeping
        # that order matters for boxes that cross an image boundary.
        letterboxed: list[Detection] = []
        for row in predictions:
            try:
                center_x, center_y, box_width, box_height, class_score = map(
                    float, row[:5]
                )
            except (TypeError, ValueError) as error:
                raise RuntimeError("The ONNX detector emitted a malformed row.") from error
            if (not all(math.isfinite(value) for value in (
                    center_x, center_y, box_width, box_height, class_score))
                    or not 0.0 <= class_score <= 1.0
                    or box_width <= 0.0 or box_height <= 0.0):
                raise RuntimeError("The ONNX detector emitted unsafe numeric values.")
            class_scores = row[4:]
            class_id = int(self.np.argmax(class_scores))
            confidence = float(class_scores[class_id])
            if class_id != 0 or confidence < self.confidence_threshold:
                continue
            letterboxed.append(Detection(
                center_x - box_width / 2,
                center_y - box_height / 2,
                center_x + box_width / 2,
                center_y + box_height / 2,
                confidence,
                class_id,
            ))
        retained = non_max_suppression(
            letterboxed, self.nms_iou_threshold, self.max_detections
        )
        detections: list[Detection] = []
        for detection in retained:
            x1 = max(0.0, min(float(width), (detection.x1 - pad_x) / ratio))
            y1 = max(0.0, min(float(height), (detection.y1 - pad_y) / ratio))
            x2 = max(0.0, min(float(width), (detection.x2 - pad_x) / ratio))
            y2 = max(0.0, min(float(height), (detection.y2 - pad_y) / ratio))
            if x2 - x1 < 2 or y2 - y1 < 2:
                continue
            detections.append(Detection(
                x1, y1, x2, y2, detection.confidence, detection.class_id
            ))
        return tuple(detections)

    def analyse(
        self, images: Sequence[Any], *, capture_mode: str, language: str
    ) -> dict[str, Any]:
        if capture_mode not in {"manual", "drive"}:
            raise RuntimeError("The YOLO detector capture mode is invalid.")
        if len(images) != 1:
            raise RuntimeError("The YOLO detector requires exactly one image.")
        item = images[0]
        image = self._decode(item.content)
        quality = image_quality(image, self.np)
        if quality == "rejected":
            detections: tuple[Detection, ...] = ()
        else:
            tensor, ratio, pad_x, pad_y = self._tensor(image)
            outputs = self.session.run(None, {self.input_name: tensor})
            if len(outputs) != 1:
                raise RuntimeError("The ONNX model must expose one raw detection output.")
            detections = self._postprocess(
                outputs[0],
                width=image.width,
                height=image.height,
                ratio=ratio,
                pad_x=pad_x,
                pad_y=pad_y,
            )
        analysed = AnalysedImage(
            width=image.width,
            height=image.height,
            quality=quality,
            detections=detections,
        )
        return canonical_verdict(
            analysed,
            confidence_threshold=self.confidence_threshold,
            language=language,
        )
