#!/usr/bin/env python
"""Accuracy eval harness for ongoing tuning.

Processes a set of labelled clips and compares the pipeline's best detected
streak per clip against your hand-counted ground truth, reporting per-clip error
and an overall Mean Absolute Error (MAE). Use it to tell whether a config change
actually improved counting instead of eyeballing debug videos every time.

It runs a *sandboxed* pipeline: Home Assistant publishing is disabled and a
throwaway SQLite DB is used, so evaluating never touches your real scores.

Ground-truth CSV (header required):

    clip,expected
    clips/kid1_session1.mp4,27
    clips/kid1_session2.mp4,14
    clips/kid2_session1.mp4,8

Usage:
    python tools/eval.py ground_truth.csv                 # uses config.yaml
    python tools/eval.py ground_truth.csv --config alt.yaml
    python tools/eval.py ground_truth.csv --debug-dir dbg # write overlay per clip
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_tracker.config import load_config  # noqa: E402


def _sandbox(cfg):
    """Disable HA publishing and point at a throwaway DB so eval is side-effect free."""
    cfg.raw.setdefault("home_assistant", {})["enabled"] = False
    tmp_db = os.path.join(tempfile.mkdtemp(prefix="jt_eval_"), "eval.db")
    cfg.raw.setdefault("database", {})["path"] = tmp_db
    cfg.__post_init__()
    return cfg


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("ground_truth", help="CSV: clip,expected")
    p.add_argument("--config", default=None)
    p.add_argument("--debug-dir", default=None,
                   help="If set, write an overlay video per clip here")
    args = p.parse_args(argv)

    from juggle_tracker.pipeline import Pipeline  # heavy import; after argparse

    cfg = _sandbox(load_config(args.config))
    pipe = Pipeline(cfg)

    rows = []
    with open(args.ground_truth, newline="") as fh:
        for r in csv.DictReader(fh):
            rows.append((r["clip"], int(r["expected"])))

    if args.debug_dir:
        os.makedirs(args.debug_dir, exist_ok=True)

    print(f"{'clip':<40}{'pred':>6}{'exp':>6}{'err':>6}")
    print("-" * 58)
    abs_err_total = 0
    for clip, expected in rows:
        dbg = (os.path.join(args.debug_dir,
                            os.path.basename(clip) + ".dbg.mp4")
               if args.debug_dir else None)
        res = pipe.process(clip, debug_video=dbg)
        pred = max((s["count"] for s in res.streaks), default=0)
        err = pred - expected
        abs_err_total += abs(err)
        print(f"{os.path.basename(clip):<40}{pred:>6}{expected:>6}{err:>+6}")

    n = len(rows) or 1
    print("-" * 58)
    print(f"MAE (mean absolute error): {abs_err_total / n:.2f} juggles over {len(rows)} clips")
    pipe.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
