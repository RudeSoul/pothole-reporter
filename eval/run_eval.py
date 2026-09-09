#!/usr/bin/env python3
"""Replay the shipped road-damage contract against labelled manual or drive images.

Production transforms and request semantics are mirrored here. Repetitions stay nested
under their source event; they are never presented as additional ground truth.
"""
import argparse, base64, hashlib, io, json, math, os, subprocess, sys
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTRACT_PATH = ROOT / "llm" / "generated" / "contract.json"
try:
    CONTRACT = json.loads(CONTRACT_PATH.read_bytes())
except (OSError, json.JSONDecodeError) as error:
    raise RuntimeError(
        "The generated LLM contract is missing or unreadable. "
        "Run `node llm/generate.mjs` from the repository root."
    ) from error

DETECTION = CONTRACT["prompts"]["detection"]
MODEL_CONFIG = CONTRACT["config"]["models"]
RUNTIME_CONFIG = CONTRACT["config"]["runtime"]
IMAGING_CONFIG = CONTRACT["config"]["imaging"]
LUMINANCE_CONFIG = IMAGING_CONFIG["adaptiveLuminance"]

API = RUNTIME_CONFIG["responsesUrl"]
DEFAULT_MODEL = MODEL_CONFIG["defaultModel"]
ALLOWED_MODELS = frozenset(MODEL_CONFIG["allowedModels"])
ALLOWED_DETAILS = frozenset(MODEL_CONFIG["allowedImageDetails"])
ORIGINAL_DETAIL_MODELS = frozenset(MODEL_CONFIG["originalDetailModels"])
DEFAULT_DETAIL = MODEL_CONFIG["defaultImageDetail"]
MAX_DETECTION_IMAGES = IMAGING_CONFIG["maxDetectionImages"]
PROMPT_VERSION = DETECTION["version"]
SCHEMA_VERSION = DETECTION["schemaVersion"]
SCHEMA_NAME = DETECTION["schemaName"]
SCHEMA = DETECTION["schema"]

if RUNTIME_CONFIG["storeResponses"] is not False:
    raise RuntimeError("The evaluator refuses to run while the canonical contract stores responses.")
if MAX_DETECTION_IMAGES != 1:
    raise RuntimeError("road-damage-v4 evaluation requires exactly one detection image.")


def sha(value):
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def load_key():
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if key:
        return key
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("OPENAI_API_KEY="):
                return line.split("=", 1)[1].strip()
    sys.exit("OPENAI_API_KEY not set (environment or .env)")


def prompts():
    """Return only prompt arms registered by the canonical LLM contract."""
    registered = DETECTION.get("evaluationVariants", {})
    if "baseline" in registered:
        raise RuntimeError("The reserved baseline arm cannot be replaced by an evaluation variant.")
    return {"baseline": DETECTION["base"], **registered}


def normalise_config(model, detail):
    model = model if model in ALLOWED_MODELS else DEFAULT_MODEL
    detail = detail if detail in ALLOWED_DETAILS else DEFAULT_DETAIL
    if detail == MODEL_CONFIG["originalImageDetail"] and model not in ORIGINAL_DETAIL_MODELS:
        detail = DEFAULT_DETAIL
    return model, detail


def adaptive_lift(image):
    """Mirror the client's sampled RGB luma test on the already-resized view."""
    from PIL import ImageEnhance
    pixels = image.load()
    step = max(1, math.floor(math.sqrt(
        (image.width * image.height) / LUMINANCE_CONFIG["targetSamples"])))
    total = count = clipped_dark = clipped_bright = 0
    for y in range(0, image.height, step):
        for x in range(0, image.width, step):
            red, green, blue = pixels[x, y]
            luminance = .2126 * red + .7152 * green + .0722 * blue
            total += luminance
            count += 1
            clipped_dark += luminance < LUMINANCE_CONFIG["darkPixelThreshold"]
            clipped_bright += luminance > LUMINANCE_CONFIG["brightPixelThreshold"]
    mean = total / max(1, count)
    dark = clipped_dark / max(1, count)
    bright = clipped_bright / max(1, count)
    if (mean >= LUMINANCE_CONFIG["meanThreshold"]
            or bright >= LUMINANCE_CONFIG["brightFractionThreshold"]):
        return image, {"luminance": mean, "dark": dark, "bright": bright,
                       "enhanced": False}
    lift = min(LUMINANCE_CONFIG["maximumLift"],
               max(LUMINANCE_CONFIG["minimumLift"],
                   LUMINANCE_CONFIG["targetMean"]
                   / max(LUMINANCE_CONFIG["meanFloor"], mean)))
    image = ImageEnhance.Brightness(image).enhance(lift)
    image = ImageEnhance.Contrast(image).enhance(LUMINANCE_CONFIG["contrast"])
    return image, {"luminance": mean, "dark": dark, "bright": bright,
                   "enhanced": True, "brightness": lift}


def encode_view(path, max_dim, quality, band, enhance):
    from PIL import Image
    image = Image.open(path).convert("RGB")
    source = {"width": image.width, "height": image.height}
    if band < 1:
        height = max(1, round(image.height * band))
        image = image.crop((0, image.height - height, image.width, image.height))
    scale = min(1.0, max_dim / max(image.size))
    if scale < 1:
        image = image.resize((round(image.width * scale), round(image.height * scale)), Image.Resampling.LANCZOS)
    light = {"enhanced": False}
    if enhance:
        image, light = adaptive_lift(image)
    buf = io.BytesIO()
    image.save(buf, "JPEG", quality=quality)
    raw = buf.getvalue()
    return "data:image/jpeg;base64," + base64.b64encode(raw).decode(), {
        "source": source, "output": {"width": image.width, "height": image.height},
        "max_dim": max_dim, "jpeg_quality": quality, "road_band": band,
        **light, "bytes_sha256": sha(raw),
    }


def entry_image(entry):
    """Return the single frame selected for this labelled event."""
    paths = entry.get("frames") or [entry["path"]]
    primary = int(entry.get("primary_index", 0))
    primary = primary if 0 <= primary < len(paths) else 0
    return paths[primary]


def entry_mode(entry):
    if entry.get("mode") in {"manual", "drive"}:
        return entry["mode"]
    return "drive" if "dashcam" in str(entry.get("source", "")).lower() else "manual"


def prepare_event(entry, root, mode):
    config = IMAGING_CONFIG[mode]
    selected = entry_image(entry)
    view, meta = encode_view(
        root / selected, config["maxDimension"],
        round(config["jpegQuality"] * 100), config["roadBand"],
        config["adaptiveBrightness"])
    transform = {"selected_image": selected, **meta}
    return [view], [transform], DETECTION["captureLayouts"][mode]


def build_request(views, prompt, model, detail):
    model, detail = normalise_config(model, detail)
    content = [
        {"type": "input_image", "image_url": url, "detail": detail}
        for url in views
    ]
    if len(content) != 1:
        raise ValueError("road-damage-v5 requests must contain exactly one image")
    content.append({"type": "input_text", "text": prompt})
    return {
        "model": model,
        "store": RUNTIME_CONFIG["storeResponses"],
        "reasoning": {"effort": MODEL_CONFIG["reasoningEffortByModel"].get(
            model, MODEL_CONFIG["defaultReasoningEffort"])},
        "input": [{"role": DETECTION["role"], "content": content}],
        "text": {"format": {"type": "json_schema", "name": SCHEMA_NAME,
                              "schema": SCHEMA,
                              "strict": RUNTIME_CONFIG["strictStructuredOutputs"]},
                 "verbosity": RUNTIME_CONFIG["textVerbosity"]},
    }


def decision(result):
    if not result or result.get("image_quality") == "rejected":
        return "review"
    if result.get("image_quality") != "acceptable":
        return "review"
    allowed_damage_types = {
        value for value in SCHEMA["properties"]["damage_type"]["enum"]
        if isinstance(value, str)
    }
    allowed_sizes = {
        value for value in SCHEMA["properties"]["size"]["enum"]
        if isinstance(value, str)
    }
    size = result.get("size")
    if (result.get("assessment") == "damaged"
            and result.get("damage_type") in allowed_damage_types
            and (size is None or size in allowed_sizes)):
        return "accept"
    if (result.get("assessment") == "undamaged"
            and result.get("damage_type") is None
            and result.get("size") is None):
        return "reject"
    # Any other field combination contradicts the canonical schema semantics.
    # It is not safe to turn a malformed result into a complaint decision.
    return "review"


def call(key, body, cache_dir, cache_slot):
    # Each stochastic repetition has its own stable slot. Caching identical body bytes
    # into one file would make five "trials" five copies of the first response.
    body_hash = sha(json.dumps(body, sort_keys=True, separators=(",", ":")))
    cache_key = f"{body_hash}-{sha(cache_slot)[:12]}"
    cached = cache_dir / f"{cache_key}.json"
    if cached.exists():
        return json.loads(cached.read_text()), True, cache_key
    request = urllib.request.Request(API, data=json.dumps(body).encode(), headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    result = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(
                    request, timeout=RUNTIME_CONFIG["timeoutsMs"]["personalOpenAI"] / 1000
            ) as response:
                payload = json.loads(response.read())
            message = next(o for o in payload.get("output", []) if o.get("type") == "message")
            text = next(c for c in message["content"] if c.get("type") == "output_text")["text"]
            result = json.loads(text)
            break
        except Exception as error:
            if attempt == 2:
                result = {"error": str(error)[:200]}
    cached.write_text(json.dumps(result, indent=1))
    return result, False, cache_key


def binary_label(label):
    if label in {"pothole", "pothole_cavity", "failed_patch", "surface_breakup",
                 "rut_or_depression", "other_road_damage", "damaged"}:
        return True
    if label in {"not_pothole", "undamaged"}:
        return False
    return None


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def grouped_metrics(source_rows, suppress_precision_without_negatives=False):
    """Count each labelled event once, regardless of stochastic trial count."""
    grouped = defaultdict(list)
    for row in source_rows:
        if binary_label(row["label"]) is not None:
            grouped[row["event"]].append(row)

    counts = Counter(tp=0, fp=0, tn=0, fn=0)
    event_results = []
    for event, event_rows in sorted(grouped.items()):
        truth = binary_label(event_rows[0]["label"])
        decisions = Counter(row["decision"] for row in event_rows)
        # A complaint is the positive action. Require a strict majority of the
        # event's repetitions so ties and review-heavy events fail closed.
        predicted_positive = decisions["accept"] > len(event_rows) / 2
        if truth and predicted_positive:
            counts["tp"] += 1
        elif truth:
            counts["fn"] += 1
        elif predicted_positive:
            counts["fp"] += 1
        else:
            counts["tn"] += 1
        event_results.append({
            "event": event,
            "truth": "positive" if truth else "negative",
            "predicted": "positive" if predicted_positive else "negative",
            "decisions": dict(decisions),
            "accept_rate": decisions["accept"] / len(event_rows),
        })

    positive_events = counts["tp"] + counts["fn"]
    negative_events = counts["tn"] + counts["fp"]
    predicted_positive_events = counts["tp"] + counts["fp"]
    precision = ratio(counts["tp"], predicted_positive_events)
    recall = ratio(counts["tp"], positive_events)
    specificity = ratio(counts["tn"], negative_events)
    false_accept_rate = ratio(counts["fp"], negative_events)
    precision_note = None
    if suppress_precision_without_negatives and negative_events == 0:
        precision = None
        precision_note = "not estimable: no owner-verified negative events"
    f1 = (2 * precision * recall / (precision + recall)
          if precision is not None and recall is not None and precision + recall else None)
    return {
        "events": len(event_results),
        "positive_events": positive_events,
        "negative_events": negative_events,
        "tp": counts["tp"], "fp": counts["fp"],
        "tn": counts["tn"], "fn": counts["fn"],
        "precision": precision, "precision_note": precision_note,
        "recall": recall, "specificity": specificity,
        "false_accept_rate": false_accept_rate, "f1": f1,
        "event_results": event_results,
    }


def git_commit():
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    except Exception:
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", type=int, default=5)
    parser.add_argument("--arms", default="baseline", help="comma-separated prompt arms")
    parser.add_argument("--models", default=DEFAULT_MODEL, help="comma-separated model IDs")
    parser.add_argument("--details", default=DEFAULT_DETAIL,
                        help="comma-separated image-detail values from the LLM contract")
    parser.add_argument("--mode", choices=["manual", "drive"], default="drive")
    parser.add_argument("--images-root", default=str(ROOT / "eval" / "images"))
    parser.add_argument("--labels", default=str(ROOT / "eval" / "labels.json"))
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--limit", type=int, default=0, help="first N matching events; smoke tests only")
    parser.add_argument("--out", default=str(ROOT / "eval" / "results"))
    parser.add_argument("--dry-run", action="store_true", help="validate and print requests without API calls")
    args = parser.parse_args()

    label_bytes = Path(args.labels).read_bytes()
    all_entries = json.loads(label_bytes)["images"]
    entries = [entry for entry in all_entries if entry_mode(entry) == args.mode]
    if args.limit > 0:
        entries = entries[:args.limit]
    if not entries:
        sys.exit(f"no {args.mode} entries in the selected label set")
    root = Path(args.images_root)
    missing = [entry_image(entry) for entry in entries if not (root / entry_image(entry)).exists()]
    if missing:
        sys.exit(f"{len(missing)} labelled images not found under {root}, first: {missing[0]}\n"
                 "Images are not committed; see eval/README.md.")

    variants = prompts()
    chosen = [x for x in args.arms.split(",") if x]
    unknown = [x for x in chosen if x not in variants]
    if unknown:
        sys.exit(f"unknown prompt arm(s): {unknown}; available: {sorted(variants)}")
    configs = []
    for arm in chosen:
        for model in filter(None, args.models.split(",")):
            for detail in filter(None, args.details.split(",")):
                model, detail = normalise_config(model, detail)
                item = (f"{arm}|{model}|{detail}|{args.mode}", variants[arm], model, detail)
                if item not in configs:
                    configs.append(item)
    if not configs:
        sys.exit("no valid evaluation configuration")
    configs.append(("baseline_replicate|" + "|".join(configs[0][0].split("|")[1:]),
                    variants["baseline"], configs[0][2], configs[0][3]))

    prepared = {}
    for entry in entries:
        views, transforms, note = prepare_event(entry, root, args.mode)
        prepared[entry["path"]] = (views, transforms, note)

    jobs = []
    for name, prompt, model, detail in configs:
        for entry in entries:
            views, transforms, note = prepared[entry["path"]]
            body = build_request(views, prompt + note, model, detail)
            for trial in range(args.trials):
                jobs.append((name, entry, trial, body, transforms))
    print(f"{len(jobs)} calls: {len(configs)} configurations x {len(entries)} events x {args.trials} trials")
    if args.dry_run:
        sample = jobs[0]
        images = [x for x in sample[3]["input"][0]["content"] if x["type"] == "input_image"]
        print(json.dumps({"arm": sample[0], "images": len(images), "model": sample[3]["model"],
                          "reasoning": sample[3]["reasoning"]["effort"],
                          "detail": images[0]["detail"], "store": sample[3]["store"],
                          "contract_source_sha256": CONTRACT["sourceHash"],
                          "schema_version": SCHEMA_VERSION,
                          "transform": sample[4]}, indent=1))
        return

    key = load_key()
    outdir = Path(args.out); outdir.mkdir(parents=True, exist_ok=True)
    cache_dir = outdir / "cache"; cache_dir.mkdir(exist_ok=True)
    rows = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        results = pool.map(lambda job: call(key, job[3], cache_dir,
                                            f"{job[0]}|{job[1].get('event_id') or job[1]['path']}|{job[2]}"), jobs)
        for index, (job, returned) in enumerate(zip(jobs, results), 1):
            name, entry, trial, body, transforms = job
            result, cached, cache_key = returned
            rows.append({"arm": name, "event": entry.get("event_id") or entry["path"],
                         "image": entry["path"], "label": entry["label"],
                         "labelled_by": entry.get("labelled_by"), "trial": trial,
                         "decision": decision(result), "cached": cached,
                         "request_hash": cache_key, "transforms": transforms, **result})
            if index % 25 == 0:
                print(f"  {index}/{len(jobs)}")

    (outdir / "raw.jsonl").write_text("\n".join(json.dumps(row) for row in rows))
    summary = {}
    print("\n=== event-clustered binary results ===")
    for name, _, _, _ in configs:
        arm_rows = [row for row in rows if row["arm"] == name and "error" not in row]
        verified_rows = [row for row in arm_rows
                         if str(row.get("labelled_by", "")).strip().lower() == "owner"]
        verified = grouped_metrics(verified_rows, suppress_precision_without_negatives=True)
        provisional = grouped_metrics(arm_rows)
        summary[name] = {
            "owner_verified": verified,
            "provisional_including_unverified": provisional,
        }
        def percent(value):
            return f"{value:.1%}" if value is not None else "n/a"

        print(f"  {name:48} OWNER VERIFIED "
              f"TP/FP/TN/FN {verified['tp']}/{verified['fp']}/{verified['tn']}/{verified['fn']} · "
              f"precision {percent(verified['precision'])} · recall {percent(verified['recall'])} · "
              f"specificity {percent(verified['specificity'])} · FAR {percent(verified['false_accept_rate'])} · "
              f"F1 {percent(verified['f1'])}")
        if verified["precision_note"]:
            print(f"    {verified['precision_note']}")
        print(f"  {name:48} PROVISIONAL    "
              f"TP/FP/TN/FN {provisional['tp']}/{provisional['fp']}/{provisional['tn']}/{provisional['fn']} · "
              f"precision {percent(provisional['precision'])} · recall {percent(provisional['recall'])} · "
              f"specificity {percent(provisional['specificity'])} · FAR {percent(provisional['false_accept_rate'])} · "
              f"F1 {percent(provisional['f1'])}")

    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(), "git_commit": git_commit(),
        "llm_contract_version": CONTRACT["contractVersion"],
        "llm_contract_source_sha256": CONTRACT["sourceHash"],
        "mode": args.mode, "trials_per_event": args.trials, "prompt_version": PROMPT_VERSION,
        "schema_name": SCHEMA_NAME, "schema_version": SCHEMA_VERSION,
        "schema_sha256": sha(json.dumps(SCHEMA, sort_keys=True)),
        "store_responses": RUNTIME_CONFIG["storeResponses"],
        "text_verbosity": RUNTIME_CONFIG["textVerbosity"],
        "max_detection_images": MAX_DETECTION_IMAGES,
        "imaging": IMAGING_CONFIG,
        "labels_sha256": sha(label_bytes), "configs": [config[0] for config in configs],
        "event_aggregation": "strict majority accept across repetitions; ties fail closed",
        "warning": "The seed set is not a release gate until it contains owner-verified positives and negatives.",
    }
    (outdir / "manifest.json").write_text(json.dumps(manifest, indent=1))
    (outdir / "summary.json").write_text(json.dumps(summary, indent=1))
    print(f"\nwrote raw.jsonl, manifest.json, summary.json and response cache under {outdir}")


if __name__ == "__main__":
    main()
