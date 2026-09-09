# -*- coding: utf-8 -*-
"""What the app tells the user must be true, in both languages, and must render.

Two bugs this guards against, both of which shipped once:
  - HTML entities inside strings applied with textContent, which render literally.
  - Translated strings drifting behind the English ones and describing an older build.
"""
import re, sys, pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
fails = []

for name in ("static/index.html", "android-app/www/index.html"):
    s = (ROOT / name).read_text(encoding="utf-8")

    # The two mirrors must be byte-identical; a partial patch is how the recording
    # toggle silently went missing once.
    if name.startswith("android"):
        if s != (ROOT / "static/index.html").read_text(encoding="utf-8"):
            fails.append("android-app/www/index.html has drifted from static/index.html")

    # Disclosure: both provider routes and central accepted-pothole collection must be
    # visible in both languages. A personal key must never be described as server-bound.
    shared_notes = re.findall(r'provider_shared_note: "([^"]+)"', s)
    personal_notes = re.findall(r'provider_personal_note: "([^"]+)"', s)
    settings_notes = re.findall(r'settings_note: "([^"]+)"', s)
    privacy_local = re.findall(r'privacy_local: "([^"]+)"', s)
    if not all(len(values) == 2 for values in (
            shared_notes, personal_notes, settings_notes, privacy_local)):
        fails.append(f"{name}: expected bilingual provider and central-service notes")
    else:
        for idx, language in enumerate(("English", "Kannada")):
            if "OpenAI" not in shared_notes[idx] or "OpenAI" not in personal_notes[idx]:
                fails.append(f"{name}: {language} provider notes do not name OpenAI")
            if "YOLO" not in shared_notes[idx]:
                fails.append(f"{name}: {language} shared note omits the in-house detector option")
            if "key" not in personal_notes[idx].lower() and "ಕೀ" not in personal_notes[idx]:
                fails.append(f"{name}: {language} personal note does not explain the key")
        if "project service" not in settings_notes[0] or "image hash" not in settings_notes[0]:
            fails.append(f"{name}: English note omits central service or image hash")
        if "ಯೋಜನೆಯ ಸೇವೆ" not in settings_notes[1] or "ಹ್ಯಾಶ್" not in settings_notes[1]:
            fails.append(f"{name}: Kannada note omits central service or image hash")
        if "retries" not in settings_notes[0] or "delete that report" not in settings_notes[0]:
            fails.append(f"{name}: English note omits durable retry/deletion behavior")
        if "ಮತ್ತೆ ಪ್ರಯತ್ನಿಸುತ್ತದೆ" not in settings_notes[1] or "ಅಳಿಸುವವರೆಗೆ" not in settings_notes[1]:
            fails.append(f"{name}: Kannada note omits durable retry/deletion behavior")
        if "accepted-metadata upload" not in privacy_local[0] or "reconnects" not in privacy_local[0]:
            fails.append(f"{name}: English consent omits accepted-only retry scope")
        if "ಮೆಟಾಡೇಟಾ ಅಪ್‌ಲೋಡ್" not in privacy_local[1] or "ಸಂಪರ್ಕ ಮರಳಿದಾಗ" not in privacy_local[1]:
            fails.append(f"{name}: Kannada consent omits accepted-only retry scope")

    # Scope: the Kannada refusal must not still describe the Bengaluru-only build.
    kn = re.findall(r'outside_coverage_help: "([^"]+)"', s)
    if len(kn) == 2:
        if "ಬೆಂಗಳೂರಿಗೆ" in kn[1] or "ಜಿಬಿಎ" in kn[1]:
            fails.append(f"{name}: Kannada out-of-coverage text still says Bengaluru only")
        if "ಕರ್ನಾಟಕ" not in kn[1]:
            fails.append(f"{name}: Kannada out-of-coverage text does not mention Karnataka")
    else:
        fails.append(f"{name}: expected 2 outside_coverage_help strings, found {len(kn)}")

    # Email is the sole complaint channel. Refusal/help copy must not steer people to
    # another app or phone line, and opening a composer must not be counted as delivery.
    for legacy_claim in ("Rajmargyatra", "1033 helpline", "Complaint sent", "complaints sent"):
        if legacy_claim.lower() in s.lower():
            fails.append(f"{name}: contains legacy alternate-channel/delivery claim: {legacy_claim}")

    # Every refusal reason the engine can emit needs user-facing text.
    eng = (ROOT / "static/standalone.js").read_text(encoding="utf-8")
    reasons = set(re.findall(r'return \[null, null, "([a-z_]+)"', eng))
    for r in reasons:
        key = {"outside_area": "outside_coverage", "rural_road": "rural_road",
               "no_location": "no_location", "no_address_for_body": "no_address",
               "national_highway": "nat_highway", "road_class_unknown": "road_unknown"}.get(r)
        if key and f"{key}:" not in s:
            fails.append(f"{name}: refusal reason '{r}' has no UI string ({key})")

    # Native Drive Mode cannot read WebView localStorage itself. The selected UI
    # language must cross the plugin boundary with the model settings.
    if "language: LANG" not in s:
        fails.append(f"{name}: native Drive Mode does not receive the selected language")

    # Entities are fine inside innerHTML, fatal inside textContent.
    for m in re.finditer(r'\$\("(\w+)"\)\.textContent = t\("(\w+)"\)', s):
        val = re.search(rf'\n    {m.group(2)}: "([^"]*)"', s)
        if val and re.search(r"&[a-z]+;|&#\d+;", val.group(1)):
            fails.append(f"{name}: {m.group(2)} holds an HTML entity but is set via textContent")

if (ROOT / "android-app/www/standalone.js").read_bytes() != (ROOT / "static/standalone.js").read_bytes():
    fails.append("android-app/www/standalone.js has drifted from static/standalone.js")

privacy = (ROOT / "docs/privacy.html").read_text(encoding="utf-8")
for phrase in (
        "Rejected photos do not use the project tender endpoint",
        "retried when the app starts or reconnects",
        "Deleting that local report",
        "configured detector",
        "no more than 24 hours",
        "without a photo, name, complaint text, or API key"):
    if phrase not in privacy:
        fails.append(f"docs/privacy.html omits retry/privacy disclosure: {phrase}")

if fails:
    print("FAIL"); [print("  -", f) for f in fails]; sys.exit(1)
print("UI TEXT TEST PASS")
