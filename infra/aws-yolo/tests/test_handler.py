from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import pathlib
import sys
import unittest
from datetime import datetime, timezone


SERVICE = pathlib.Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from handler import (  # noqa: E402
    AdmissionLimitExceeded,
    AdmissionReceipt,
    Config,
    DynamoAdmissionGate,
    handle_request,
)


TOKEN = "test-token-with-enough-entropy-for-tests"


def config(**overrides):
    values = {
        "api_key_sha256": hashlib.sha256(TOKEN.encode()).hexdigest(),
        "budget_table": "test-budget",
        "monthly_request_cap": 10,
        "monthly_estimated_microusd_cap": 20_000,
        "estimated_microusd_per_request": 1_000,
        "max_json_body_bytes": 5_500_000,
        "max_image_bytes": 3_500_000,
        "max_decoded_pixels": 12_000_000,
        "model_path": "/unused/model.onnx",
        "model_parity_path": "/unused/runtime-parity.json",
        "model_version": "pothole-yolo-test",
        "confidence_threshold": 0.5,
    }
    values.update(overrides)
    return Config(**values)


def verdict(damaged=True):
    return {
        "image_quality": "acceptable",
        "assessment": "damaged" if damaged else "undamaged",
        "damage_type": "pothole_cavity" if damaged else None,
        "size": None,
        "description": "A test verdict.",
    }


def event(
    *, token=TOKEN, header_request_id="request-123", body_request_id="request-123",
    capture_mode="manual", extra_image_field=False, token_header="authorization"
):
    # A real 2x2 JPEG. Decoder validation deliberately occurs before admission.
    image = (
        "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkS"
        "Ew8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJ"
        "CQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIy"
        "MjIyMjIyMjIyMjIyMjL/wAARCAACAAIDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEA"
        "AAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIh"
        "MUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6"
        "Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZ"
        "mqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx"
        "8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREA"
        "AgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAV"
        "YnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hp"
        "anN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPE"
        "xcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwAo"
        "oooA/9k="
    )
    image_input = {"data_url": f"data:image/jpeg;base64,{image}"}
    if extra_image_field:
        image_input["unexpected"] = "value"
    payload = {
        "version": 1,
        "task": "road_damage_detection",
        "request_id": body_request_id,
        "model": "ignored-client-model",
        "capture_mode": capture_mode,
        "language": "en",
        "prompt_version": "road-damage-v5",
        "schema_version": 4,
        "images": [image_input],
    }
    return {
        "requestContext": {"http": {"method": "POST", "path": "/v1/detect"}},
        "headers": {
            token_header: token if token_header == "x-yolo-api-key" else f"Bearer {token}",
            "content-type": "application/json",
            "x-request-id": header_request_id,
        },
        "body": json.dumps(payload),
        "isBase64Encoded": False,
    }


class FakeGate:
    def __init__(self, error=None, preflight_error=None):
        self.error = error
        self.preflight_error = preflight_error
        self.calls = 0
        self.preflight_calls = 0

    def preflight(self, when):
        self.preflight_calls += 1
        if self.preflight_error:
            raise self.preflight_error

    def admit(self, when):
        self.calls += 1
        if self.error:
            raise self.error
        return AdmissionReceipt("2026-09", 2, 2_000)


class FakeDetector:
    model_version = "pothole-yolo-test"

    def __init__(self):
        self.calls = 0

    def analyse(self, images, *, capture_mode, language):
        self.calls += 1
        self.last = (images, capture_mode, language)
        return verdict()


class HandlerTests(unittest.TestCase):
    def call(self, supplied_event, gate, detector):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            response = handle_request(
                supplied_event,
                config=config(),
                gate=gate,
                detector=detector,
                now=lambda: datetime(2026, 9, 5, tzinfo=timezone.utc),
            )
        return response, output.getvalue()

    def test_valid_request_is_admitted_once_and_returns_v5_envelope(self):
        gate = FakeGate()
        detector = FakeDetector()
        response, logs = self.call(event(), gate, detector)
        body = json.loads(response["body"])
        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(response["headers"]["x-request-id"], "request-123")
        self.assertEqual(body["request_id"], "request-123")
        self.assertEqual(body["model"], "pothole-yolo-test")
        self.assertEqual(body["verdict"]["assessment"], "damaged")
        self.assertEqual(
            set(body["verdict"]),
            {"image_quality", "assessment", "damage_type", "size", "description"},
        )
        self.assertEqual(gate.calls, 1)
        self.assertEqual(gate.preflight_calls, 1)
        self.assertEqual(detector.calls, 1)
        self.assertIn('"monthly_requests_used":2', logs)
        self.assertNotIn("/9j/", logs)

    def test_drive_capture_mode_is_preserved(self):
        gate = FakeGate()
        detector = FakeDetector()
        response, _ = self.call(event(capture_mode="drive"), gate, detector)
        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(detector.last[1], "drive")

    def test_unexpected_image_metadata_is_rejected_before_admission(self):
        gate = FakeGate()
        detector = FakeDetector()
        response, _ = self.call(event(extra_image_field=True), gate, detector)
        self.assertEqual(response["statusCode"], 400)
        self.assertEqual(json.loads(response["body"])["error"], "bad_image")
        self.assertEqual(gate.calls, 0)
        self.assertEqual(detector.calls, 0)

    def test_multiple_images_are_rejected_before_admission(self):
        supplied = event()
        body = json.loads(supplied["body"])
        body["images"].append(dict(body["images"][0]))
        supplied["body"] = json.dumps(body)
        gate = FakeGate()
        detector = FakeDetector()
        response, _ = self.call(supplied, gate, detector)
        self.assertEqual(response["statusCode"], 400)
        self.assertEqual(json.loads(response["body"])["error"], "bad_image_count")
        self.assertEqual(gate.calls, 0)
        self.assertEqual(detector.calls, 0)

    def test_signed_gateway_token_header_is_verified_inside_lambda(self):
        gate = FakeGate()
        detector = FakeDetector()
        response, _ = self.call(
            event(token_header="x-yolo-api-key"), gate, detector
        )
        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(gate.calls, 1)
        self.assertEqual(detector.calls, 1)

    def test_bad_bearer_never_consumes_admission(self):
        gate = FakeGate()
        detector = FakeDetector()
        response, _ = self.call(event(token="wrong"), gate, detector)
        self.assertEqual(response["statusCode"], 401)
        self.assertEqual(gate.calls, 0)
        self.assertEqual(detector.calls, 0)

    def test_request_id_mismatch_never_consumes_admission(self):
        gate = FakeGate()
        detector = FakeDetector()
        response, _ = self.call(
            event(header_request_id="header-id", body_request_id="body-id"), gate, detector
        )
        self.assertEqual(response["statusCode"], 400)
        self.assertEqual(json.loads(response["body"])["error"], "request_id_mismatch")
        self.assertEqual(gate.calls, 0)
        self.assertEqual(detector.calls, 0)

    def test_monthly_cap_returns_429_before_model_load(self):
        limit = AdmissionLimitExceeded("monthly_request_cap_exceeded", 123)
        gate = FakeGate(preflight_error=limit)
        detector = FakeDetector()
        response, _ = self.call(event(), gate, detector)
        self.assertEqual(response["statusCode"], 429)
        self.assertEqual(response["headers"]["retry-after"], "123")
        self.assertEqual(json.loads(response["body"])["error"], "monthly_request_cap_exceeded")
        self.assertEqual(gate.preflight_calls, 1)
        self.assertEqual(gate.calls, 0)
        self.assertEqual(detector.calls, 0)

    def test_bad_image_never_consumes_admission(self):
        supplied = event()
        body = json.loads(supplied["body"])
        body["images"][0]["data_url"] = "https://example.test/private.jpg"
        supplied["body"] = json.dumps(body)
        gate = FakeGate()
        detector = FakeDetector()
        response, _ = self.call(supplied, gate, detector)
        self.assertEqual(response["statusCode"], 400)
        self.assertEqual(gate.calls, 0)
        self.assertEqual(detector.calls, 0)

    def test_magic_only_jpeg_never_consumes_admission(self):
        supplied = event()
        body = json.loads(supplied["body"])
        invalid = base64.b64encode(b"\xff\xd8\xffnot-a-jpeg").decode()
        body["images"][0]["data_url"] = f"data:image/jpeg;base64,{invalid}"
        supplied["body"] = json.dumps(body)
        gate = FakeGate()
        detector = FakeDetector()
        response, _ = self.call(supplied, gate, detector)
        self.assertEqual(response["statusCode"], 400)
        self.assertEqual(json.loads(response["body"])["error"], "bad_image")
        self.assertEqual(gate.calls, 0)
        self.assertEqual(detector.calls, 0)


class FakeTable:
    def __init__(self, result=None, error=None, current=None):
        self.result = result
        self.error = error
        self.current = current or {}
        self.update_kwargs = None
        self.get_kwargs = None

    def update_item(self, **kwargs):
        self.update_kwargs = kwargs
        if self.error:
            raise self.error
        return self.result

    def get_item(self, **kwargs):
        self.get_kwargs = kwargs
        return {"Item": self.current}


class ConditionalFailure(Exception):
    response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class DynamoGateTests(unittest.TestCase):
    def test_preflight_rejects_exhausted_month_before_atomic_admission(self):
        table = FakeTable(current={"requests": 10, "estimated_microusd": 10_000})
        gate = DynamoAdmissionGate(table, config())
        with self.assertRaises(AdmissionLimitExceeded) as caught:
            gate.preflight(datetime(2026, 9, 5, tzinfo=timezone.utc))
        self.assertEqual(caught.exception.code, "monthly_request_cap_exceeded")
        self.assertEqual(table.get_kwargs, {
            "Key": {"period": "2026-09"}, "ConsistentRead": True,
        })
        self.assertIsNone(table.update_kwargs)

    def test_atomic_update_enforces_both_caps(self):
        table = FakeTable(
            result={"Attributes": {"requests": 4, "estimated_microusd": 4_000}}
        )
        gate = DynamoAdmissionGate(table, config())
        receipt = gate.admit(datetime(2026, 9, 5, tzinfo=timezone.utc))
        self.assertEqual(receipt.requests_used, 4)
        self.assertIn("#requests < :request_cap", table.update_kwargs["ConditionExpression"])
        self.assertIn("#estimated <= :budget_before", table.update_kwargs["ConditionExpression"])
        values = table.update_kwargs["ExpressionAttributeValues"]
        self.assertEqual(values[":request_cap"], 10)
        self.assertEqual(values[":budget_before"], 19_000)

    def test_conditional_failure_reports_request_cap(self):
        table = FakeTable(
            error=ConditionalFailure(),
            current={"requests": 10, "estimated_microusd": 10_000},
        )
        gate = DynamoAdmissionGate(table, config())
        with self.assertRaises(AdmissionLimitExceeded) as caught:
            gate.admit(datetime(2026, 9, 5, tzinfo=timezone.utc))
        self.assertEqual(caught.exception.code, "monthly_request_cap_exceeded")

    def test_zero_budget_is_an_immediate_kill_switch(self):
        table = FakeTable()
        gate = DynamoAdmissionGate(
            table, config(monthly_estimated_microusd_cap=0)
        )
        with self.assertRaises(AdmissionLimitExceeded) as caught:
            gate.admit(datetime(2026, 9, 5, tzinfo=timezone.utc))
        self.assertEqual(
            caught.exception.code, "monthly_estimated_budget_cap_exceeded"
        )
        self.assertIsNone(table.update_kwargs)


if __name__ == "__main__":
    unittest.main()
