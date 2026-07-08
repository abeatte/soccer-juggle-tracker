#!/usr/bin/env python
"""Export the YOLO models to OpenVINO for a big CPU speedup on Intel.

This box is an **Intel** CPU running Ubuntu. Intel's OpenVINO runtime accelerates
inference on Intel CPUs substantially versus stock PyTorch-CPU (commonly ~2-3x
for these nano models) — the single most effective way to get this batch
pipeline "as close to real time as possible" without new hardware.

Ultralytics auto-detects the model format from the path, so after exporting you
just point config.yaml at the exported directories:

    models:
      detector: "models/yolo11n_openvino_model"
      pose:     "models/yolo11n-pose_openvino_model"

Usage:
    python tools/export_openvino.py                 # FP32 export (safe default)
    python tools/export_openvino.py --int8          # INT8 (needs `pip install nncf`)
    python tools/export_openvino.py --imgsz 640     # match your infer size

INT8 is faster still but can slightly reduce ball-detection recall on small/blurry
balls — validate with tools/eval.py before committing to it.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_tracker.config import load_config  # noqa: E402


def _export_one(pt_path: str, imgsz: int, int8: bool) -> str:
    from ultralytics import YOLO

    model = YOLO(pt_path)
    out = model.export(format="openvino", imgsz=imgsz, int8=int8, half=False)
    # Ultralytics returns the exported path (dir for openvino).
    return str(out)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default=None)
    p.add_argument("--imgsz", type=int, default=640,
                   help="Export input size (default 640). Ultralytics letterboxes "
                        "frames to this; keep it consistent between runs.")
    p.add_argument("--int8", action="store_true",
                   help="INT8 quantization (needs nncf). Faster, validate accuracy.")
    args = p.parse_args(argv)

    cfg = load_config(args.config)
    # Use the .pt weights as the export source even if config now points at OV dirs.
    det_pt = cfg.models.detector.replace("_openvino_model", ".pt")
    pose_pt = cfg.models.pose.replace("_openvino_model", ".pt")

    print(f"Exporting detector: {det_pt}")
    det_out = _export_one(det_pt, args.imgsz, args.int8)
    print(f"  -> {det_out}")

    print(f"Exporting pose:     {pose_pt}")
    pose_out = _export_one(pose_pt, args.imgsz, args.int8)
    print(f"  -> {pose_out}")

    print("\nDone. Point config.yaml at the exported directories:")
    print("  models:")
    print(f"    detector: \"{os.path.relpath(det_out)}\"")
    print(f"    pose:     \"{os.path.relpath(pose_out)}\"")
    print("\nThen re-run tools/eval.py to confirm accuracy held up.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
