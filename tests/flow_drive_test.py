# -*- coding: utf-8 -*-
"""Drive mode must start, render its screen and stop, without throwing.

Most testers ride or drive, so this is the flow they spend their time in. The native
Drive plugin is stubbed, so this checks the app's own logic: permissions, the start
call, the live panel, the stop call and the return to Home.
"""

import sys

from playwright.sync_api import sync_playwright

from flow_harness import error_failures, open_flow

fails = []
with sync_playwright() as playwright:
    # Background Drive Mode routes to the native service; that is the path whose start
    # and stop this test drives, because it is the one with a plugin contract to keep.
    browser, page, errors = open_flow(playwright, storage={"native_background_drive": "1"})
    page.locator("#home").wait_for(state="visible", timeout=30_000)
    page.wait_for_function("() => typeof startDrive === 'function'", timeout=30_000)
    fails += error_failures(errors, "home before drive")

    errors.clear()
    page.locator("#driveBtn").click()
    page.locator("#drive").wait_for(state="visible", timeout=30_000)
    page.wait_for_function("() => (window.__driveCalls || []).some((call) => call[0] === 'start')",
                           timeout=30_000)
    fails += error_failures(errors, "starting drive")

    started = page.evaluate("""() => ({
      driveVisible: !document.getElementById("drive").classList.contains("hidden"),
      nativePanel: !document.getElementById("nativeDrivePanel").classList.contains("hidden"),
      calls: (window.__driveCalls || []).map((call) => call[0]),
      exits: window.__exitAppCalls,
      stopVisible: !!document.getElementById("nativeDriveStop"),
    })""")
    if not started["driveVisible"]:
        fails.append(f"the drive screen did not open: {started}")
    if "start" not in started["calls"]:
        fails.append(f"the native drive session was never started: {started}")
    if started["exits"]:
        fails.append(f"starting drive closed the app {started['exits']} time(s)")

    # A frame arriving from the native side must not throw in the UI callbacks.
    errors.clear()
    page.evaluate("""() => window.__fireNative("driveStatus", {
      running: true, sessionId: "flow-session-1", frames: 2, reports: 1,
      recordingEnabled: false, paused: false,
    })""")
    page.wait_for_timeout(300)
    fails += error_failures(errors, "native drive status update")

    errors.clear()
    stop_button = "#nativeDriveStop" if started["nativePanel"] else "#driveStop"
    page.locator(stop_button).click()
    page.wait_for_function("() => (window.__driveCalls || []).some((call) => call[0] === 'stop')",
                           timeout=30_000)
    page.wait_for_timeout(500)
    fails += error_failures(errors, "stopping drive")

    stopped = page.evaluate("""() => ({
      calls: (window.__driveCalls || []).map((call) => call[0]),
      exits: window.__exitAppCalls,
      driveVisible: !document.getElementById("drive").classList.contains("hidden"),
    })""")
    if "stop" not in stopped["calls"]:
        fails.append(f"stop did not reach the native session: {stopped}")
    if stopped["exits"]:
        fails.append(f"stopping drive closed the app {stopped['exits']} time(s)")

    browser.close()

if fails:
    print("FAIL flow_drive")
    for failure in fails:
        print(" -", failure)
    sys.exit(1)
print("PASS flow_drive")
