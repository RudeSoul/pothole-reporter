# -*- coding: utf-8 -*-
"""Tester feedback is signed, survives a failed send, and is asked for once."""

import json
import sys

from playwright.sync_api import sync_playwright

from browser_test_utils import _central_service, open_app


SERVICE = "https://ffjvg34k07.execute-api.ap-south-1.amazonaws.com"

failures = []
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(args=["--disable-web-security"])
    context = browser.new_context(viewport={"width": 390, "height": 844})
    page = context.new_page()

    feedback_requests = []
    service_up = {"value": False}

    def central(route, request):
        if request.url.startswith(f"{SERVICE}/v1/feedback"):
            feedback_requests.append({
                "body": json.loads(request.post_data or "{}"),
                "headers": {k.lower(): v for k, v in request.headers.items()},
            })
            if not service_up["value"]:
                route.fulfill(status=503, headers={"content-type": "application/json"},
                              body=json.dumps({"error": "service_unavailable", "message": "down"}))
            else:
                route.fulfill(status=201, headers={"content-type": "application/json"},
                              body=json.dumps({"accepted": True, "created_at": 1}))
            return
        _central_service(route, request)

    open_app(page, "test-key-never-sent")
    page.unroute(f"{SERVICE}/**")
    page.route(f"{SERVICE}/**", central)
    page.evaluate("openSettings()")
    page.locator("#feedbackBtn").click()
    page.locator("#feedback").wait_for(state="visible")

    # Nothing to send: no request and a clear message.
    page.locator("#feedbackSend").click()
    if feedback_requests:
        failures.append("empty feedback reached the service")
    if "star" not in page.locator("#feedbackStatus").inner_text().lower():
        failures.append(f"empty feedback did not explain itself: {page.locator('#feedbackStatus').inner_text()}")

    # A malformed optional email is caught on the phone.
    page.locator("#feedbackStars button[data-rating='4']").click()
    page.locator("#feedbackEmail").fill("not-an-email")
    page.locator("#feedbackSend").click()
    if feedback_requests:
        failures.append("malformed email reached the service")

    # Service down: the entry is kept on the phone, not lost.
    page.locator("#feedbackMode").select_option("car")
    page.locator("#feedbackText").fill("Drive mode froze after ten minutes.")
    page.locator("#feedbackEmail").fill("tester@example.com")
    page.locator("#feedbackSend").click()
    page.wait_for_function("document.getElementById('feedbackStatus').textContent.includes('Saved on this phone')",
                           timeout=15_000)
    queued = page.evaluate("JSON.parse(localStorage.getItem('pending_feedback') || '[]')")
    if len(queued) != 1:
        failures.append(f"failed feedback was not queued: {queued}")
    if len(feedback_requests) != 1:
        failures.append(f"expected one failed attempt, saw {len(feedback_requests)}")

    # Connection returns: the same signed entry is resent once and the queue empties.
    service_up["value"] = True
    page.evaluate("window.dispatchEvent(new Event('online'))")
    page.wait_for_function("!localStorage.getItem('pending_feedback')", timeout=15_000)
    if len(feedback_requests) != 2:
        failures.append(f"expected a single retry, saw {len(feedback_requests)} requests")
    else:
        first, retry = feedback_requests
        body = retry["body"]
        expected = {"rating": 4, "text": "Drive mode froze after ten minutes.",
                    "test_mode": "car", "email": "tester@example.com"}
        for key, value in expected.items():
            if body.get(key) != value:
                failures.append(f"feedback {key} was {body.get(key)!r}, expected {value!r}")
        for header in ("x-install-id", "x-timestamp", "x-signature", "idempotency-key"):
            if not retry["headers"].get(header):
                failures.append(f"feedback request was not signed: missing {header}")
        if first["headers"].get("idempotency-key") != retry["headers"].get("idempotency-key"):
            failures.append("retry used a new idempotency key, so it could be counted twice")

    # The one-time nudge appears from the third report and never again once answered.
    page.evaluate("localStorage.removeItem('feedback_nudged'); show('home')")
    page.evaluate("maybeNudgeFeedback(2)")
    if page.locator("#feedbackNudge").is_visible():
        failures.append("nudge appeared before the third report")
    page.evaluate("maybeNudgeFeedback(3)")
    if not page.locator("#feedbackNudge").is_visible():
        failures.append("nudge did not appear at the third report")
    page.locator("#feedbackNudgeDismiss").click()
    page.evaluate("maybeNudgeFeedback(7)")
    if page.locator("#feedbackNudge").is_visible():
        failures.append("nudge came back after it was dismissed")

    browser.close()

if failures:
    print("FAIL")
    for failure in failures:
        print(" -", failure)
    sys.exit(1)
print("PASS tester feedback")
