#!/usr/bin/env python3
"""Record the native Drive detection contract so later drift has to be deliberate.

    python3 tools/snapshot-native-contract.py            # check the snapshot
    python3 tools/snapshot-native-contract.py --write     # re-record it after a review

The app ships two detectors: native Drive runs its own on-device contract, and the
shared service runs the generated road-damage contract. They are not the same prompt and
were never meant to be, so the release gate cannot prove one by comparing it with the
other. It compares the Kotlin source with this committed snapshot instead: any change to
the native prompt, schema, model, detail, token budget or retry policy fails the gate
until someone regenerates the snapshot and reviews the diff.
"""

import argparse
import hashlib
import importlib.util
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SNAPSHOT = ROOT / "eval" / "native-detection-contract.json"

sys.path.insert(0, str(ROOT / "eval"))
spec = importlib.util.spec_from_file_location(
    "release_gate", ROOT / "eval" / "private_release_gate.py")
release_gate = importlib.util.module_from_spec(spec)
sys.modules["release_gate"] = release_gate
spec.loader.exec_module(release_gate)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def current() -> dict:
    contract = release_gate.read_native_contract()
    return {
        "model": contract["model"],
        "detail": contract["detail"],
        "prompt_version": contract["prompt_version"],
        "schema_version": contract["schema_version"],
        "max_output_tokens": contract["max_output_tokens"],
        "retry_max_attempts": contract["retry_max_attempts"],
        "prompt_sha256": digest(contract["prompt"]),
        "schema_sha256": digest(json.dumps(contract["schema"], sort_keys=True,
                                          separators=(",", ":"))),
    }


parser = argparse.ArgumentParser()
parser.add_argument("--write", action="store_true",
                    help="re-record the snapshot from the current Kotlin sources")
args = parser.parse_args()

live = current()
if args.write:
    SNAPSHOT.write_text(json.dumps(live, indent=2, sort_keys=True) + "\n")
    print(f"recorded native contract snapshot: {SNAPSHOT.relative_to(ROOT)}")
    raise SystemExit(0)

if not SNAPSHOT.is_file():
    print(f"FAIL no native contract snapshot; run with --write after reviewing")
    raise SystemExit(1)

recorded = json.loads(SNAPSHOT.read_text())
differences = {key: (recorded.get(key), live[key]) for key in live if recorded.get(key) != live[key]}
if differences:
    print("FAIL native Drive detection contract changed without re-recording:")
    for key, (was, now) in differences.items():
        print(f"  {key}: recorded {was!r}, source {now!r}")
    raise SystemExit(1)
print("native Drive detection contract matches its recorded snapshot")
