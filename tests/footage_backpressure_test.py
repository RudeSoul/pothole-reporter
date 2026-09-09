# -*- coding: utf-8 -*-
"""Recorded-footage analysis drains every frame once, with bounded concurrency.

This is a local browser regression: it records a short synthetic WebM, stores it as
two consecutive clips, samples them densely enough to produce roughly 60 frames,
and replaces the detector at the page
API boundary.  No OpenAI or project-service request is made.

The first attempt for five frames fails transiently.  That reproduces the old
failure mode, where three independent seeker loops submitted about 11 frames and
then abandoned the rest of the video.  Starting the same analysis twice also
guards the UI race that used to submit every frame twice.
"""

import json
import pathlib
import sys

from playwright.sync_api import sync_playwright


APP = "http://localhost:8765/"
DRIVE_ID = "backpressure-overlap-drive"
fails = []
remote_leaks = []


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(args=[
        "--disable-web-security",
        "--allow-running-insecure-content",
        "--use-fake-device-for-media-stream",
        "--use-fake-ui-for-media-stream",
    ])
    context = browser.new_context(viewport={"width": 390, "height": 844})

    def block_remote(route):
        url = route.request.url
        if url.startswith(APP) or url.startswith("blob:") or url.startswith("data:"):
            route.continue_()
        elif url == "https://pothole-detect.gauravsen.workers.dev/v1/health":
            # The app probes health at startup; keep that unrelated bootstrap request
            # deterministic while still failing on any analysis-time network leak.
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"ok": True, "shared_vision_configured": True}),
            )
        else:
            remote_leaks.append(url)
            route.abort()

    context.route("**/*", block_remote)
    page = context.new_page()
    page.goto(APP)
    page.wait_for_load_state("networkidle")
    page.wait_for_function("typeof StandaloneAPI !== 'undefined'", timeout=30_000)

    result = page.evaluate(
        r"""async (driveId) => {
          await StandaloneAPI.handle("/api/reports", { method: "DELETE" });
          localStorage.setItem("debug_mode", "0");
          localStorage.removeItem("keep_frames");
          // The detector boundary is stubbed below; bypass the unrelated remote-health
          // preflight so this test measures only VOD scheduling and retries.
          health.ai_configured = true;
          // Satisfy the local availability gate without contacting either provider;
          // `/api/frame` is replaced below before analysis begins.
          localStorage.setItem("vision_provider", "personal");
          localStorage.setItem("openai_key", "test-only-not-a-real-key");

          const stream = await navigator.mediaDevices.getUserMedia({
            video: { width: 320, height: 240 }, audio: false,
          });
          const mime = ["video/webm;codecs=vp8", "video/webm"]
            .find((type) => MediaRecorder.isTypeSupported(type));
          if (!mime) throw new Error("Chromium exposes no WebM MediaRecorder");
          const recorder = new MediaRecorder(stream, { mimeType: mime });
          const parts = [];
          recorder.ondataavailable = (event) => {
            if (event.data && event.data.size) parts.push(event.data);
          };
          recorder.start();
          await new Promise((resolve) => setTimeout(resolve, 3200));
          await new Promise((resolve) => {
            recorder.onstop = resolve;
            recorder.stop();
          });
          stream.getTracks().forEach((track) => track.stop());
          const blob = new Blob(parts, { type: mime });
          if (!blob.size) throw new Error("MediaRecorder produced an empty clip");

          const stored = new FormData();
          stored.append("segment", blob, "backpressure.webm");
          stored.append("drive_id", driveId);
          stored.append("seq", "0");
          stored.append("recording_started_at_ms", "1800000000000");
          stored.append("source_offset_ms", "0");
          await StandaloneAPI.handle("/api/footage", { method: "POST", body: stored });

          // Reuse the valid bytes as a second segment. This forces the analyser to
          // tear down one decoder pool and open another—the Android WebView boundary
          // where analysis previously froze around 12–15 frames.
          const storedSecond = new FormData();
          storedSecond.append("segment", blob, "backpressure-2.webm");
          storedSecond.append("drive_id", driveId);
          storedSecond.append("seq", "1");
          storedSecond.append("recording_started_at_ms", "1800000003200");
          storedSecond.append("source_offset_ms", "3200");
          await StandaloneAPI.handle("/api/footage", { method: "POST", body: storedSecond });

          // An unreadable trailing segment must make the run incomplete and keep all
          // source footage; it must never be silently omitted from the denominator.
          const invalid = new Blob(["not a video segment"], { type: mime });
          const storedInvalid = new FormData();
          storedInvalid.append("segment", invalid, "backpressure-invalid.webm");
          storedInvalid.append("drive_id", driveId);
          storedInvalid.append("seq", "2");
          storedInvalid.append("recording_started_at_ms", "1800000006400");
          storedInvalid.append("source_offset_ms", "6400");
          await StandaloneAPI.handle("/api/footage", { method: "POST", body: storedInvalid });

          // Two three-second clips now plan about 60 frames, making the old ~11-frame
          // early-abort and the over-six concurrent submissions deterministic.
          VOD_STEP_S = 0.1;
          const originalApi = window.api;
          const attempts = new Map();
          const transientKeys = new Set();
          const completedKeys = new Set();
          const analysisBodies = [];
          const alerts = [];
          let active = 0;
          let maxActive = 0;
          let frameCalls = 0;
          let forcedFrameFailures = 0;
          const originalToBlob = HTMLCanvasElement.prototype.toBlob;
          HTMLCanvasElement.prototype.toBlob = function(callback, ...args) {
            if (!forcedFrameFailures) {
              forcedFrameFailures++;
              callback(null);
              return;
            }
            return originalToBlob.call(this, callback, ...args);
          };

          window.alert = (message) => alerts.push(String(message));
          window.confirm = () => true;
          window.api = async (path, options = {}) => {
            if (path === "/api/frame") {
              const key = String(options.body.get("source_event_key") || "missing");
              const count = (attempts.get(key) || 0) + 1;
              attempts.set(key, count);
              frameCalls++;
              active++;
              maxActive = Math.max(maxActive, active);
              try {
                await new Promise((resolve) => setTimeout(resolve, 30));
                if (count === 1 && transientKeys.size < 5) {
                  transientKeys.add(key);
                  const error = new Error("synthetic transient outage");
                  error.timeout = true;
                  throw error;
                }
                completedKeys.add(key);
                return {
                  analyzed: true,
                  accepted: false,
                  stored: false,
                  found: false,
                  duplicate: false,
                  decision: "reject",
                  image_quality: "acceptable",
                  assessment: "undamaged",
                  damage_type: null,
                  size: null,
                  description: "The road is intact.",
                };
              } finally {
                active--;
              }
            }
            if (path === `/api/drives/${encodeURIComponent(driveId)}/analysis`
                && options.method === "POST") {
              analysisBodies.push(JSON.parse(options.body || "{}"));
            }
            return originalApi(path, options);
          };

          let settled;
          try {
            // The second user action must join the first run.  It must not start a
            // second decoder pool or make a second set of model requests.
            settled = await Promise.allSettled([
              analyseFootage(driveId, { started_at: 1800000000, gps_track: [] }),
              analyseFootage(driveId, { started_at: 1800000000, gps_track: [] }),
            ]);
          } finally {
            window.api = originalApi;
            HTMLCanvasElement.prototype.toBlob = originalToBlob;
          }

          const retained = await StandaloneAPI.handle(`/api/footage/${driveId}/blobs`);

          return {
            blobBytes: blob.size,
            maxAllowed: MAX_IN_FLIGHT,
            maxActive,
            frameCalls,
            uniqueFrames: attempts.size,
            completedFrames: completedKeys.size,
            transientFrames: transientKeys.size,
            forcedFrameFailures,
            retainedClips: retained.blobs.length,
            maxAttemptsForOneFrame: Math.max(...attempts.values()),
            analysisBodies,
            alerts,
            settled: settled.map((item) => ({
              status: item.status,
              reason: item.status === "rejected" ? String(item.reason) : null,
            })),
          };
        }""",
        DRIVE_ID,
    )
    browser.close()


if remote_leaks:
    fails.append(f"analysis attempted remote requests: {remote_leaks}")
if any(item["status"] != "fulfilled" for item in result["settled"]):
    fails.append(f"overlapping callers did not both finish: {result['settled']}")
if result["uniqueFrames"] < 50:
    fails.append(
        "analysis abandoned the clip early: "
        f"only {result['uniqueFrames']} unique frames reached the detector"
    )
if result["completedFrames"] != result["uniqueFrames"]:
    fails.append(
        "transiently failed frames were not drained successfully: "
        f"{result['completedFrames']} completed of {result['uniqueFrames']}"
    )
if result["transientFrames"] != 5:
    fails.append(f"test did not inject all five transient failures: {result['transientFrames']}")
if result["forcedFrameFailures"] != 1:
    fails.append(f"test did not inject one decoder/frame failure: {result['forcedFrameFailures']}")
if result["retainedClips"] != 3:
    fails.append(
        "an incomplete decoder run deleted its source footage: "
        f"{result['retainedClips']} of 3 clips remain"
    )
expected_attempts = result["uniqueFrames"] + result["transientFrames"]
if result["frameCalls"] != expected_attempts:
    fails.append(
        "overlapping analysis duplicated detector work: "
        f"{result['frameCalls']} calls for {result['uniqueFrames']} frames and "
        f"{result['transientFrames']} intentional retries (expected {expected_attempts})"
    )
if result["maxActive"] > result["maxAllowed"]:
    fails.append(
        f"detector concurrency reached {result['maxActive']}; "
        f"MAX_IN_FLIGHT is {result['maxAllowed']}"
    )
if len(result["analysisBodies"]) != 1:
    fails.append(
        "overlapping analyseFootage calls committed multiple analysis summaries: "
        f"{len(result['analysisBodies'])}"
    )
else:
    summary = result["analysisBodies"][0]
    if summary.get("checked") != result["uniqueFrames"]:
        fails.append(
            "final summary did not claim every successfully drained frame: "
            f"{json.dumps(summary, sort_keys=True)}"
        )
if len(result["alerts"]) != 1:
    fails.append(f"one joined run produced {len(result['alerts'])} completion alerts")
elif "Could not finish" not in result["alerts"][0]:
    fails.append(f"decoder failures were presented as a successful run: {result['alerts'][0]!r}")

print(
    "  frames: "
    f"{result['completedFrames']}/{result['uniqueFrames']} completed, "
    f"{result['frameCalls']} attempts"
)
print(
    "  concurrency: "
    f"{result['maxActive']}/{result['maxAllowed']} max active"
)
print(f"  joined summaries/alerts: {len(result['analysisBodies'])}/{len(result['alerts'])}")

if fails:
    print("\nFAIL")
    for failure in fails:
        print("  -", failure)
    sys.exit(1)
print("\nFOOTAGE BACKPRESSURE TEST PASS")
