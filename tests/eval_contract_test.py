#!/usr/bin/env python3
"""Offline guard that the evaluator represents the generated production contract."""
import importlib.util, json, pathlib, sys, tempfile
from PIL import Image

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("road_eval", ROOT / "eval" / "run_eval.py")
road_eval = importlib.util.module_from_spec(spec)
spec.loader.exec_module(road_eval)
contract = json.loads((ROOT / "llm" / "generated" / "contract.json").read_text())
detection = contract["prompts"]["detection"]
models = contract["config"]["models"]
runtime = contract["config"]["runtime"]
imaging = contract["config"]["imaging"]
fails = []


def check(name, condition):
    print(f"  {'ok  ' if condition else 'FAIL'} {name}")
    if not condition:
        fails.append(name)


check("generated contract loaded exactly", road_eval.CONTRACT == contract)
check("schema version", road_eval.SCHEMA_VERSION == detection["schemaVersion"])
check("schema name", road_eval.SCHEMA_NAME == detection["schemaName"])
check("schema exact", road_eval.SCHEMA == detection["schema"])
check("prompt version", road_eval.PROMPT_VERSION == detection["version"])
check("prompt arms exact", road_eval.prompts() == {
    "baseline": detection["base"], **detection["evaluationVariants"],
})
legacy_eval_fields = {"is_pothole", "looks_like_speed_breaker", "confidence"}
check("evaluation variants use the production schema vocabulary",
      all(not any(field in prompt for field in legacy_eval_fields)
          for prompt in detection["evaluationVariants"].values()))
check("responses endpoint", road_eval.API == runtime["responsesUrl"])
check("default model", road_eval.DEFAULT_MODEL == models["defaultModel"])
check("allowed models", road_eval.ALLOWED_MODELS == frozenset(models["allowedModels"]))
check("allowed details", road_eval.ALLOWED_DETAILS == frozenset(models["allowedImageDetails"]))
check("image cap", road_eval.MAX_DETECTION_IMAGES == imaging["maxDetectionImages"])
check("one-image contract", road_eval.MAX_DETECTION_IMAGES == 1)
check("schema has no model-confidence gate", "confidence" not in road_eval.SCHEMA["properties"])
check("schema is exactly the five v4 fields", set(road_eval.SCHEMA["properties"]) == {
    "image_quality", "assessment", "damage_type", "size", "description",
})
check("every field has an example in its definition",
      all("Example:" in definition.get("description", "")
          for definition in road_eval.SCHEMA["properties"].values()))
for field in road_eval.SCHEMA["required"]:
    check(f"required field {field}", field in detection["schema"]["properties"])
for damage in road_eval.SCHEMA["properties"]["damage_type"]["enum"]:
    check(f"damage enum {damage}", damage in detection["schema"]["properties"]["damage_type"]["enum"])

original_model = next(iter(models["originalDetailModels"]))
request = road_eval.build_request(["image-0"], "PROMPT", original_model, "original")
content = request["input"][0]["content"]
images = [item for item in content if item["type"] == "input_image"]
check("request image cap", len(images) == imaging["maxDetectionImages"])
try:
    road_eval.build_request(["one", "two"], "PROMPT", original_model, "original")
    rejects_multiple_images = False
except ValueError:
    rejects_multiple_images = True
check("multi-image detection request is rejected", rejects_multiple_images)
check("responses are not stored", request.get("store") is False
      and request["store"] == runtime["storeResponses"])
check("canonical input role", request["input"][0]["role"] == detection["role"])
check("canonical reasoning", request["reasoning"]["effort"]
      == models["reasoningEffortByModel"][original_model])
check("canonical structured output", request["text"]["format"] == {
    "type": "json_schema", "name": detection["schemaName"],
    "schema": detection["schema"], "strict": runtime["strictStructuredOutputs"],
})
check("canonical text verbosity", request["text"]["verbosity"] == runtime["textVerbosity"])
check("prompt once and last", len([x for x in content if x["type"] == "input_text"]) == 1
      and content[-1]["type"] == "input_text")
check("request does not invent an ordered-image suffix", content[-1]["text"] == "PROMPT")
check("detail belongs to every image", all(x.get("detail") == "original" for x in images))
non_original_model = next(model for model in models["allowedModels"]
                          if model not in models["originalDetailModels"])
check("unsupported original detail uses canonical default",
      road_eval.build_request(["one"], "P", non_original_model, "original")
      ["input"][0]["content"][0]["detail"] == models["defaultImageDetail"])
fallback = road_eval.build_request(["one"], "P", "not-a-model", "not-a-detail")
check("invalid model and detail use canonical defaults",
      fallback["model"] == models["defaultModel"]
      and fallback["input"][0]["content"][0]["detail"] == models["defaultImageDetail"]
      and fallback["reasoning"]["effort"]
      == models["reasoningEffortByModel"][models["defaultModel"]])

# `prepare_event` must use the generated transform parameters, not shadow copies.
transform_calls = []
real_encode = road_eval.encode_view
def observe_encode(path, max_dim, quality=85, band=1.0, enhance=False):
    transform_calls.append((path.name, max_dim, quality, band, enhance))
    return f"data:{path.name}", {"path": path.name}
road_eval.encode_view = observe_encode
manual_views, _, manual_note = road_eval.prepare_event(
    {"path": "manual.jpg"}, pathlib.Path("/unused"), "manual")
drive_views, _, drive_note = road_eval.prepare_event(
    {"path": "a.jpg", "frames": ["a.jpg", "b.jpg", "c.jpg"], "primary_index": 1},
    pathlib.Path("/unused"), "drive")
road_eval.encode_view = real_encode
manual = imaging["manual"]
drive = imaging["drive"]
check("manual transform comes from contract", transform_calls[0][1:] == (
    manual["maxDimension"], round(manual["jpegQuality"] * 100),
    manual["roadBand"], manual["adaptiveBrightness"]))
check("drive transform comes from contract", transform_calls[1][1:] == (
    drive["maxDimension"], round(drive["jpegQuality"] * 100),
    drive["roadBand"], drive["adaptiveBrightness"]))
check("Drive selects only its labelled primary frame",
      [call[0] for call in transform_calls] == ["manual.jpg", "b.jpg"]
      and len(drive_views) == 1)
check("manual layout comes from contract",
      manual_note == detection["captureLayouts"]["manual"] and len(manual_views) == 1)
check("drive layout comes from contract",
      drive_note == detection["captureLayouts"]["drive"])

good = {"image_quality": "acceptable", "assessment": "damaged",
        "damage_type": "failed_patch", "size": "medium",
        "description": "A prior patch has broken open."}
check("damaged road is accepted", road_eval.decision(good) == "accept")
check("acceptable undamaged road is rejected", road_eval.decision({
    **good, "assessment": "undamaged", "damage_type": None, "size": None,
}) == "reject")
check("rejected image is held for review",
      road_eval.decision({**good, "image_quality": "rejected"}) == "review")
check("damaged without subtype is contradictory",
      road_eval.decision({**good, "damage_type": None}) == "review")
check("damaged with unknown subtype is contradictory",
      road_eval.decision({**good, "damage_type": "cat"}) == "review")
check("damaged with unknown size is contradictory",
      road_eval.decision({**good, "size": "huge"}) == "review")
check("undamaged with subtype is contradictory",
      road_eval.decision({**good, "assessment": "undamaged"}) == "review")
check("undamaged with size is contradictory",
      road_eval.decision({**good, "assessment": "undamaged",
                          "damage_type": None, "size": "small"}) == "review")
check("legacy positive label", road_eval.binary_label("pothole") is True)
check("new failed-surface label", road_eval.binary_label("surface_breakup") is True)
check("unverified category excluded", road_eval.binary_label("disputed") is None)
check("manual and drive sets stay separate",
      road_eval.entry_mode({"source": "project owner, dashcam frame"}) == "drive"
      and road_eval.entry_mode({"source": "project owner, own camera"}) == "manual")

# Low-light decisions must be taken from the cropped/resized production view. This
# caught the evaluator enhancing the original first and measuring it with a different
# grayscale formula.
observed = {}
real_lift = road_eval.adaptive_lift
def observe_lift(image):
    observed["size"] = image.size
    return real_lift(image)
road_eval.adaptive_lift = observe_lift
with tempfile.TemporaryDirectory() as tmp:
    path = pathlib.Path(tmp) / "dark.jpg"
    Image.new("RGB", (2000, 1000), (30, 30, 30)).save(path, quality=100)
    _, transform = road_eval.encode_view(path, 1000, 85, 1, True)
road_eval.adaptive_lift = real_lift
check("evaluator resizes full frame before luminance", observed.get("size") == (1000, 500))
check("dark resized view is enhanced", transform["enhanced"] is True)
_, green = real_lift(Image.new("RGB", (32, 32), (0, 101, 0)))
check("evaluator uses client RGB luma weights", green["enhanced"] is False)

if fails:
    print(f"\n{len(fails)} check(s) failed")
    sys.exit(1)
print("\nEVAL CONTRACT TEST PASS")
