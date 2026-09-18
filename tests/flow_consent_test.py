# -*- coding: utf-8 -*-
"""Continue on the camera and location notice must continue, never close the app.

A tester reported that pressing the green Continue button on "Before camera and
location access" exited the app. The stub records every exitApp() call, so that
behaviour is a failure here instead of a one-star review.
"""

import sys

from playwright.sync_api import sync_playwright

from flow_harness import DATA_NOTICE_VERSION, error_failures, open_flow

fails = []
with sync_playwright() as playwright:
    # Onboarding done, but the data notice has never been accepted on this install.
    browser, page, errors = open_flow(playwright, storage={
        "data_notice_version": "",
        "initial_setup_complete": "1",
        "vision_provider": "shared",
    })
    page.wait_for_function("() => typeof window.ensureDataConsent === 'function'"
                           " || typeof window.StandaloneAPI === 'object'", timeout=30_000)

    # Ask for consent exactly as the capture button does, then press Continue.
    # Returning the promise would make Playwright wait for a resolution that only the
    # click below can produce, so the call deliberately returns nothing.
    page.evaluate("""() => {
      window.__consentResult = null;
      ensureDataConsent().then((value) => { window.__consentResult = value; });
    }""")
    page.locator("#dataConsent").wait_for(state="visible", timeout=15_000)
    fails += error_failures(errors, "opening the consent screen")

    errors.clear()
    page.locator("#privacyAccept").click()
    page.wait_for_function("() => window.__consentResult !== null", timeout=15_000)
    fails += error_failures(errors, "pressing Continue")

    after = page.evaluate("""() => ({
      accepted: window.__consentResult,
      home: !document.getElementById("home").classList.contains("hidden"),
      consentVisible: !document.getElementById("dataConsent").classList.contains("hidden"),
      stored: localStorage.getItem("data_notice_version"),
      exits: window.__exitAppCalls,
    })""")
    if after["accepted"] is not True:
        fails.append(f"Continue did not accept the notice: {after}")
    if after["exits"]:
        fails.append(f"Continue closed the app {after['exits']} time(s)")
    if after["consentVisible"] or not after["home"]:
        fails.append(f"Continue did not return to Home: {after}")
    if after["stored"] != DATA_NOTICE_VERSION:
        fails.append(f"the accepted notice version was not stored: {after}")

    # Accepting once must be enough: a second ask resolves without showing the screen.
    errors.clear()
    second = page.evaluate("""async () => ({
      value: await ensureDataConsent(),
      visible: !document.getElementById("dataConsent").classList.contains("hidden"),
    })""")
    if second["value"] is not True or second["visible"]:
        fails.append(f"the notice was asked again after being accepted: {second}")
    fails += error_failures(errors, "second consent check")

    # Not now must decline without closing the app either.
    page.evaluate("""() => {
      localStorage.setItem("data_notice_version", "");
      window.__declineResult = null;
      window.__declinePromise = ensureDataConsent()
        .then((value) => { window.__declineResult = value; });
    }""")
    page.locator("#dataConsent").wait_for(state="visible", timeout=15_000)
    errors.clear()
    page.locator("#privacyDecline").click()
    page.wait_for_function("() => window.__declineResult !== null", timeout=15_000)
    declined = page.evaluate("({ value: window.__declineResult, exits: window.__exitAppCalls })")
    if declined["value"] is not False:
        fails.append(f"Not now did not decline: {declined}")
    if declined["exits"]:
        fails.append(f"Not now closed the app {declined['exits']} time(s)")
    fails += error_failures(errors, "pressing Not now")

    browser.close()

if fails:
    print("FAIL flow_consent")
    for failure in fails:
        print(" -", failure)
    sys.exit(1)
print("PASS flow_consent")
