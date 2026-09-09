# -*- coding: utf-8 -*-
"""Release gate for deterministic tender selection, precision, and recall.

The evaluator shuffles candidate order, executes the production prompt/schema, and
fails if either exact-selection precision or recall drops below the configured gate.
Consequently an all-null model cannot pass merely by being deterministic.
"""

import importlib.util
import json
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
RUNS = max(1, int(os.environ.get("TENDER_RUNS", "3")))

# Keep a deterministic negative control in the release gate. A model returning null
# for every case may look stable and have no false positives, but it has zero recall
# and must never pass merely because the live arm happened to be deterministic.
spec = importlib.util.spec_from_file_location(
    "tender_eval", ROOT / "eval" / "run_tender_eval.py")
tender_eval = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tender_eval)
cases = json.loads((ROOT / "eval" / "tender_cases.json").read_text())["cases"]
all_null = tender_eval.metrics([
    (case.get("expected_tender_number"), None) for case in cases
])
if all_null["recall"] != 0 or all_null["precision"] is not None:
    raise SystemExit("all-null tender negative control did not produce zero recall")
print("ALL-NULL NEGATIVE CONTROL PASS (recall 0; release gate would fail)")

completed = subprocess.run(
    [
        sys.executable,
        str(ROOT / "eval" / "run_tender_eval.py"),
        "--trials", str(RUNS),
        "--out", str(ROOT / "eval" / "results" / "tender"),
    ],
    cwd=ROOT,
    text=True,
    capture_output=True,
    check=False,
)

if completed.stdout:
    print(completed.stdout.rstrip())
if completed.returncode:
    if completed.stderr:
        print(completed.stderr.rstrip(), file=sys.stderr)
    raise SystemExit(completed.returncode)

print("TENDER DETERMINISM + PRECISION/RECALL TEST PASS")
