"""Auto-calibration: tune the tracker's counting params against labelled clips.

Given clips whose filenames encode the *confirmed* juggle count
(``5_juggles.mp4`` = a run of 5), this repeatedly processes them and tweaks the
detection/counting parameters until the tracker's reported count matches the
labels as closely as possible, then (optionally) writes the winning values into
``calibration_overrides.yaml`` — the same file the Home Assistant calibration
sliders edit.

Exposed for both the CLI (``juggle_tracker.cli tune``) and the standalone
``tools/tune.py`` wrapper:
  * :func:`label_from_filename`, :func:`discover_clips`
  * :func:`add_arguments` — register the CLI flags on an argparse parser
  * :func:`run` — execute a tuning session from a parsed args namespace
  * :func:`main` — standalone entry point (own argparse)

How the count is scored
-----------------------
A clip named ``5_juggles.mp4`` is one continuous run of 5 contacts, so the right
answer is the tracker's **longest single streak** (what the high-score board
records). Objective, in order: (1) maximise clips whose longest streak == label,
(2) minimise mean absolute error. The report also shows how many separate
streaks each clip produced, exposing over-splitting (ball lost mid-run).

Search strategy
---------------
Coordinate descent: sweep one parameter across its range, keep the value that
scores best, move to the next, repeat for several rounds until nothing helps.
Identical configurations are cached. The counting params (juggle.* / processing.*)
are re-read every ``process()`` call, so changing them reuses the loaded models;
only ``models.*`` / ``ball_fallback.*`` changes force a model reload.

Side effects
------------
None: Home Assistant publishing is disabled, a throwaway SQLite DB is used, and
high-score replay-video rendering is turned off, so a run never touches real
scores or spends time transcoding.
"""
from __future__ import annotations

import argparse
import csv
import glob
import os
import re
import sys
import tempfile
import time


# --- label parsing ----------------------------------------------------------
_JUGGLE_NUM = re.compile(r"(\d+)\s*[-_ ]?\s*juggl", re.IGNORECASE)
_JUGGLE_NUM_AFTER = re.compile(r"juggl\w*\s*[-_ ]?\s*(\d+)", re.IGNORECASE)
# A standalone integer token (not glued to letters/other digits), e.g. the "5"
# in "5.mp4" or "art_5.mp4" — but NOT the "3" in "kid3" or a date stamp.
_STANDALONE_INT = re.compile(r"(?<![A-Za-z0-9])(\d+)(?![A-Za-z0-9])")
_VIDEO_EXTS = (".mp4", ".mkv", ".mov", ".avi")


def label_from_filename(path: str):
    """Return the confirmed juggle count encoded in a filename, or None.

    Prefers a number attached to the word "juggle" (either order). Falls back to
    a standalone integer token only when it's a plausible count (<1000), so
    "kid3_no_label.mp4" and date stamps are not misread as counts."""
    stem = os.path.splitext(os.path.basename(path))[0]
    for rx in (_JUGGLE_NUM, _JUGGLE_NUM_AFTER):
        m = rx.search(stem)
        if m:
            return int(m.group(1))
    for m in _STANDALONE_INT.finditer(stem):
        val = int(m.group(1))
        if val < 1000:
            return val
    return None


def discover_clips(source: str, truth_csv: str | None):
    """Build a list of (clip_path, expected_count) from a CSV or a folder."""
    rows: list[tuple[str, int]] = []
    if truth_csv:
        base = os.path.dirname(os.path.abspath(truth_csv))
        with open(truth_csv, newline="") as fh:
            for r in csv.DictReader(fh):
                clip = r["clip"]
                if not os.path.isabs(clip):
                    clip = os.path.join(base, clip)
                rows.append((clip, int(r["expected"])))
        return rows

    if os.path.isdir(source):
        paths: list[str] = []
        for ext in _VIDEO_EXTS:
            paths += glob.glob(os.path.join(source, f"*{ext}"))
            paths += glob.glob(os.path.join(source, f"*{ext.upper()}"))
        for p in sorted(set(paths)):
            lbl = label_from_filename(p)
            if lbl is None:
                print(f"  ! skipping (no count in name): {os.path.basename(p)}",
                      file=sys.stderr)
                continue
            rows.append((p, lbl))
        return rows

    # A single file.
    lbl = label_from_filename(source)
    if lbl is not None:
        rows.append((source, lbl))
    return rows


# --- parameter search space -------------------------------------------------
# Parameters that actually move the juggle *count*. Identity/face params are
# excluded (no enrollment happens in the sandbox, so they cannot affect counts).
QUICK_PARAMS = [
    "person_stride", "contact_radius_px", "min_arc_px", "ground_margin_px",
    "smooth_window", "lost_frames_reset", "max_bridge_frames", "ball_conf",
]
# 'full' adds the remaining count-relevant knobs (resolution + person conf +
# classical-fallback tuning). Still excludes face/identity params.
FULL_EXTRA = [
    "person_conf", "infer_long_edge",
    "fallback_sensitivity", "fallback_max_radius", "fallback_search_radius",
]
EXCLUDE_ALWAYS = {"face_conf", "match_threshold", "vote_min_frames"}

# Top-level config sections whose values are read in Pipeline.__init__ (model
# construction) and therefore require rebuilding the Pipeline when they change.
# Everything else (juggle.*, processing.*) is re-read every process() call, so
# we can mutate it in place and reuse the loaded models — the whole reason the
# tuner is tractable on this hardware.
_REBUILD_SECTIONS = {"models", "ball_fallback", "identity"}


def _candidate_values(spec: dict, current, max_candidates: int = 7):
    """Even-spaced candidate values across a param's range, incl. the current."""
    lo, hi, step = float(spec["min"]), float(spec["max"]), float(spec["step"])
    n_steps = int(round((hi - lo) / step)) + 1 if step > 0 else 1
    if n_steps <= max_candidates:
        vals = [lo + i * step for i in range(n_steps)]
    else:
        # Sub-sample evenly, snapping each to the step grid.
        vals = []
        for i in range(max_candidates):
            v = lo + (hi - lo) * i / (max_candidates - 1)
            v = lo + round((v - lo) / step) * step
            vals.append(v)
    cast = (lambda x: int(round(x))) if spec.get("int") else (lambda x: round(x, 4))
    out = sorted({cast(v) for v in vals} | {cast(float(current))})
    return out


# --- sandbox (side-effect free evaluation) ---------------------------------
def _sandbox(cfg):
    """Disable HA, use a throwaway DB, and turn off high-score video rendering.

    Without disabling high-score video, a fresh DB would treat the first streak
    of every clip as a new record and kick off an expensive annotated ffmpeg
    second pass on every single evaluation."""
    cfg.raw.setdefault("home_assistant", {})["enabled"] = False
    cfg.raw.setdefault("capture", {})["save_highscore_video"] = False
    tmp_db = os.path.join(tempfile.mkdtemp(prefix="jt_tune_"), "tune.db")
    cfg.raw.setdefault("database", {})["path"] = tmp_db
    cfg.__post_init__()
    return cfg


def _set_path(raw: dict, path: tuple, value) -> None:
    d = raw
    for k in path[:-1]:
        d = d.setdefault(k, {})
    d[path[-1]] = value


class Evaluator:
    """Evaluates a configuration against the labelled clip set.

    Reuses one loaded Pipeline as long as the model-affecting ('rebuild')
    parameters are unchanged; only rebuilds (reloads YOLO/pose) when one of
    those changes. Cheap params are mutated on the shared config in place.
    """

    def __init__(self, cfg, specs: dict, clips, debug_dir: str | None = None):
        from .pipeline import Pipeline  # heavy import, deferred
        self._Pipeline = Pipeline
        self.cfg = cfg
        self.specs = specs
        self.clips = clips
        self.debug_dir = debug_dir
        self._pipe = None
        self._pipe_sig = None
        self._cache: dict[tuple, dict] = {}
        self.n_process_calls = 0

    def _rebuild_sig(self, values: dict) -> tuple:
        sig = []
        for name, spec in self.specs.items():
            if spec["path"][0] in _REBUILD_SECTIONS:
                sig.append((name, values[name]))
        return tuple(sig)

    def _apply(self, values: dict) -> None:
        for name, spec in self.specs.items():
            _set_path(self.cfg.raw, spec["path"], values[name])
        self.cfg.__post_init__()

    def _pipeline_for(self, values: dict):
        sig = self._rebuild_sig(values)
        if self._pipe is None or sig != self._pipe_sig:
            if self._pipe is not None:
                self._pipe.close()
            self._apply(values)               # rebuild params must be set first
            self._pipe = self._Pipeline(self.cfg)
            self._pipe_sig = sig
        else:
            self._apply(values)               # cheap params re-read per process
        return self._pipe

    def evaluate(self, values: dict, debug: bool = False) -> dict:
        key = tuple(sorted((k, values[k]) for k in self.specs))
        if not debug and key in self._cache:
            return self._cache[key]

        pipe = self._pipeline_for(values)
        per_clip = []
        abs_err = exact = 0
        for clip, expected in self.clips:
            dbg = None
            if debug and self.debug_dir:
                dbg = os.path.join(self.debug_dir,
                                   os.path.basename(clip) + ".dbg.mp4")
            res = pipe.process(clip, debug_video=dbg)
            self.n_process_calls += 1
            counts = [s["count"] for s in res.streaks]
            pred = max(counts, default=0)
            err = pred - expected
            abs_err += abs(err)
            exact += int(err == 0)
            per_clip.append({
                "clip": os.path.basename(clip), "expected": expected,
                "pred": pred, "err": err, "n_streaks": len(counts),
                "total": sum(counts),
            })
        n = len(self.clips) or 1
        result = {
            "exact": exact, "mae": abs_err / n, "n": len(self.clips),
            "per_clip": per_clip, "values": dict(values),
        }
        if not debug:
            self._cache[key] = result
        return result

    def close(self):
        if self._pipe is not None:
            self._pipe.close()
            self._pipe = None


def _loss(result: dict) -> tuple:
    """Lower is better: maximise exact hits, then minimise mean abs error."""
    return (-result["exact"], round(result["mae"], 4))


def _fmt_values(values: dict, specs: dict) -> str:
    return ", ".join(f"{k}={values[k]}" for k in specs)


def coordinate_descent(ev: Evaluator, specs: dict, start: dict,
                       rounds: int, max_evals: int, max_candidates: int) -> dict:
    best_vals = dict(start)
    best = ev.evaluate(best_vals)
    evals = 1
    print(f"\nstart: exact={best['exact']}/{best['n']} MAE={best['mae']:.2f}"
          f"  [{_fmt_values(best_vals, specs)}]")

    for rnd in range(1, rounds + 1):
        loss_at_round_start = _loss(best)
        print(f"\n--- round {rnd} ---")
        for name, spec in specs.items():
            for v in _candidate_values(spec, best_vals[name], max_candidates):
                if v == best_vals[name]:
                    continue
                if max_evals and evals >= max_evals:
                    print("  (max-evals budget reached)")
                    print(f"\nbest: exact={best['exact']}/{best['n']} "
                          f"MAE={best['mae']:.2f}")
                    return {"values": best_vals, "result": best}
                trial = dict(best_vals)
                trial[name] = v
                r = ev.evaluate(trial)
                evals += 1
                better = _loss(r) < _loss(best)
                print(f"  [{'*' if better else ' '}] {name}={v!s:<8} -> "
                      f"exact={r['exact']}/{r['n']} MAE={r['mae']:.2f}")
                if better:
                    best, best_vals = r, dict(trial)

        # Convergence: a full round produced no strictly-better configuration.
        if _loss(best) == loss_at_round_start:
            print("  (converged — no improvement this round)")
            break

    print(f"\nbest: exact={best['exact']}/{best['n']} MAE={best['mae']:.2f}")
    return {"values": best_vals, "result": best}


def _print_table(result: dict) -> None:
    print(f"\n{'clip':<34}{'exp':>5}{'pred':>6}{'err':>6}{'streaks':>9}{'total':>7}")
    print("-" * 67)
    for r in result["per_clip"]:
        print(f"{r['clip']:<34}{r['expected']:>5}{r['pred']:>6}{r['err']:>+6}"
              f"{r['n_streaks']:>9}{r['total']:>7}")
    print("-" * 67)
    print(f"exact matches: {result['exact']}/{result['n']}   "
          f"MAE: {result['mae']:.2f} juggles")


# --- CLI glue ---------------------------------------------------------------
def add_arguments(parser: argparse.ArgumentParser, include_config: bool = False) -> None:
    """Register the tuner's flags on ``parser`` (shared by CLI + standalone).

    Set ``include_config=True`` for the standalone script, which lacks the
    package CLI's global ``--config`` option."""
    if include_config:
        parser.add_argument("--config", default=None, help="Path to config.yaml")
    parser.add_argument("source", help="Folder of labelled clips (or a single clip)")
    parser.add_argument("--truth", default=None,
                        help="CSV (clip,expected) instead of filename labels")
    parser.add_argument("--space", choices=["quick", "full"], default="quick",
                        help="Which parameters to tune (default: quick)")
    parser.add_argument("--params", default=None,
                        help="Comma-separated param slugs to tune (overrides --space)")
    parser.add_argument("--rounds", type=int, default=3,
                        help="Max coordinate-descent refinement rounds (default 3)")
    parser.add_argument("--max-evals", type=int, default=0,
                        help="Cap total config evaluations (0 = unlimited)")
    parser.add_argument("--max-candidates", type=int, default=7,
                        help="Values tried per parameter per round (default 7)")
    parser.add_argument("--write", action="store_true",
                        help="Write winning values into calibration_overrides.yaml")
    parser.add_argument("--debug-dir", default=None,
                        help="Write an overlay video per clip for the winning config")


def run(args) -> int:
    """Execute a tuning session from a parsed args namespace."""
    from .config import (load_config, TUNABLE_PARAMS, get_by_path,
                         clamp_param, set_override)

    clips = discover_clips(args.source, args.truth)
    if not clips:
        print("No labelled clips found. Name files like '5_juggles.mp4' or "
              "pass --truth CSV.", file=sys.stderr)
        return 2
    print(f"Loaded {len(clips)} labelled clip(s):")
    for c, e in clips:
        print(f"  {os.path.basename(c):<40} expected={e}")

    # Resolve the parameter search space to concrete specs.
    by_slug = {sp["slug"]: sp for sp in TUNABLE_PARAMS}
    if args.params:
        wanted = [s.strip() for s in args.params.split(",") if s.strip()]
    else:
        wanted = QUICK_PARAMS + (FULL_EXTRA if args.space == "full" else [])
    specs: dict[str, dict] = {}
    for slug in wanted:
        if slug in EXCLUDE_ALWAYS:
            print(f"  (ignoring {slug}: does not affect counts in sandbox)")
            continue
        sp = by_slug.get(slug)
        if not sp:
            print(f"  ! unknown param '{slug}' — valid: {sorted(by_slug)}",
                  file=sys.stderr)
            return 2
        specs[slug] = sp
    if not specs:
        print("No tunable parameters selected.", file=sys.stderr)
        return 2
    print(f"\nTuning {len(specs)} parameter(s): {', '.join(specs)}")

    cfg = _sandbox(load_config(args.config))

    # Starting point = current (config.yaml + any existing overrides) values,
    # clamped into each param's bounds.
    start = {}
    for name, spec in specs.items():
        cur = get_by_path(cfg.raw, spec["path"], spec["min"])
        start[name] = clamp_param(spec, cur)

    if args.debug_dir:
        os.makedirs(args.debug_dir, exist_ok=True)

    ev = Evaluator(cfg, specs, clips, debug_dir=args.debug_dir)
    t0 = time.time()
    try:
        outcome = coordinate_descent(
            ev, specs, start, rounds=args.rounds,
            max_evals=args.max_evals, max_candidates=args.max_candidates)
        best_vals = outcome["values"]

        # Final report on the winning config (optionally with debug overlays).
        final = ev.evaluate(best_vals, debug=bool(args.debug_dir))
        print("\n" + "=" * 67)
        print("WINNING CONFIGURATION")
        print("=" * 67)
        for name in specs:
            print(f"  {name:<22} {best_vals[name]}")
        _print_table(final)
        dt = time.time() - t0
        print(f"\n{ev.n_process_calls} clip-processings in {dt:.0f}s "
              f"({len(ev._cache)} unique configs evaluated).")

        if args.write:
            for name, spec in specs.items():
                set_override(cfg.overrides_path, spec["path"], best_vals[name])
            print(f"\nWrote winning values to {cfg.overrides_path}")
            print("Restart juggle-tracker.service (or press 'Apply Calibration & "
                  "Restart' in HA) to apply.")
        else:
            print("\n(Re-run with --write to save these into "
                  "calibration_overrides.yaml.)")
    finally:
        ev.close()
    return 0


def main(argv=None) -> int:
    """Standalone entry point (own argparse)."""
    p = argparse.ArgumentParser(
        prog="juggle_tracker.tune", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    add_arguments(p, include_config=True)
    args = p.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
