#!/usr/bin/env python3
"""Reject stale generated LLM contracts and prompt copies in runtime sources."""

import json
import pathlib
import subprocess
import sys


ROOT = pathlib.Path(__file__).resolve().parent.parent
CONTRACT_PATH = ROOT / "llm" / "generated" / "contract.json"
contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
fails = []


def check(name, condition, detail=None):
    print(f"  {'ok  ' if condition else 'FAIL'} {name}")
    if not condition:
        fails.append(f"{name}{': ' + detail if detail else ''}")


generated = subprocess.run(
    ["node", "llm/generate.mjs", "--check"],
    cwd=ROOT, text=True, capture_output=True, check=False,
)
check("canonical generator reports every output current", generated.returncode == 0,
      (generated.stderr or generated.stdout).strip())

browser_paths = [
    ROOT / "static" / "llm-contract.generated.js",
    ROOT / "android-app" / "www" / "llm-contract.generated.js",
    ROOT / "android-app" / "android" / "app" / "src" / "main" / "assets"
    / "public" / "llm-contract.generated.js",
]
browser_bytes = [path.read_bytes() if path.is_file() else None for path in browser_paths]
check("all generated browser contracts exist", all(value is not None for value in browser_bytes))
check("generated browser contracts are byte-identical",
      bool(browser_bytes) and browser_bytes[0] is not None
      and all(value == browser_bytes[0] for value in browser_bytes[1:]))

prompts = contract["prompts"]
check("canonical detection role is explicit", prompts["detection"].get("role") == "user")
check("repair prompt is removed", "repair" not in prompts)
check("canonical tender instruction role is explicit",
      prompts["tender"].get("role") == "developer")
check("canonical tender data role is explicit",
      prompts["tender"].get("dataRole") == "user")

detection = prompts["detection"]
schema = detection["schema"]
required = {"image_quality", "assessment", "damage_type", "size", "description"}
removed = {"reportable", "on_drivable_surface", "has_broken_edge_or_rim",
           "has_depth_or_surface_loss", "temporal_consistency"}
check("detection schema is the exact five-field v4 contract",
      detection.get("version") == "road-damage-v5"
      and detection.get("schemaVersion") == 4
      and set(schema.get("required", [])) == required
      and set(schema.get("properties", {})) == required)
check("image quality is binary",
      schema["properties"]["image_quality"].get("enum") == ["acceptable", "rejected"])
check("assessment is binary",
      schema["properties"]["assessment"].get("enum") == ["damaged", "undamaged"])
check("removed detection fields stay removed",
      removed.isdisjoint(schema.get("properties", {})))
check("every output field is described with an example",
      all("Example:" in value.get("description", "")
          for value in schema["properties"].values()))
check("unpaved and road-edge damage remain eligible",
      all(term in detection["base"] for term in ("gravel", "dirt", "mud", "edge of the road")))
check("single-image layouts are canonical",
      set(detection.get("captureLayouts", {})) == {"manual", "drive"}
      and all("one" in note.lower() for note in detection["captureLayouts"].values()))
check("tender policy explicitly excludes footpath-only work",
      "footpath" in prompts["tender"]["instructions"].lower()
      and "not a match" in prompts["tender"]["instructions"].lower()
      and "match_index to null" in prompts["tender"]["instructions"])

# static/ is the browser source, android-app/www is Capacitor's web root, and
# assets/public is what Gradle actually packages. A successful web-only test is
# not enough if either mirror still contains an older UI/runtime.
for asset_name in ("index.html", "standalone.js"):
    shipped_paths = [
        ROOT / "static" / asset_name,
        ROOT / "android-app" / "www" / asset_name,
        ROOT / "android-app" / "android" / "app" / "src" / "main"
        / "assets" / "public" / asset_name,
    ]
    shipped_bytes = [
        path.read_bytes() if path.is_file() else None for path in shipped_paths
    ]
    check(f"all shipped {asset_name} copies exist",
          all(value is not None for value in shipped_bytes))
    check(f"all shipped {asset_name} copies are byte-identical",
          shipped_bytes[0] is not None
          and all(value == shipped_bytes[0] for value in shipped_bytes[1:]))

for relative in (
        "llm/generated/contract.mjs",
        "android-app/android/app/src/main/java/com/gauravsen/potholereporter/"
        "drivemode/LlmContractGenerated.kt"):
    path = ROOT / relative
    source = path.read_text(encoding="utf-8") if path.is_file() else ""
    check(f"generated consumer exists: {relative}", bool(source))
    check(f"generated consumer records source hash: {relative}",
          contract["sourceHash"] in source)

for relative in (
        "static/index.html",
        "android-app/www/index.html",
        "android-app/android/app/src/main/assets/public/index.html"):
    source = (ROOT / relative).read_text(encoding="utf-8")
    contract_index = source.find('src="llm-contract.generated.js"')
    runtime_index = source.find('src="standalone.js"')
    check(f"LLM contract loads before browser runtime: {relative}",
          contract_index >= 0 and runtime_index > contract_index)

# Generated files contain prompt text by design. Hand-written runtime files may
# reference generated constants, but must not retain another copy of the wording.
runtime_paths = [
    ROOT / "static" / "standalone.js",
    ROOT / "android-app" / "www" / "standalone.js",
    ROOT / "server" / "src" / "index.js",
    ROOT / "server" / "src" / "prompts.js",
    ROOT / "android-app" / "android" / "app" / "src" / "main" / "java"
    / "com" / "gauravsen" / "potholereporter" / "drivemode" / "DetectionDispatcher.kt",
]
prompt_markers = [
    contract["prompts"]["detection"]["base"].splitlines()[0],
    contract["prompts"]["tender"]["instructions"].splitlines()[0],
]
for path in runtime_paths:
    # Compatibility shims such as the former server prompts module may be removed
    # entirely once their consumers import the generated MJS directly.
    source = path.read_text(encoding="utf-8") if path.is_file() else ""
    relative = path.relative_to(ROOT)
    check(f"runtime has no copied prompt text: {relative}",
          not any(marker in source for marker in prompt_markers))

browser_runtime = (ROOT / "static" / "standalone.js").read_text(encoding="utf-8")
server_runtime = (ROOT / "server" / "src" / "index.js").read_text(encoding="utf-8")
server_prompts = ROOT / "server" / "src" / "prompts.js"
if server_prompts.is_file():
    server_runtime += server_prompts.read_text(encoding="utf-8")
native_runtime = runtime_paths[-1].read_text(encoding="utf-8")
check("browser consumes generated contract", "PotholeLlmContract" in browser_runtime)
check("server consumes generated contract", "llm/generated/contract.mjs" in server_runtime)
check("native consumes generated contract", "LlmContractGenerated" in native_runtime)
check("browser consumes canonical detection role",
      "role: DETECTION_PROMPT_CONFIG.role" in browser_runtime)
check("browser consumes canonical tender data role",
      "role: TENDER_PROMPT_CONFIG.dataRole" in browser_runtime)
check("server consumes canonical detection role",
      "DETECT_PROMPT_CONFIG.role" in server_runtime)
check("server consumes canonical tender data role",
      "role: TENDER_PROMPT_CONFIG.dataRole" in server_runtime)
check("native consumes generated detection role",
      'put("role", LlmContractGenerated.DETECT_PROMPT_ROLE)' in native_runtime)
check("browser selects the canonical capture layout",
      "DETECTION_PROMPT_CONFIG.captureLayouts.manual" in browser_runtime
      and "DETECTION_PROMPT_CONFIG.captureLayouts.drive" in browser_runtime)
check("server selects the canonical capture layout",
      "DETECT_PROMPT_CONFIG.captureLayouts.manual" in server_runtime
      and "DETECT_PROMPT_CONFIG.captureLayouts.drive" in server_runtime)
check("native selects its generated Drive capture layout",
      "LlmContractGenerated.DETECT_CAPTURE_DRIVE" in native_runtime)
for label, source in (("browser", browser_runtime), ("server", server_runtime),
                      ("native", native_runtime),
                      ("generated Kotlin", (ROOT / "android-app" / "android" / "app"
                       / "src" / "main" / "java" / "com" / "gauravsen"
                       / "potholereporter" / "drivemode"
                       / "LlmContractGenerated.kt").read_text(encoding="utf-8"))):
    check(f"{label} has no repair-model contract", "REPAIR_PROMPT" not in source
          and "road_repair_assessment" not in source)

generated_kotlin = (ROOT / "android-app" / "android" / "app" / "src" / "main"
                    / "java" / "com" / "gauravsen" / "potholereporter"
                    / "drivemode" / "LlmContractGenerated.kt").read_text(encoding="utf-8")
for constant in (
        "DETECT_PROMPT_ROLE", "TENDER_PROMPT_ROLE", "TENDER_DATA_ROLE"):
    check(f"generated Kotlin exposes {constant}", f"const val {constant}" in generated_kotlin)
check("generated Kotlin omits repair prompt constants", "REPAIR_PROMPT" not in generated_kotlin)

for label, source in (
        ("browser", browser_runtime), ("server", server_runtime),
        ("native", native_runtime)):
    check(f"{label} has no hard-coded OpenAI user message role",
          'role: "user"' not in source and 'put("role", "user")' not in source)

if fails:
    print(f"\n{len(fails)} check(s) failed")
    for failure in fails:
        print(f"  - {failure}")
    sys.exit(1)
print("\nLLM CONTRACT PARITY TEST PASS")
