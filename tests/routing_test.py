# -*- coding: utf-8 -*-
"""Routing must name the right officer, and refuse for the right reason.

Runs against live KGIS: these are the answers the state actually gives today, which is
the only version that matters. Needs a local server on 8765 serving android-app/www.
"""
import os, sys, base64, pathlib
from dotenv import load_dotenv
ROOT = pathlib.Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
from playwright.sync_api import sync_playwright
from browser_test_utils import open_app

KEY = os.environ["OPENAI_API_KEY"]
IMG = ROOT / "eval/images/seed/IMG20260720144404.jpg"

# Personal-key capture returns its model verdict immediately. The Email tap is the point
# where jurisdiction is resolved, so these expected states are the persisted post-tap
# states rather than the optimistic local draft returned by /api/report.
# name, lat, lng, expected post-tap status, expected refusal reason
CASES = [
    ("Bengaluru HSR",          12.9115,  77.6427,  "queued",   None),
    ("Mysuru city",            12.2958,  76.6394,  "queued",   None),
    ("Hubballi-Dharwad",       15.3647,  75.1240,  "queued",   None),
    ("Chikkaballapur CMC",     13.4310,  77.7270,  "queued",   None),
    # 13.4355,77.7315 is on NH69. It used to be addressed to the town's Chief Officer.
    ("NH69 at Chikkaballapur", 13.4355,  77.7315,  "unrouted", "national_highway"),
    ("rural Magadi taluk",     13.0000,  77.2000,  "unrouted", "rural_road"),
    ("Chennai, out of state",  13.0827,  80.2707,  "unrouted", "outside_area"),
    ("no GPS",                 None,     None,     "unrouted", "no_location"),
]

POST = """async ([b64, lat, lng]) => {
  await StandaloneAPI.handle('/api/reports', {method:'DELETE'});
  const bin = atob(b64); const arr = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
  const fd = new FormData();
  fd.append('photo', new Blob([arr], {type:'image/jpeg'}), 'p.jpg');
  if (lat !== null) { fd.append('lat', String(lat)); fd.append('lng', String(lng)); }
  const initial = await StandaloneAPI.handle('/api/report', {method:'POST', body: fd});
  let sent = null;
  let blocked = null;
  try {
    sent = await StandaloneAPI.handle('/api/reports/' + initial.id + '/send', {method:'POST'});
  } catch (e) { blocked = e.message; }
  const stored = (await StandaloneAPI.handle('/api/reports'))
    .find((row) => row.id === initial.id);
  const r = sent || stored || initial;
  return { initial_status: initial.status, status: r.status,
           reason: r.unrouted_reason, body: r.unrouted_body,
           officer: r.officer_name, email: r.officer_email,
           subject: r.email_subject, tender: r.tender_number, blocked };
}"""

fails = []
with sync_playwright() as p:
    b = p.chromium.launch(args=["--disable-web-security", "--allow-running-insecure-content"])
    pg = b.new_context(viewport={"width": 390, "height": 844}).new_page()
    open_app(pg, KEY)
    src = base64.standard_b64encode(IMG.read_bytes()).decode()
    for name, lat, lng, want, reason in CASES:
        r = pg.evaluate(POST, [src, lat, lng])
        print(f"  {name:24} {r['status']:9} {str(r['reason'] or ''):20} {str(r['officer'] or '')[:34]}")
        if r["status"] != want:
            fails.append(f"{name}: expected {want}, got {r['status']}")
        if lat is not None and r["initial_status"] != "draft":
            fails.append(f"{name}: personal detection did not return its initial draft immediately")
        if reason and r["reason"] != reason:
            fails.append(f"{name}: expected reason {reason}, got {r['reason']}")
        if want == "unrouted":
            if r["email"]:  fails.append(f"{name}: named a recipient it should have refused")
            if r["subject"]: fails.append(f"{name}: retained a sendable subject after routing refused")
            if r["tender"]: fails.append(f"{name}: named a contract outside coverage")
            if not r["blocked"]: fails.append(f"{name}: Email tap was not visibly blocked")
            reason_tokens = {
                "national_highway": "national highway",
                "rural_road": "town boundary",
                "outside_area": "outside karnataka",
                "no_location": "no location",
            }
            token = reason_tokens.get(reason)
            if token and token not in (r["blocked"] or "").lower():
                fails.append(f"{name}: refusal did not explain {reason}: {r['blocked']!r}")
            # The highway layer sometimes classifies a feature while publishing a blank
            # Name; do not invent a route number. Panchayat rows do publish their body
            # name, so that useful refusal context must survive persistence.
            if reason == "rural_road" and not r["body"]:
                fails.append(f"{name}: did not persist the routed body detail")
        else:
            if not r["email"]: fails.append(f"{name}: routable point named no officer")
            if r["blocked"]: fails.append(f"{name}: Email tap was unexpectedly blocked: {r['blocked']}")
    # A Chief Officer for a council, a Commissioner for a corporation.
    b.close()

if fails:
    print("\nFAIL"); [print("  -", f) for f in fails]; sys.exit(1)
print("\nROUTING TEST PASS")
