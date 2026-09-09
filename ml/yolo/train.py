#!/usr/bin/env python3
"""Fine-tune a single-class Ultralytics detector on a sealed dataset."""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
from pathlib import Path

try:
    from .common import (
        PipelineError, detection_api_contract, installed_training_engine_versions,
        load_json, sealed, sha256_file, validate_prepared_dataset, write_json,
    )
except ImportError:
    from common import (  # type: ignore
        PipelineError, detection_api_contract, installed_training_engine_versions,
        load_json, sealed, sha256_file, validate_prepared_dataset, write_json,
    )


DEFAULT_SEED = 20260905


def validate_dataset(path: Path) -> dict:
    manifest_path = path / "manifest.json"
    manifest = load_json(manifest_path)
    validate_prepared_dataset(path, manifest)
    return manifest


def command_train(args: argparse.Namespace) -> None:
    dataset = Path(args.dataset).resolve()
    manifest = validate_dataset(dataset)
    base_model = Path(args.base_model).resolve()
    if not base_model.is_file() or base_model.suffix != ".pt":
        raise PipelineError("--base-model must be a local .pt file so its bytes can be sealed")
    base_sha = sha256_file(base_model)
    if args.base_model_sha256 and base_sha != args.base_model_sha256:
        raise PipelineError("base-model SHA-256 does not match --base-model-sha256")
    try:
        import torch
        import ultralytics
        from ultralytics import YOLO
    except ImportError as error:
        raise PipelineError(
            "training dependencies are missing; install ml/yolo/requirements-train.txt") from error
    engine_environment = installed_training_engine_versions()

    project = Path(args.project).resolve()
    project.mkdir(parents=True, exist_ok=True)
    model = YOLO(str(base_model))
    model.train(
        data=str(dataset / "dataset.yaml"),
        imgsz=640,
        epochs=args.epochs,
        patience=args.patience,
        batch=args.batch,
        workers=args.workers,
        device=args.device,
        seed=args.seed,
        deterministic=True,
        single_cls=True,
        pretrained=True,
        cache=False,
        amp=not args.no_amp,
        plots=True,
        val=True,
        project=str(project),
        name=args.name,
        exist_ok=False,
    )
    trainer = getattr(model, "trainer", None)
    best = Path(str(getattr(trainer, "best", "")))
    if not best.is_file():
        expected = project / args.name / "weights" / "best.pt"
        if expected.is_file():
            best = expected
        else:
            raise PipelineError("Ultralytics finished without a best.pt artifact")
    run_dir = best.parent.parent
    receipt = sealed({
        "schema_version": "pothole-yolo-training-receipt-v1",
        "task": "pothole_detection",
        "detection_contract": detection_api_contract(),
        "dataset_manifest_sha256": manifest["manifest_sha256"],
        "base_model": {
            "filename": base_model.name,
            "sha256": base_sha,
        },
        "result": {
            "best_weights": best.relative_to(run_dir).as_posix(),
            "best_weights_sha256": sha256_file(best),
        },
        "training": {
            "image_size": 640,
            "epochs": args.epochs,
            "patience": args.patience,
            "batch": args.batch,
            "workers": args.workers,
            "device": str(args.device),
            "seed": args.seed,
            "deterministic": True,
            "single_class": True,
            "amp": not args.no_amp,
        },
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "ultralytics": ultralytics.__version__,
            "torch": torch.__version__,
            "cuda_available": bool(torch.cuda.is_available()),
            "engine_versions": engine_environment,
        },
    }, field="receipt_sha256")
    write_json(run_dir / "training-receipt.json", receipt)
    print(json.dumps({
        "best_weights": str(best),
        "best_weights_sha256": receipt["result"]["best_weights_sha256"],
        "receipt": str(run_dir / "training-receipt.json"),
        "receipt_sha256": receipt["receipt_sha256"],
    }, indent=2, sort_keys=True))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--dataset", required=True, help="sealed prepared dataset directory")
    result.add_argument("--base-model", required=True,
                        help="local Ultralytics detection .pt checkpoint, e.g. yolo11n.pt")
    result.add_argument("--base-model-sha256")
    result.add_argument("--project", default="ml/yolo/runs")
    result.add_argument("--name", default="pothole-yolo-v1")
    result.add_argument("--epochs", type=int, default=100)
    result.add_argument("--patience", type=int, default=20)
    result.add_argument("--batch", type=int, default=16)
    result.add_argument("--workers", type=int, default=8)
    result.add_argument("--device", default="0")
    result.add_argument("--seed", type=int, default=DEFAULT_SEED)
    result.add_argument("--no-amp", action="store_true")
    return result


def main() -> None:
    try:
        args = parser().parse_args()
        if args.epochs < 1 or args.patience < 0 or args.batch < 1 or args.workers < 0:
            raise PipelineError("training counts are outside their valid range")
        command_train(args)
    except PipelineError as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
