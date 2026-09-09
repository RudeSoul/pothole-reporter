"""The standalone APK must not silently depend on an undeployed shared server."""

from playwright.sync_api import sync_playwright


APP = "http://localhost:8765/"
fails = []

with sync_playwright() as p:
    browser = p.chromium.launch()
    context = browser.new_context(viewport={"width": 390, "height": 844})
    context.add_init_script("""(() => {
      localStorage.setItem('openai_key', 'sk-local-test-only');
      localStorage.setItem('data_notice_version', '2026-09-05-v3');
      localStorage.removeItem('vision_provider');
    })();""")
    central_requests = []
    context.route("https://pothole-detect.gauravsen.workers.dev/**",
                  lambda route, request: (central_requests.append(request.url), route.abort()))
    page = context.new_page()
    page.goto(APP)
    page.wait_for_load_state("networkidle")
    page.wait_for_function("() => !!window.StandaloneAPI")

    health = page.evaluate("StandaloneAPI.handle('/api/health')")
    if health.get("provider") != "personal_openai" or not health.get("ai_configured"):
        fails.append(f"fresh standalone install did not default to configured personal mode: {health}")
    if page.locator("#setProvider").input_value() != "personal":
        fails.append("Settings did not render Personal as the standalone default")
    unexpected_central = [url for url in central_requests if not url.endswith("/v1/health")]
    if unexpected_central:
        fails.append(f"fresh personal-mode startup depended on central work: {unexpected_central}")

    viewer = page.evaluate("""(() => {
      openViewer({id: 7, status: 'draft', assessment: 'damaged',
        damage_type: 'surface_breakup', size: 'medium',
        description: 'Broken roadway in the travel lane.',
        photo_url: 'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=='});
      return {text: document.getElementById('viewerMeta').innerText,
              hidden: document.getElementById('viewer').classList.contains('hidden')};
    })()""")
    for expected in ("damaged", "broken road surface", "medium", "Broken roadway"):
        if expected.lower() not in viewer["text"].lower():
            fails.append(f"viewer omits visible result {expected!r}: {viewer['text']!r}")
    if viewer["hidden"]:
        fails.append("viewer did not open")
    page.locator("#viewer").click(position={"x": 5, "y": 400})
    if not page.locator("#viewer").evaluate("element => element.classList.contains('hidden')"):
        fails.append("tapping the viewer backdrop did not close it")
    context.close()

    # Real upgrades retain the old explicit-looking default in WebView localStorage.
    # Migrate that dead value once, then preserve a genuinely explicit later choice.
    upgrade = browser.new_context(viewport={"width": 390, "height": 844})
    upgrade.add_init_script("""(() => {
      window.Capacitor = {isNativePlatform: () => true,
        Plugins: {App: {addListener: () => null, exitApp: () => null}}};
      if (!sessionStorage.getItem('upgrade_seeded')) {
        localStorage.setItem('openai_key', 'sk-local-test-only');
        localStorage.setItem('data_notice_version', '2026-09-05-v3');
        localStorage.setItem('vision_provider', 'shared');
        localStorage.removeItem('provider_default_migration');
        sessionStorage.setItem('upgrade_seeded', '1');
      }
    })();""")
    upgrade.route("https://pothole-detect.gauravsen.workers.dev/**",
                  lambda route: route.abort())
    upgraded_page = upgrade.new_page()
    upgraded_page.goto(APP)
    upgraded_page.wait_for_load_state("networkidle")
    migrated = upgraded_page.evaluate("localStorage.getItem('vision_provider')")
    if migrated != "personal":
        fails.append(f"upgrade retained the dead shared default: {migrated!r}")
    upgraded_page.evaluate("localStorage.setItem('vision_provider', 'shared')")
    upgraded_page.reload()
    upgraded_page.wait_for_load_state("networkidle")
    preserved = upgraded_page.evaluate("localStorage.getItem('vision_provider')")
    if preserved != "shared":
        fails.append("one-time migration overwrote a later explicit Shared choice")
    upgrade.close()
    browser.close()

if fails:
    print("FAIL")
    for fail in fails:
        print(" -", fail)
    raise SystemExit(1)

print("PASS: standalone defaults to personal vision and the photo viewer exposes the verdict")
