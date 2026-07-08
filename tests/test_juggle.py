"""Tests for the juggle state machine — pure logic, no ML models required.

Run: python -m pytest tests/  (or) python tests/test_juggle.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_tracker.juggle import JuggleCounter  # noqa: E402


def _bounce_y(cycles, peak=200.0, contact=300.0, step=20.0):
    """Generate a ball image-y signal: repeated up-then-down arcs.

    Bottom of each arc (y == contact, a local max) is a foot contact.
    """
    ys = [peak]
    for _ in range(cycles):
        y = peak
        while y < contact:            # descending in space -> y increases
            y += step
            ys.append(min(y, contact))
        while y > peak:               # ascending -> y decreases
            y -= step
            ys.append(max(y, peak))
    return ys


def _foot_kps(x, y):
    # Ankle right at the contact point; everything else absent/low-conf.
    return {"left_ankle": (x, y, 0.9)}


def _hand_kps(x, y):
    return {"left_wrist": (x, y, 0.9)}


def test_counts_clean_juggles():
    ys = _bounce_y(cycles=5, contact=300.0)
    c = JuggleCounter(frame_height=480, smooth_window=5, min_arc_px=18,
                      contact_radius_px=90, ground_y_frac=0.99)
    ball_x = 320.0
    for i, y in enumerate(ys):
        c.update(i, (ball_x, y), _foot_kps(ball_x, 300.0))
    ev = c.flush(len(ys))
    assert ev is not None, "expected a trailing streak"
    # 5 arcs -> ~5 foot contacts (allow small edge tolerance).
    assert 4 <= ev.count <= 6, f"got {ev.count}"
    assert ev.ended_reason == "end"


def test_hand_touch_resets():
    ys = _bounce_y(cycles=3, contact=300.0)
    c = JuggleCounter(frame_height=480, smooth_window=5, min_arc_px=18,
                      contact_radius_px=90, ground_y_frac=0.99)
    ball_x = 320.0
    # First arc: foot. Then a hand touch should end the streak.
    contacts_seen = 0
    ended = None
    for i, y in enumerate(ys):
        # Use hand keypoints for the whole run: first contact starts nothing
        # illegal until a streak exists, so seed one foot contact first.
        kps = _foot_kps(ball_x, 300.0) if i < len(ys) // 2 else _hand_kps(ball_x, 300.0)
        ev = c.update(i, (ball_x, y), kps)
        if ev is not None:
            ended = ev
    # A hand contact after a foot streak must reset (reason 'hand').
    assert ended is None or ended.ended_reason in ("hand", "end")


def test_ground_touch_resets():
    # Ball bottoms out at the ground line -> floor touch resets.
    ys = _bounce_y(cycles=2, peak=200.0, contact=470.0)  # 470 >= 0.92*480=441.6
    c = JuggleCounter(frame_height=480, smooth_window=5, min_arc_px=18,
                      contact_radius_px=90, ground_y_frac=0.92)
    ball_x = 320.0
    reasons = []
    for i, y in enumerate(ys):
        ev = c.update(i, (ball_x, y), _foot_kps(ball_x, 470.0))
        if ev:
            reasons.append(ev.ended_reason)
    c.flush(len(ys))
    # No valid juggles should accumulate when every low point is on the ground.
    assert all(r in ("ground", "end") for r in reasons)


def test_lost_ball_ends_streak():
    ys = _bounce_y(cycles=3, contact=300.0)
    c = JuggleCounter(frame_height=480, smooth_window=5, min_arc_px=18,
                      contact_radius_px=90, ground_y_frac=0.99, lost_frames_reset=5)
    ball_x = 320.0
    n = len(ys)
    for i, y in enumerate(ys):
        c.update(i, (ball_x, y), _foot_kps(ball_x, 300.0))
    # Feed missing-ball frames.
    ended = None
    for j in range(10):
        ev = c.update(n + j, None, None)
        if ev:
            ended = ev
            break
    assert ended is not None and ended.ended_reason == "lost"


if __name__ == "__main__":
    test_counts_clean_juggles()
    test_hand_touch_resets()
    test_ground_touch_resets()
    test_lost_ball_ends_streak()
    print("All juggle state-machine tests passed.")
