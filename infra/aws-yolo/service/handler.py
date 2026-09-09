"""Authenticated, budget-gated Lambda entry point for pothole YOLO inference.

The expensive model is loaded only after a request has passed validation and the
monthly admission counter has atomically accepted it.  Request bodies and images
are intentionally never written to logs.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable


REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
DATA_URL_RE = re.compile(
    r"^data:(image/(?:jpeg|jpg|png|webp));base64,([A-Za-z0-9+/=_-]+)$",
    re.IGNORECASE,
)
ALLOWED_CAPTURE_MODES = {"manual", "drive"}
ALLOWED_LANGUAGES = {"en", "kn"}
VERDICT_KEYS = {
    "image_quality",
    "assessment",
    "damage_type",
    "size",
    "description",
}


class ServiceError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retry_after = retry_after


class AdmissionLimitExceeded(ServiceError):
    def __init__(self, code: str, retry_after: int) -> None:
        message = (
            "The monthly YOLO request cap has been reached."
            if code == "monthly_request_cap_exceeded"
            else "The monthly YOLO estimated-compute budget has been reached."
        )
        super().__init__(429, code, message, retry_after=retry_after)


@dataclass(frozen=True)
class Config:
    api_key_sha256: str
    budget_table: str
    monthly_request_cap: int
    monthly_estimated_microusd_cap: int
    estimated_microusd_per_request: int
    max_json_body_bytes: int
    max_image_bytes: int
    max_decoded_pixels: int
    model_path: str
    model_parity_path: str
    model_version: str
    confidence_threshold: float

    @classmethod
    def from_env(cls) -> "Config":
        api_key_hash = os.environ.get("API_KEY_SHA256", "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", api_key_hash):
            raise ServiceError(
                503,
                "service_not_configured",
                "The YOLO gateway credential hash is not configured.",
            )

        def integer(name: str, default: str, *, minimum: int = 0) -> int:
            try:
                value = int(os.environ.get(name, default))
            except ValueError as error:
                raise ServiceError(
                    503, "service_not_configured", f"{name} must be an integer."
                ) from error
            if value < minimum:
                raise ServiceError(
                    503,
                    "service_not_configured",
                    f"{name} must be at least {minimum}.",
                )
            return value

        def decimal(name: str, default: str, *, minimum: float) -> float:
            try:
                value = float(os.environ.get(name, default))
            except ValueError as error:
                raise ServiceError(
                    503, "service_not_configured", f"{name} must be numeric."
                ) from error
            if not minimum <= value <= 1:
                raise ServiceError(
                    503,
                    "service_not_configured",
                    f"{name} must be between {minimum} and 1.",
                )
            return value

        table = os.environ.get("BUDGET_TABLE", "").strip()
        if not table:
            raise ServiceError(
                503,
                "service_not_configured",
                "The monthly admission-counter table is not configured.",
            )
        request_cap = integer("MONTHLY_REQUEST_CAP", "0")
        budget_cap = integer("MONTHLY_ESTIMATED_MICROUSD_CAP", "0")
        per_request = integer("ESTIMATED_MICROUSD_PER_REQUEST", "0", minimum=1)
        if request_cap < 1 or budget_cap < 1:
            # A zero cap is an intentional kill switch, handled as a limit rather
            # than a broken model configuration.
            pass
        confidence_threshold = decimal(
            "DETECTION_CONFIDENCE_THRESHOLD", "0.50", minimum=0.01
        )
        return cls(
            api_key_sha256=api_key_hash,
            budget_table=table,
            monthly_request_cap=request_cap,
            monthly_estimated_microusd_cap=budget_cap,
            estimated_microusd_per_request=per_request,
            max_json_body_bytes=integer("MAX_JSON_BODY_BYTES", "5500000", minimum=1),
            max_image_bytes=integer("MAX_IMAGE_BYTES", "3500000", minimum=1),
            max_decoded_pixels=integer(
                "MAX_DECODED_PIXELS", "12000000", minimum=1
            ),
            model_path=os.environ.get("MODEL_PATH", "/opt/model/model.onnx"),
            model_parity_path=os.environ.get(
                "MODEL_PARITY_PATH", "/opt/model/runtime-parity.json"
            ),
            model_version=os.environ.get("MODEL_VERSION", "pothole-yolo-unversioned")[:80],
            confidence_threshold=confidence_threshold,
        )


@dataclass(frozen=True)
class ImageInput:
    content: bytes
    mime: str


@dataclass(frozen=True)
class DetectionRequest:
    request_id: str
    capture_mode: str
    language: str
    images: tuple[ImageInput, ...]


@dataclass(frozen=True)
class AdmissionReceipt:
    period: str
    requests_used: int
    estimated_microusd_used: int


@dataclass
class RequestContext:
    request_id: str
    aws_request_id: str | None
    outcome: str = "internal_error"
    status: int = 500
    admitted: bool = False
    receipt: AdmissionReceipt | None = None
    model_version: str | None = None


class DynamoAdmissionGate:
    """Atomic monthly two-cap admission using one DynamoDB item per UTC month."""

    def __init__(self, table: Any, config: Config) -> None:
        self.table = table
        self.config = config

    @classmethod
    def from_config(cls, config: Config) -> "DynamoAdmissionGate":
        # boto3 ships in the Lambda Python base image. Keeping this import lazy
        # lets the pure unit tests run without AWS packages installed.
        import boto3  # type: ignore

        table = boto3.resource("dynamodb").Table(config.budget_table)
        return cls(table, config)

    def admit(self, when: datetime) -> AdmissionReceipt:
        period = when.astimezone(timezone.utc).strftime("%Y-%m")
        retry_after = seconds_until_next_month(when)
        request_cap = self.config.monthly_request_cap
        budget_cap = self.config.monthly_estimated_microusd_cap
        cost = self.config.estimated_microusd_per_request
        if request_cap < 1:
            raise AdmissionLimitExceeded("monthly_request_cap_exceeded", retry_after)
        if budget_cap < cost:
            raise AdmissionLimitExceeded(
                "monthly_estimated_budget_cap_exceeded", retry_after
            )

        # The conditional update serializes concurrent admissions on the monthly
        # item and prevents either configured total from being crossed.
        try:
            result = self.table.update_item(
                Key={"period": period},
                UpdateExpression=(
                    "SET #requests = if_not_exists(#requests, :zero) + :one, "
                    "#estimated = if_not_exists(#estimated, :zero) + :cost, "
                    "#expires = :expires"
                ),
                ConditionExpression=(
                    "(attribute_not_exists(#requests) OR #requests < :request_cap) "
                    "AND (attribute_not_exists(#estimated) OR #estimated <= :budget_before)"
                ),
                ExpressionAttributeNames={
                    "#requests": "requests",
                    "#estimated": "estimated_microusd",
                    "#expires": "expires_at",
                },
                ExpressionAttributeValues={
                    ":zero": 0,
                    ":one": 1,
                    ":cost": cost,
                    ":request_cap": request_cap,
                    ":budget_before": budget_cap - cost,
                    ":expires": int(when.timestamp()) + (400 * 24 * 60 * 60),
                },
                ReturnValues="ALL_NEW",
            )
        except Exception as error:
            code = getattr(error, "response", {}).get("Error", {}).get("Code")
            if code != "ConditionalCheckFailedException":
                raise
            current = self.table.get_item(
                Key={"period": period}, ConsistentRead=True
            ).get("Item", {})
            requests_used = int(current.get("requests", 0))
            estimated_used = int(current.get("estimated_microusd", 0))
            limit_code = (
                "monthly_request_cap_exceeded"
                if requests_used >= request_cap
                else "monthly_estimated_budget_cap_exceeded"
            )
            raise AdmissionLimitExceeded(limit_code, retry_after) from None

        attributes = result["Attributes"]
        return AdmissionReceipt(
            period=period,
            requests_used=int(attributes["requests"]),
            estimated_microusd_used=int(attributes["estimated_microusd"]),
        )

    def preflight(self, when: datetime) -> None:
        """Reject an already exhausted month before PIL decode; admit remains atomic."""
        retry_after = seconds_until_next_month(when)
        request_cap = self.config.monthly_request_cap
        budget_cap = self.config.monthly_estimated_microusd_cap
        cost = self.config.estimated_microusd_per_request
        if request_cap < 1:
            raise AdmissionLimitExceeded("monthly_request_cap_exceeded", retry_after)
        if budget_cap < cost:
            raise AdmissionLimitExceeded(
                "monthly_estimated_budget_cap_exceeded", retry_after
            )
        period = when.astimezone(timezone.utc).strftime("%Y-%m")
        current = self.table.get_item(
            Key={"period": period}, ConsistentRead=True
        ).get("Item", {})
        requests_used = int(current.get("requests", 0))
        estimated_used = int(current.get("estimated_microusd", 0))
        if requests_used >= request_cap:
            raise AdmissionLimitExceeded("monthly_request_cap_exceeded", retry_after)
        if estimated_used > budget_cap - cost:
            raise AdmissionLimitExceeded(
                "monthly_estimated_budget_cap_exceeded", retry_after
            )


_CONFIG: Config | None = None
_GATE: DynamoAdmissionGate | None = None
_DETECTOR: Any | None = None


def get_config() -> Config:
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = Config.from_env()
    return _CONFIG


def get_gate(config: Config) -> DynamoAdmissionGate:
    global _GATE
    if _GATE is None:
        _GATE = DynamoAdmissionGate.from_config(config)
    return _GATE


def get_detector(config: Config) -> Any:
    global _DETECTOR
    if _DETECTOR is None:
        from detector import YoloOnnxDetector

        _DETECTOR = YoloOnnxDetector(
            model_path=config.model_path,
            parity_path=config.model_parity_path,
            model_version=config.model_version,
            confidence_threshold=config.confidence_threshold,
            max_decoded_pixels=config.max_decoded_pixels,
        )
    return _DETECTOR


def seconds_until_next_month(when: datetime) -> int:
    current = when.astimezone(timezone.utc)
    year = current.year + (1 if current.month == 12 else 0)
    month = 1 if current.month == 12 else current.month + 1
    next_month = datetime(year, month, 1, tzinfo=timezone.utc)
    return max(1, int((next_month - current).total_seconds()))


def _headers(event: dict[str, Any]) -> dict[str, str]:
    return {
        str(key).lower(): str(value)
        for key, value in (event.get("headers") or {}).items()
        if value is not None
    }


def _initial_request_id(event: dict[str, Any], lambda_context: Any) -> str:
    candidate = _headers(event).get("x-request-id", "").strip()
    if REQUEST_ID_RE.fullmatch(candidate):
        return candidate
    aws_id = getattr(lambda_context, "aws_request_id", None)
    if isinstance(aws_id, str) and REQUEST_ID_RE.fullmatch(aws_id):
        return aws_id
    return str(uuid.uuid4())


def _decode_event_body(event: dict[str, Any], max_bytes: int) -> bytes:
    body = event.get("body")
    if not isinstance(body, str) or not body:
        raise ServiceError(400, "bad_request", "Send a JSON request body.")
    try:
        raw = base64.b64decode(body, validate=True) if event.get("isBase64Encoded") else body.encode("utf-8")
    except (ValueError, binascii.Error, UnicodeError) as error:
        raise ServiceError(400, "bad_request", "The request body encoding is invalid.") from error
    if len(raw) > max_bytes:
        raise ServiceError(
            413, "request_too_large", "The JSON request body is too large."
        )
    return raw


def _image_magic_matches(mime: str, content: bytes) -> bool:
    if mime == "image/jpeg":
        return content.startswith(b"\xff\xd8\xff")
    if mime == "image/png":
        return content.startswith(b"\x89PNG\r\n\x1a\n")
    if mime == "image/webp":
        return len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP"
    return False


def _decode_image(value: Any, index: int, config: Config) -> ImageInput:
    if (
        not isinstance(value, dict)
        or set(value) != {"data_url"}
        or not isinstance(value.get("data_url"), str)
    ):
        raise ServiceError(
            400, "bad_image", f"images[{index}] must contain only a data_url."
        )
    match = DATA_URL_RE.fullmatch(value["data_url"])
    if not match:
        raise ServiceError(
            400,
            "bad_image",
            f"images[{index}] must be a base64 JPEG, PNG or WebP data URL.",
        )
    mime = match.group(1).lower().replace("image/jpg", "image/jpeg")
    encoded = match.group(2)
    encoded += "=" * ((4 - len(encoded) % 4) % 4)
    try:
        content = base64.b64decode(encoded, altchars=b"-_", validate=True)
    except (ValueError, binascii.Error) as error:
        raise ServiceError(
            400, "bad_image", f"images[{index}] contains invalid base64."
        ) from error
    if not content or len(content) > config.max_image_bytes:
        raise ServiceError(
            413,
            "image_too_large",
            f"images[{index}] is empty or exceeds the per-image limit.",
        )
    if not _image_magic_matches(mime, content):
        raise ServiceError(
            400,
            "bad_image",
            f"images[{index}] content does not match its media type.",
        )
    return ImageInput(content=content, mime=mime)


def parse_request(
    event: dict[str, Any], config: Config, initial_request_id: str
) -> DetectionRequest:
    request_context = event.get("requestContext") or {}
    http_context = request_context.get("http") or {}
    method = str(http_context.get("method") or event.get("httpMethod") or "").upper()
    path = str(http_context.get("path") or event.get("rawPath") or event.get("path") or "")
    if method != "POST" or path not in {"/v1/detect", ""}:
        raise ServiceError(404, "not_found", "This route does not exist.")
    content_type = _headers(event).get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise ServiceError(415, "unsupported_media_type", "Content-Type must be application/json.")
    raw = _decode_event_body(event, config.max_json_body_bytes)
    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ServiceError(400, "bad_json", "The request body is not valid JSON.") from error
    if not isinstance(body, dict):
        raise ServiceError(400, "bad_json", "The request body must be a JSON object.")
    if body.get("version") != 1 or body.get("task") != "road_damage_detection":
        raise ServiceError(409, "contract_mismatch", "This gateway requires contract version 1.")
    if body.get("schema_version") != 4 or body.get("prompt_version") != "road-damage-v5":
        raise ServiceError(409, "schema_version_mismatch", "This gateway requires road-damage-v5 schema 4.")
    capture_mode = str(body.get("capture_mode", ""))
    language = str(body.get("language", ""))
    if capture_mode not in ALLOWED_CAPTURE_MODES:
        raise ServiceError(400, "bad_capture_mode", "capture_mode must be manual or drive.")
    if language not in ALLOWED_LANGUAGES:
        raise ServiceError(400, "bad_language", "language must be en or kn.")
    body_request_id = str(body.get("request_id", "")).strip()
    if not REQUEST_ID_RE.fullmatch(body_request_id):
        raise ServiceError(400, "bad_request_id", "request_id is invalid.")
    header_request_id = _headers(event).get("x-request-id", "").strip()
    if header_request_id and not REQUEST_ID_RE.fullmatch(header_request_id):
        raise ServiceError(400, "bad_request_id", "X-Request-ID is invalid.")
    if header_request_id and not hmac.compare_digest(header_request_id, body_request_id):
        raise ServiceError(400, "request_id_mismatch", "Header and body request IDs differ.")
    request_id = body_request_id or initial_request_id
    values = body.get("images")
    if not isinstance(values, list) or len(values) != 1:
        raise ServiceError(400, "bad_image_count", "Send exactly one image.")
    images = tuple(_decode_image(value, index, config) for index, value in enumerate(values))
    return DetectionRequest(
        request_id=request_id,
        capture_mode=capture_mode,
        language=language,
        images=images,
    )


def authenticate(event: dict[str, Any], config: Config) -> None:
    headers = _headers(event)
    # API Gateway AWS_IAM authentication owns the Authorization header in
    # production. Retain the project token as a second, Lambda-local gate under
    # X-Yolo-API-Key; accepting the legacy Bearer shape keeps direct/local tests
    # and a staged rollout backward compatible.
    token = headers.get("x-yolo-api-key", "").strip()
    if not token:
        authorization = headers.get("authorization", "")
        token = authorization[7:] if authorization.startswith("Bearer ") else ""
    if not token or len(token) > 2048:
        raise ServiceError(401, "unauthorized", "The gateway credential is invalid.")
    supplied_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(supplied_hash, config.api_key_sha256):
        raise ServiceError(401, "unauthorized", "The gateway credential is invalid.")


def validate_decodable_images(images: tuple[ImageInput, ...], config: Config) -> None:
    """Fully decode bounded images before consuming either monthly counter."""
    try:
        from detector import validate_image_payloads

        validate_image_payloads(images, max_decoded_pixels=config.max_decoded_pixels)
    except ValueError as error:
        raise ServiceError(
            400, "bad_image", "One or more images could not be decoded safely."
        ) from error


def validate_release_configuration(config: Config) -> None:
    """Bind the packaged bytes and decision threshold before budget admission."""
    try:
        from detector import validate_release_bundle

        validate_release_bundle(
            config.model_path,
            os.environ.get("MODEL_MANIFEST_PATH", "/opt/model/model-manifest.json"),
            config.model_parity_path,
            config.model_version,
            config.confidence_threshold,
            config.max_decoded_pixels,
        )
    except (OSError, RuntimeError, ValueError) as error:
        raise ServiceError(
            503,
            "service_not_configured",
            "The evaluated YOLO release bundle is missing or inconsistent.",
        ) from error
    except ImportError as error:
        raise ServiceError(
            503, "service_not_configured", "The image validation runtime is unavailable."
        ) from error


def validate_verdict(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != VERDICT_KEYS:
        raise ServiceError(502, "bad_model_response", "The detector returned an invalid verdict.")
    if value["image_quality"] not in {"acceptable", "rejected"}:
        raise ServiceError(502, "bad_model_response", "The detector returned an invalid verdict.")
    if value["assessment"] not in {"damaged", "undamaged"}:
        raise ServiceError(502, "bad_model_response", "The detector returned an invalid verdict.")
    if value["damage_type"] not in {"pothole_cavity", None}:
        raise ServiceError(502, "bad_model_response", "The detector returned an invalid verdict.")
    if value["size"] not in {"small", "medium", "large", None}:
        raise ServiceError(502, "bad_model_response", "The detector returned an invalid verdict.")
    if not isinstance(value["description"], str) or not value["description"].strip():
        raise ServiceError(502, "bad_model_response", "The detector returned an invalid verdict.")
    damaged = value["assessment"] == "damaged"
    if damaged != (value["damage_type"] == "pothole_cavity"):
        raise ServiceError(502, "bad_model_response", "The detector returned a contradictory verdict.")
    if not damaged and value["size"] is not None:
        raise ServiceError(502, "bad_model_response", "The detector returned a contradictory verdict.")
    if value["image_quality"] == "rejected" and damaged:
        raise ServiceError(502, "bad_model_response", "The detector returned a contradictory verdict.")
    result = dict(value)
    result["description"] = value["description"].strip()[:1000]
    return result


def _response(
    status: int,
    request_id: str,
    payload: dict[str, Any],
    retry_after: int | None = None,
) -> dict[str, Any]:
    headers = {
        "content-type": "application/json; charset=utf-8",
        "cache-control": "no-store",
        "x-request-id": request_id,
    }
    if retry_after is not None:
        headers["retry-after"] = str(retry_after)
    return {
        "statusCode": status,
        "headers": headers,
        "body": json.dumps({"request_id": request_id, **payload}, separators=(",", ":")),
        "isBase64Encoded": False,
    }


def _safe_log(context: RequestContext, started: float) -> None:
    record: dict[str, Any] = {
        "event": "yolo_request_complete",
        "request_id": context.request_id,
        "aws_request_id": context.aws_request_id,
        "outcome": context.outcome,
        "status": context.status,
        "admitted": context.admitted,
        "latency_ms": round((time.monotonic() - started) * 1000),
    }
    if context.receipt:
        record.update(
            {
                "budget_period": context.receipt.period,
                "monthly_requests_used": context.receipt.requests_used,
                "monthly_estimated_microusd_used": context.receipt.estimated_microusd_used,
            }
        )
    if context.model_version:
        record["model_version"] = context.model_version
    print(json.dumps(record, separators=(",", ":"), sort_keys=True), flush=True)


def handle_request(
    event: dict[str, Any],
    lambda_context: Any = None,
    *,
    config: Config | None = None,
    gate: Any = None,
    detector: Any = None,
    image_validator: Callable[[tuple[ImageInput, ...], Config], None] | None = None,
    release_validator: Callable[[Config], None] | None = None,
    now: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    request_id = _initial_request_id(event, lambda_context)
    context = RequestContext(
        request_id=request_id,
        aws_request_id=getattr(lambda_context, "aws_request_id", None),
    )
    try:
        active_config = config or get_config()
        authenticate(event, active_config)
        request = parse_request(event, active_config, request_id)
        context.request_id = request.request_id
        instant = (now or (lambda: datetime.now(timezone.utc)))()
        active_gate = gate or get_gate(active_config)
        # The read-only preflight avoids expensive PIL decode/model checks once a
        # month is already exhausted. The conditional update below is still the
        # only admission authority and closes races between concurrent requests.
        preflight = getattr(active_gate, "preflight", None)
        if callable(preflight):
            preflight(instant)
        (image_validator or validate_decodable_images)(request.images, active_config)
        if detector is None:
            (release_validator or validate_release_configuration)(active_config)
        receipt = active_gate.admit(instant)
        context.admitted = True
        context.receipt = receipt

        # Model initialization happens after admission. An initialization or
        # inference failure still consumes the admitted unit, which makes the cap
        # fail closed under repeated bad-model retries.
        active_detector = detector or get_detector(active_config)
        context.model_version = str(
            getattr(active_detector, "model_version", active_config.model_version)
        )[:80]
        verdict = validate_verdict(
            active_detector.analyse(
                request.images,
                capture_mode=request.capture_mode,
                language=request.language,
            )
        )
        context.status = 200
        context.outcome = verdict["assessment"]
        return _response(
            200,
            context.request_id,
            {"verdict": verdict, "model": context.model_version},
        )
    except ServiceError as error:
        context.status = error.status
        context.outcome = error.code
        return _response(
            error.status,
            context.request_id,
            {"error": error.code, "message": error.message},
            error.retry_after,
        )
    except Exception as error:  # Fail closed without returning or logging internals.
        context.status = 503
        context.outcome = "inference_unavailable" if context.admitted else "service_unavailable"
        # Exception type is operationally useful and cannot contain image/body data.
        print(
            json.dumps(
                {
                    "event": "yolo_internal_error",
                    "request_id": context.request_id,
                    "error_type": type(error).__name__,
                },
                separators=(",", ":"),
                sort_keys=True,
            ),
            flush=True,
        )
        return _response(
            503,
            context.request_id,
            {
                "error": context.outcome,
                "message": "The YOLO detector is temporarily unavailable.",
            },
        )
    finally:
        _safe_log(context, started)


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    return handle_request(event, context)
