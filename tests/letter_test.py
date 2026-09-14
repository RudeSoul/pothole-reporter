# -*- coding: utf-8 -*-
"""The generated email is concise, complete, and does not overclaim.

The detector has one accepted road-defect class: pothole. Candidate public records
must never appear in outbound copy until every responsibility gate is verified.
"""
import sys

from playwright.sync_api import sync_playwright


FOOTER = (
    "Pothole Reporter is an independent app. Please verify any suggested authority, "
    "ward, road ownership, and tender details."
)

JS = r"""
(() => {
  const P = StandaloneAPI.__pure;
  const a = { is_pothole: true, size: "medium", confidence: 0.8, description: "d" };
  const tender = { tender_number: "DMA/1", contractor: "ACME", title: "Road work",
                   published: "01-02-2026", warranty: "within the defect liability period",
                   warranty_code: "dlp" };
  const out = {};
  const [, councilBody] = P.draftEmail(a, 12.9, 77.6, "Main Road, Channagiri, 577213",
                                       "Chief Officer, Channagiri", tender);
  const [, corpBody] = P.draftEmail(a, 12.9, 77.6, "17th Main, HSR Layout, Bengaluru",
                                    "Commissioner, Bengaluru South City Corporation", tender);
  out.council = councilBody;
  out.corporation = corpBody;
  // and the no-contract case, which most complaints are
  const [, noTender] = P.draftEmail(a, 12.9, 77.6, "Main Road, Channagiri, 577213",
                                    "Chief Officer, Channagiri", null);
  out.noTender = noTender;
  out.english = { subject: P.draftEmail(a, 12.9, 77.6,
    "Main Road, Channagiri, 577213", "Chief Officer, Channagiri", tender)[0],
    withTender: councilBody, noTender };

  // draftEmail reads the language at call time, so the same production helper can be
  // checked in both shipped languages without maintaining a second test implementation.
  localStorage.setItem("app_lang", "kn");
  const [knSubject, knTender] = P.draftEmail(a, 12.9, 77.6,
    "ಮುಖ್ಯ ರಸ್ತೆ, ಚನ್ನಗಿರಿ, 577213", "ಮುಖ್ಯ ಅಧಿಕಾರಿ, ಚನ್ನಗಿರಿ", tender);
  const [, knNoTender] = P.draftEmail(a, 12.9, 77.6,
    "ಮುಖ್ಯ ರಸ್ತೆ, ಚನ್ನಗಿರಿ, 577213", "ಮುಖ್ಯ ಅಧಿಕಾರಿ, ಚನ್ನಗಿರಿ", null);
  out.kannada = { subject: knSubject, withTender: knTender, noTender: knNoTender };
  localStorage.removeItem("app_lang");

  out.types = {};
  for (const type of ["pothole_cavity", "failed_patch", "surface_breakup", "rut_or_depression"]) {
    const [subject, body] = P.draftEmail({ damage_type:type, size:"medium", description:"d" },
      12.9, 77.6, "Main Road, Channagiri, 577213", "Chief Officer, Channagiri", null);
    out.types[type] = {subject, body};
  }
  return out;
})()
"""


def require(failures, condition, message):
    if not condition:
        failures.append(message)

for phrase in ("city corporation", "the city"):
    if phrase in council:
        fails.append(f'a letter to a Chief Officer says "{phrase}", but that body is not a corporation')
if "Chief Officer, Channagiri" not in council:
    fails.append("the greeting does not address the routed officer")
if "probable" not in council.lower():
    fails.append("the contract claim lost its hedge")
if "ACME" not in council:
    fails.append("the contractor is not named when one is recorded")
if "publication date does not establish" not in council.lower():
    fails.append("English complaint does not explicitly disclaim current contractor liability")
for claim in ("within the defect liability period", "within the maintenance period",
              "at no additional cost"):
    if claim in council.lower():
        fails.append(f"English complaint makes an unsupported liability claim: {claim!r}")

# A matched tender may be useful context, but the app must not hint that one was found
# when resolution returned null. Check the whole conditional paragraph rather than just
# one contractor name: that catches a stale number, title, date, or boilerplate claim.
for language, values in (("English", r["english"]), ("Kannada", r["kannada"])):
    with_tender = values["withTender"]
    no_tender = values["noTender"]
    for token in ("DMA/1", "Road work", "ACME", "01-02-2026"):
        if token not in with_tender:
            fails.append(f"{language} matched-tender complaint omits {token!r}")
        if token in no_tender:
            fails.append(f"{language} no-tender complaint leaks {token!r}")
    if language == "English":
        if "tender DMA/1" not in with_tender or "probably" not in with_tender.lower():
            fails.append("English tender wording lost the exact number or its probability hedge")
        if "tender" in no_tender.lower():
            fails.append("English no-tender complaint says a tender was found")
        for phrase in ("Public procurement records", "probable record match", "tender documents"):
            if phrase in no_tender:
                fails.append(f"English no-tender complaint contains conditional wording: {phrase!r}")
    else:
        if "ಟೆಂಡರ್ DMA/1" not in with_tender or "ಸಂಭಾವ್ಯ" not in with_tender:
            fails.append("Kannada tender wording lost the exact number or its probability hedge")
        if "ಟೆಂಡರ್" in no_tender:
            fails.append("Kannada no-tender complaint says a tender was found")
        if "ಪ್ರಕಟಣೆ ದಿನಾಂಕವು" not in with_tender or "ಸ್ಥಾಪಿಸುವುದಿಲ್ಲ" not in with_tender:
            fails.append("Kannada complaint does not explicitly disclaim current contractor liability")
        if "ಹೆಚ್ಚುವರಿ ವೆಚ್ಚವಿಲ್ಲದೆ" in with_tender:
            fails.append("Kannada complaint makes an unsupported no-additional-cost claim")

for language, values in (("English", r["english"]), ("Kannada", r["kannada"])):
    combined = values["subject"] + "\n" + values["withTender"]
    for token in ("12.900000", "77.600000", "https://maps.google.com/?q=12.900000,77.600000"):
        if token not in combined:
            fails.append(f"{language} complaint omits location evidence {token!r}")

def main():
    failures = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=["--disable-web-security"])
        page = browser.new_context(viewport={"width": 390, "height": 844}).new_page()
        page.goto("http://localhost:8765/")
        page.wait_for_load_state("networkidle")
        page.wait_for_function(
            "typeof StandaloneAPI !== 'undefined' && StandaloneAPI.__pure",
            timeout=30000,
        )
        result = page.evaluate(JS)
        browser.close()

    matched = result["matched"]
    body = matched["email_body"]
    no_candidate = result["noCandidate"]["email_body"]

    print("  generated structured complaint:")
    for line in body.splitlines():
        if line.strip():
            print(f"    | {line[:112]}")

    require(failures, matched["email_subject"] == "Pothole complaint — 17th Main Road",
            "subject is not the concise road-specific subject")
    for heading in ("LOCATION", "CLASSIFICATION", "ROUTING", "CONTRACT VERIFICATION"):
        require(failures, body.count(heading) == 1,
                f"email must contain exactly one {heading} section")
    for expected in (
        "Address / landmark: 17th Main Road, HSR Layout, Bengaluru",
        "Coordinates: 12.912345, 77.612345",
        "Map: https://maps.google.com/?q=12.912345,77.612345",
        "Defect decision: Pothole — YES",
        "Surface: Bituminous / asphalt",
        "App visual size class: medium",
        "Physical dimensions (length / width / depth): Unknown / Unknown / Unknown",
        "Measurement provenance: Visual estimate without a scale reference",
        "Measurement confidence: Low",
        "Geographic corporation/body: Bengaluru South City Corporation",
        "Complaint intake authority: Bengaluru South City Corporation",
        "Road owner/maintainer: Unknown — authority to inspect and transfer if required",
        "Status: No verified exact-road public contract found; tender and contractor omitted.",
    ):
        require(failures, expected in body, f'missing or altered email field: "{expected}"')
    for leaked in (
        "BBMP/2025-26/RD/WORK-42", "Resurfacing of 17th Main Road in HSR Layout",
        "ACME Roads Pvt Ltd", "Karnataka Public Procurement Portal (KPPP) snapshot",
    ):
        require(failures, leaked not in body,
                f'unverified contract identity leaked into the email: "{leaked}"')

    require(failures, body.count(FOOTER) == 1,
            "email must contain exactly one independent-app disclaimer")
    require(failures, body.rstrip().endswith(FOOTER),
            "independent-app disclaimer must be the final email paragraph")
    for forbidden in (
        "within the defect liability period",
        "within maintenance period",
        "official size",
        "official category",
        "does not submit a grievance",
        "no official grievance submission is confirmed",
    ):
        require(failures, forbidden not in body.lower(),
                f'email retains an unsupported or noisy claim: "{forbidden}"')

    require(failures,
            "Status: No verified exact-road public contract found; tender and contractor omitted."
            in no_candidate,
            "no-candidate email does not state the fail-closed attribution result")
    require(failures, "BBMP/2025-26/RD/WORK-42" not in no_candidate,
            "no-candidate email leaked a tender from another render")
    require(failures, result["rejectedScope"] is None,
            "drain-and-footpath-only WORK_INDENT2505 was accepted as road work")

    print()
    if failures:
        print("FAIL")
        for failure in failures:
            print("  -", failure)
        sys.exit(1)
    print("LETTER TEST PASS")


if __name__ == "__main__":
    main()
