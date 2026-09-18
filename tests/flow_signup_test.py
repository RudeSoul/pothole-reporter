# -*- coding: utf-8 -*-
"""A fresh install must get from first launch to Home without an error.

This is the screen v1.38.1 broke: Settings threw "trimmedKey is not defined" the moment
a tester tapped Save, so nobody could enter the app at all.
"""

import sys

from playwright.sync_api import sync_playwright

from flow_harness import error_failures, open_flow

fails = []
with sync_playwright() as playwright:
    browser, page, errors = open_flow(playwright, fresh=True)

    page.wait_for_function("() => typeof window.openSettings === 'function'", timeout=30_000)
    page.locator("#settings").wait_for(state="visible", timeout=30_000)
    fails += error_failures(errors, "first launch")

    state = page.evaluate("""() => ({
      settings: !document.getElementById("settings").classList.contains("hidden"),
      provider: document.getElementById("setProvider").value,
      keyDisabled: document.getElementById("setKey").disabled,
      saveLabel: document.getElementById("setSave").textContent,
    })""")
    if not state["settings"]:
        fails.append(f"fresh install did not open Settings: {state}")
    if state["provider"] != "shared":
        fails.append(f"a fresh install must default to the no-key shared service: {state}")
    if not state["keyDisabled"]:
        fails.append(f"the API key field must be disabled in shared mode: {state}")

    # Saving with no API key is the whole point of the shared default.
    errors.clear()
    page.locator("#setSave").click()
    page.locator("#home").wait_for(state="visible", timeout=30_000)
    fails += error_failures(errors, "saving settings")

    saved = page.evaluate("""() => ({
      home: !document.getElementById("home").classList.contains("hidden"),
      setup: localStorage.getItem("initial_setup_complete"),
      provider: localStorage.getItem("vision_provider"),
      exits: window.__exitAppCalls,
    })""")
    if not saved["home"] or saved["setup"] != "1":
        fails.append(f"Save did not complete onboarding: {saved}")
    if saved["exits"]:
        fails.append(f"saving settings closed the app {saved['exits']} time(s)")

    # The decision must survive a restart, and the restart must be clean.
    errors.clear()
    page.reload()
    page.wait_for_function("() => !!window.StandaloneAPI", timeout=30_000)
    page.locator("#home").wait_for(state="visible", timeout=30_000)
    fails += error_failures(errors, "relaunch")

    # Reopening Settings and saving again must not throw either.
    errors.clear()
    page.evaluate("openSettings()")
    page.locator("#settings").wait_for(state="visible", timeout=15_000)
    page.locator("#setName").fill("Test Citizen")
    page.locator("#setSave").click()
    page.locator("#home").wait_for(state="visible", timeout=30_000)
    fails += error_failures(errors, "second settings save")
    if page.evaluate("localStorage.getItem('sender_name')") != "Test Citizen":
        fails.append("the name entered in Settings was not saved")

    browser.close()

if fails:
    print("FAIL flow_signup")
    for failure in fails:
        print(" -", failure)
    sys.exit(1)
print("PASS flow_signup")
