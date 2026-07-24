"""Labelled-clip evaluation harness for the juggle-counting state machine.

Why this exists
---------------
``tune.py`` scores parameter sets by re-running the *full* pipeline (YOLO +
pose + ball detection) on every clip for every candidate config. That's slow and
it folds detection error into counting error, so you can't tell whether a miss
came from the state machine or from the ball detector.

This harness splits the two:

1. **dump-trace** runs detection **once** per clip and records the exact
   per-frame observations fed to :class:`~juggle_tracker.juggle.JuggleCounter`
   — ``(frame_index, ball_xy, keypoints, ground_y)`` — to a compact JSONL
   "trace" file.
2. **replay** feeds a trace back through a fresh ``JuggleCounter`` with any
   parameters, with **zero ML cost**. Iterating on smoothing / refractory /
   contact-radius is now milliseconds per clip and fully deterministic, so it
   also runs in CI.

Ground truth comes from the filename convention shared with ``tune.py``
(``5_juggles.mp4`` -> a run of 5), or a CSV/JSON label file. The primary metric
is the tracker's **longest single streak** vs the label (what the high-score
board records), reported alongside mean-absolute-error and how many separate
streaks each clip produced (exposing over-splitting from ball dropout).

Trace format (JSONL)
--------------------
Line 1 is a meta header ``{"meta": {"clip": ..., "frame_height": H, "fps": F}}``.
Each subsequent line is one frame::

    {"f": 12, "ball": [320.5, 210.0], "kps": {"left_ankle": [320,300,0.9]}, "ground": 305.0}

``ball``/``kps``/``ground`` may be ``null``.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Optional

from .juggle import JuggleCounter, StreakEvent

TRACE_SUFFIX = ".trace.jsonl"


# --------------------------------------------------------------------------
# Trace I/O
# --------------------------------------------------------------------------
def _ball_to_json(ball_xy) -> Optional[list]:
    return None if ball_xy is None else [float(ball_xy[0]), float(ball_xy[1])]


def _kps_to_json(kps) -> Optional[dict]:
    if not kps:
        return None
    return {name: [float(x), float(y), float(c)] for name, (x, y, c) in kps.items()}


def make_trace_sink(fh):
    """Return a ``trace_sink`` callable that writes one JSONL frame per call.

    Pass it to :meth:`Pipeline.process(clip, trace_sink=...)`."""
    def _sink(frame_index: int, ball_xy, kps, ground_y) -> None:
        fh.write(json.dumps({
            "f": int(frame_index),
            "ball": _ball_to_json(ball_xy),
            "kps": _kps_to_json(kps),
            "ground": None if ground_y is None else float(ground_y),
        }) + "\n")
    return _sink


def load_trace(path: str) -> tuple[dict, list[dict]]:
    """Return (meta, frames) from a trace file. ``frames`` are decoded dicts
    with keys f/ball/kps/ground, ready for :func:`replay`."""
    meta: dict = {}
    frames: list[dict] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if "meta" in obj and not frames:
                meta = obj["meta"]
                continue
            frames.append(obj)
    return meta, frames


# --------------------------------------------------------------------------
# Detection-free replay
# --------------------------------------------------------------------------
@dataclass
class ReplayResult:
    streaks: list[int]          # count of every completed streak, in order
    best: int                   # longest single streak (the high-score metric)
    total: int                  # sum of all streak counts
    n_streaks: int              # number of separate streaks (over-split signal)
    events: list[StreakEvent]   # full events (count + ended_reason + frames)


def replay(frames: list[dict], params: Optional[dict] = None,
           frame_height: int = 1080) -> ReplayResult:
    """Feed a decoded trace through a fresh ``JuggleCounter`` and summarise.

    ``params`` overrides counter kwargs (smooth_window, min_arc_px,
    contact_radius_px, lost_frames_reset, min_contact_gap_frames,
    kp_staleness_frames, valid_keypoints, illegal_keypoints)."""
    kw = dict(params or {})
    counter = JuggleCounter(frame_height=frame_height, **kw)
    events: list[StreakEvent] = []
    last_f = 0
    for fr in frames:
        f = int(fr["f"])
        last_f = f
        ball = fr.get("ball")
        ball_xy = (float(ball[0]), float(ball[1])) if ball else None
        raw_kps = fr.get("kps")
        kps = ({name: (float(v[0]), float(v[1]), float(v[2]))
                for name, v in raw_kps.items()} if raw_kps else None)
        ground = fr.get("ground")
        ev = counter.update(f, ball_xy, kps,
                            ground_y=None if ground is None else float(ground))
        if ev is not None and ev.count > 0:
            events.append(ev)
    tail = counter.flush(last_f + 1)
    if tail is not None and tail.count > 0:
        events.append(tail)
    counts = [e.count for e in events]
    return ReplayResult(
        streaks=counts,
        best=max(counts, default=0),
        total=sum(counts),
        n_streaks=len(counts),
        events=events,
    )


# --------------------------------------------------------------------------
# Scoring against labels
# --------------------------------------------------------------------------
@dataclass
class ClipScore:
    name: str
    expected: int
    pred: int          # longest streak
    err: int           # pred - expected
    n_streaks: int
    total: int


@dataclass
class EvalReport:
    rows: list[ClipScore]
    exact: int         # clips whose longest streak == label
    mae: float         # mean absolute error of longest streak

    @property
    def n(self) -> int:
        return len(self.rows)


def counter_params_from_cfg(cfg) -> dict:
    """Extract the JuggleCounter-affecting params from a loaded config."""
    j = cfg.juggle
    return {
        "smooth_window": int(j.get("smooth_window", 5)),
        "min_arc_px": float(j.get("min_arc_px", 18)),
        "contact_radius_px": float(j.get("contact_radius_px", 90)),
        "lost_frames_reset": int(j.get("lost_frames_reset", 15)),
        "min_contact_gap_frames": int(j.get("min_contact_gap_frames", 6)),
        "kp_staleness_frames": int(j.get("kp_staleness_frames", 6)),
        "valid_keypoints": set(j.get("valid_keypoints", [])) or None,
        "illegal_keypoints": set(j.get("illegal_keypoints", [])) or None,
    }


def evaluate(labelled_traces: list[tuple[str, int]],
             params: Optional[dict] = None) -> EvalReport:
    """Replay each (trace_path, expected_count) and score longest-streak error."""
    rows: list[ClipScore] = []
    abs_err = 0
    exact = 0
    for path, expected in labelled_traces:
        meta, frames = load_trace(path)
        fh = int(meta.get("frame_height", 1080))
        res = replay(frames, params, frame_height=fh)
        err = res.best - int(expected)
        abs_err += abs(err)
        exact += int(err == 0)
        rows.append(ClipScore(
            name=os.path.basename(path), expected=int(expected), pred=res.best,
            err=err, n_streaks=res.n_streaks, total=res.total))
    mae = (abs_err / len(rows)) if rows else 0.0
    return EvalReport(rows=rows, exact=exact, mae=mae)


def format_report(report: EvalReport) -> str:
    """Human-readable table + aggregate line (mirrors tune.py's layout)."""
    lines = [f"\n{'trace':<40}{'exp':>5}{'pred':>6}{'err':>6}"
             f"{'streaks':>9}{'total':>7}"]
    lines.append("-" * 73)
    for r in report.rows:
        lines.append(f"{r.name:<40}{r.expected:>5}{r.pred:>6}{r.err:>+6}"
                     f"{r.n_streaks:>9}{r.total:>7}")
    lines.append("-" * 73)
    lines.append(f"clips: {report.n}   exact-longest-streak: {report.exact}"
                 f"/{report.n}   MAE: {report.mae:.2f}")
    return "\n".join(lines)
