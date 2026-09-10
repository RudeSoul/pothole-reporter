# -*- coding: utf-8 -*-
"""Concurrent same-coordinate captures keep the authority result owned by each call."""

import json
import os
import sys
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright


APP = os.environ.get("POTHOLE_TEST_APP", "http://localhost:8765/")
SERVICE = "https://central-isolation.test"
fails = []
remote_leaks = []


with sync_playwright() as playwright:
    browser = playwright.chromium.launch()
    context = browser.new_context(viewport={"width": 390, "height": 844})
    context.add_init_script(script=f"""(() => {{
      localStorage.setItem("service_url", {json.dumps(SERVICE)});
      localStorage.setItem("vision_provider", "shared");
      localStorage.setItem("debug_mode", "1");
    }})();""")

    def initial_network(route):
        request = route.request
        url = request.url
        if url.startswith(APP) or url.startswith("blob:") or url.startswith("data:"):
            route.continue_()
            return
        if url.startswith(SERVICE):
            path = urlparse(url).path
            if path == "/v1/health":
                payload = {"ok": True, "shared_vision_configured": True, "ai_configured": True}
            elif path == "/v1/map":
                payload = {"type": "FeatureCollection", "features": [], "total": 0}
            elif path == "/v1/impact":
                payload = {"requests_total": 0, "requests": [], "potholes": {"total": 0}}
            else:
                payload = {"error": "unexpected_initial_request", "message": path}
            route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))
            return
        remote_leaks.append(url)
        route.abort()

    context.route("**/*", initial_network)
    page = context.new_page()
    page.goto(APP)
    page.wait_for_load_state("networkidle")
    page.wait_for_function("window.StandaloneAPI && typeof StandaloneAPI.handle === 'function'")

    result = page.evaluate(r"""async (serviceUrl) => {
      const originalFetch = window.fetch;
      const labelByObservation = new Map();
      const labelByTenderKey = new Map();
      const centralBodies = {};
      let detectorSequence = 0;
      const response = (value, status = 200) => new Response(JSON.stringify(value), {
        status, headers: { "content-type": "application/json" },
      });
      const sha256 = async (text) => [...new Uint8Array(await crypto.subtle.digest(
        "SHA-256", new TextEncoder().encode(text),
      ))].map((byte) => byte.toString(16).padStart(2, "0")).join("");
      const bodyOf = (options) => JSON.parse(options && options.body || "{}");
      const headerOf = (options, name) => new Headers(options && options.headers || {}).get(name);
      window.fetch = async (input, options = {}) => {
        const url = input && input.url ? input.url : String(input);
        if (url.endsWith("karnataka-bodies.json")) {
          return response({ bodies: { "999001": {
            name: "Test City Corporation", type: "CC", officer: "Commissioner",
            email: "commissioner@example.gov.in",
          } } });
        }
        if (!url.startsWith(serviceUrl)) return originalFetch(input, options);
        const path = new URL(url).pathname;
        if (path === "/v1/health") {
          return response({ ok: true, shared_vision_configured: true, ai_configured: true });
        }
        if (path === "/v1/installations") {
          return response({ install_id: "isolation-install" }, 201);
        }
        if (path === "/v1/vision/detect") {
          const body = bodyOf(options);
          const label = detectorSequence++ === 0 ? "A" : "B";
          labelByObservation.set(body.client_observation_id, label);
          labelByTenderKey.set(`tender-${await sha256(body.client_observation_id)}`, label);
          return response({
            request_id: `detect-${label}`,
            image_quality: "acceptable", assessment: "damaged",
            damage_type: "pothole_cavity", size: "medium",
            description: `Accepted capture ${label}`,
            detection_receipt: label === "A" ? "a".repeat(64) : "b".repeat(64),
          });
        }
        if (path === "/v1/tenders/resolve") {
          const body = bodyOf(options);
          const label = labelByTenderKey.get(headerOf(options, "Idempotency-Key"));
          // B finishes first. The old global coordinate cache then made A consume B's
          // municipal result even though A's own result says this is a national highway.
          await new Promise((resolve) => setTimeout(resolve, label === "A" ? 100 : 5));
          if (label === "A") {
            return response({
              request_id: "tender-A", reason: "national_highway", tender: null,
              jurisdiction: { road_ownership: "national_highway", highway_name: "NH 48" },
            });
          }
          return response({
            request_id: "tender-B", reason: null,
            jurisdiction: {
              road_ownership: "municipal", address: "Test Road, Test City",
              lgd: "999001", town: "Test City Corporation", town_type: "CC",
            },
            tender: {
              tender_number: "T-B", contractor: "Municipal Roads Ltd",
              title: "Repair Test Road", published: "01-08-2026",
              confidence: 0.96, match_method: "model_adjudicated",
            },
          });
        }
        if (path === "/v1/potholes/report") {
          const body = bodyOf(options);
          const label = labelByObservation.get(body.client_observation_id);
          centralBodies[label] = body;
          return response({
            request_id: `report-${label}`, duplicate: false, dedupe: null,
            pothole: {
              id: label === "A" ? 8101 : 8102, lat: body.lat, lng: body.lng,
              damage_type: body.damage_type, size: body.size,
              first_seen_at: body.observed_at, last_seen_at: body.observed_at,
              seen_count: 1, lgd: body.lgd_hint || null, town: body.town_hint || null,
            },
          }, 201);
        }
        return response({ error: "not_mocked", message: path }, 404);
      };

      const canvas = document.createElement("canvas");
      canvas.width = 32; canvas.height = 32;
      canvas.getContext("2d").fillRect(0, 0, 32, 32);
      const photo = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.8));
      const request = () => {
        const form = new FormData();
        form.append("photo", photo, "road.jpg");
        form.append("lat", "12.9716001");
        form.append("lng", "77.5946001");
        return StandaloneAPI.handle("/api/report", { method: "POST", body: form });
      };
      try {
        const reports = await Promise.all([request(), request()]);
        return {
          reports: Object.fromEntries(reports.map((report) => [
            String(report.vision_request_id).replace("detect-", ""), report,
          ])),
          centralBodies,
        };
      } finally {
        window.fetch = originalFetch;
      }
    }""", SERVICE)
    browser.close()


if remote_leaks:
    fails.append(f"real network request escaped deterministic test: {remote_leaks}")
reports = result["reports"]
highway = reports.get("A", {})
municipal = reports.get("B", {})
if highway.get("road_ownership") != "national_highway":
    fails.append(f"highway capture inherited another call's ownership: {highway}")
if highway.get("officer_email") or highway.get("tender_number") or highway.get("body_lgd"):
    fails.append(f"highway capture inherited municipal routing/tender: {highway}")
if highway.get("status") != "unrouted" or highway.get("tender_request_id") != "tender-A":
    fails.append(f"highway capture did not retain its exact resolver result: {highway}")
if municipal.get("road_ownership") != "municipal":
    fails.append(f"municipal capture lost its own ownership: {municipal}")
if municipal.get("officer_email") != "commissioner@example.gov.in":
    fails.append(f"municipal capture was not routed to its verified officer: {municipal}")
if municipal.get("tender_number") != "T-B" or municipal.get("body_lgd") != "999001":
    fails.append(f"municipal capture lost its tender/LGD: {municipal}")
if municipal.get("status") != "draft" or municipal.get("tender_request_id") != "tender-B":
    fails.append(f"municipal capture did not retain its exact resolver result: {municipal}")
central = result["centralBodies"]
if central.get("A", {}).get("lgd_hint") or central.get("A", {}).get("town_hint"):
    fails.append(f"highway shared-map write inherited municipal hints: {central.get('A')}")
if central.get("B", {}).get("lgd_hint") != "999001":
    fails.append(f"municipal shared-map write lost its own LGD hint: {central.get('B')}")

print("  ownership A/B:", highway.get("road_ownership"), municipal.get("road_ownership"))
print("  central LGD hints A/B:", central.get("A", {}).get("lgd_hint"),
      central.get("B", {}).get("lgd_hint"))
if fails:
    print("\nFAIL")
    for failure in fails:
        print("  -", failure)
    sys.exit(1)
print("\nCENTRAL RESOLUTION ISOLATION TEST PASS")
