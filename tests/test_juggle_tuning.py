"""Tests for the juggle-counting bug fixes and the eval-harness replay.

Pure logic — no ML, no video. Run: python -m pytest tests/
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_tracker.juggle import JuggleCounter  # noqa: E402
from juggle_tracker import eval_harness as ev  # noqa: E402


def _foot(x, y):
    return {"left_ankle": (x, y, 0.9)}


def _arc(ys, peak=200.0, contact=300.0, step=20.0):
    """One descending+ascending arc as a list of y samples."""
    out = []
    y = peak
    while y < contact:
        y += step
        out.append(min(y, contact))
    while y > peak:
        y -= step
        out.append(max(y, peak))
    return out


# --------------------------------------------------------------------------
# Fix #1: median smoothing rejects a single-frame spike
# --------------------------------------------------------------------------
def test_smoothing_rejects_single_frame_spike():
    """A lone noisy sample that dips then recovers must NOT be counted as a
    contact — the median filter should absorb it."""
    c = JuggleCounter(frame_height=480, smooth_window=5, min_arc_px=18,
                      contact_radius_px=90, min_contact_gap_frames=0)
    ball_x = 320.0
    # Ball hovering high (small values), with one 1-frame downward spike.
    ys = [200, 200, 200, 260, 200, 200, 200, 200]  # index 3 is the spike
    for i, y in enumerate(ys):
        c.update(i, (ball_x, y), _foot(ball_x, 300.0))
    ev_ = c.flush(len(ys))
    count = ev_.count if ev_ else 0
    assert count == 0, f"single-frame spike was miscounted as {count} contacts"


# --------------------------------------------------------------------------
# Fix #2: no blind +1 when there's no pose to classify the contact
# --------------------------------------------------------------------------
def test_no_blind_count_without_pose():
    """A clean arc with NO keypoints (and no cached pose) must not be counted —
    the old code blindly incremented the streak on pose-less contact frames."""
    c = JuggleCounter(frame_height=480, smooth_window=5, min_arc_px=18,
                      contact_radius_px=90, kp_staleness_frames=0)
    ball_x = 320.0
    ys = [y for _ in range(4) for y in _arc([])]  # several clean arcs
    for i, y in enumerate(ys):
        c.update(i, (ball_x, y), None)  # never any pose
    ev_ = c.flush(len(ys))
    count = ev_.count if ev_ else 0
    assert count == 0, f"counted {count} contacts with no pose evidence"


def test_stale_pose_within_window_still_classifies():
    """A cached pose within kp_staleness_frames should still classify a contact
    on a pose-skipped frame (so stride>1 doesn't lose real juggles)."""
    c = JuggleCounter(frame_height=480, smooth_window=3, min_arc_px=18,
                      contact_radius_px=90, kp_staleness_frames=30,
                      min_contact_gap_frames=0)
    ball_x = 320.0
    ys = [y for _ in range(4) for y in _arc([])]
    for i, y in enumerate(ys):
        # Provide pose only on the very first frame, None thereafter.
        kps = _foot(ball_x, 300.0) if i == 0 else None
        c.update(i, (ball_x, y), kps)
    ev_ = c.flush(len(ys))
    assert ev_ is not None and ev_.count >= 3, \
        f"cached pose should classify contacts; got {ev_.count if ev_ else 0}"


# --------------------------------------------------------------------------
# Fix #3: refractory period rejects double-detected contacts
# --------------------------------------------------------------------------
def test_refractory_period_suppresses_close_double_contacts():
    """Two arc-bottoms closer than min_contact_gap_frames should count once."""
    ball_x = 320.0
    # Two back-to-back arcs ~6 frames each, contacts ~6 frames apart.
    arc = [240, 280, 320, 280, 240, 200]
    ys = [200] + arc + arc
    strict = JuggleCounter(frame_height=480, smooth_window=3, min_arc_px=15,
                           contact_radius_px=90, min_contact_gap_frames=10)
    loose = JuggleCounter(frame_height=480, smooth_window=3, min_arc_px=15,
                          contact_radius_px=90, min_contact_gap_frames=0)
    for i, y in enumerate(ys):
        strict.update(i, (ball_x, y), _foot(ball_x, 300.0))
        loose.update(i, (ball_x, y), _foot(ball_x, 300.0))
    s = strict.flush(len(ys))
    l = loose.flush(len(ys))
    sc = s.count if s else 0
    lc = l.count if l else 0
    assert lc >= 2, f"loose config should see both contacts, got {lc}"
    assert sc < lc, f"refractory should reduce count (strict={sc}, loose={lc})"


# --------------------------------------------------------------------------
# Eval harness: detection-free replay of a synthetic trace
# --------------------------------------------------------------------------
def _synthetic_frames(n_arcs=5, ball_x=320.0):
    frames = []
    i = 0
    for y in [y for _ in range(n_arcs) for y in _arc([])]:
        frames.append({"f": i, "ball": [ball_x, y],
                       "kps": {"left_ankle": [ball_x, 300.0, 0.9]},
                       "ground": None})
        i += 1
    return frames


def test_replay_counts_synthetic_arcs():
    frames = _synthetic_frames(n_arcs=5)
    res = ev.replay(frames, params={"smooth_window": 5, "min_arc_px": 18,
                                    "contact_radius_px": 90,
                                    "min_contact_gap_frames": 6},
                    frame_height=480)
    assert 4 <= res.best <= 6, f"expected ~5, got best={res.best}"
    assert res.total >= res.best


def test_trace_roundtrip_and_evaluate(tmp_path):
    """Write a trace file (meta + frames), reload, evaluate against a label
    encoded in the filename."""
    frames = _synthetic_frames(n_arcs=5)
    path = os.path.join(str(tmp_path), "5_juggles.trace.jsonl")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"meta": {"clip": "5_juggles.mp4",
                                      "frame_height": 480, "fps": 25.0}}) + "\n")
        for fr in frames:
            fh.write(json.dumps(fr) + "\n")
    meta, loaded = ev.load_trace(path)
    assert meta["frame_height"] == 480
    assert len(loaded) == len(frames)
    report = ev.evaluate([(path, 5)],
                         params={"smooth_window": 5, "min_arc_px": 18,
                                 "contact_radius_px": 90,
                                 "min_contact_gap_frames": 6})
    assert report.n == 1
    row = report.rows[0]
    assert abs(row.err) <= 1, f"longest-streak error too high: {row.err}"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
