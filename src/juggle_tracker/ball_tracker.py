"""Short-gap ball position bridge.

The ball detector misses the fast, motion-blurred ball for a few frames at a
time (mid-arc). Feeding those gaps to the juggle counter as "missing" breaks the
y-signal and resets streaks. This bridges *short* dropouts by predicting the
ball's position with a constant-velocity model from the last two confirmed
detections, so the arc stays continuous and consecutive contacts chain into a
real streak. Gaps longer than ``max_bridge_frames`` are reported as genuinely
lost (``None``), so a truly missing ball still ends the streak.
"""
from __future__ import annotations

from collections import deque
from typing import Optional, Tuple


class BallTracker:
    def __init__(self, max_bridge_frames: int = 8, history: int = 4):
        self.max_bridge = max(0, int(max_bridge_frames))
        self._pts: deque = deque(maxlen=max(2, history))
        self._last_frame: Optional[int] = None

    @property
    def last_xy(self) -> Optional[Tuple[float, float]]:
        """Last confirmed (detected) ball position, or None."""
        if self._pts:
            return (self._pts[-1][1], self._pts[-1][2])
        return None

    def update(
        self, frame_index: int, ball_xy: Optional[Tuple[float, float]]
    ) -> Tuple[Optional[Tuple[float, float]], bool]:
        """Feed one frame's ball detection (or None).

        Returns ``(xy, bridged)`` where ``xy`` is the real detection, a predicted
        position during a short gap (``bridged=True``), or ``None`` when the gap
        exceeds ``max_bridge_frames`` or there's no motion history yet."""
        if ball_xy is not None:
            self._pts.append((frame_index, float(ball_xy[0]), float(ball_xy[1])))
            self._last_frame = frame_index
            return (float(ball_xy[0]), float(ball_xy[1])), False

        # Ball missing this frame — bridge if we can.
        if self.max_bridge <= 0 or self._last_frame is None or len(self._pts) < 2:
            return None, False
        gap = frame_index - self._last_frame
        if gap > self.max_bridge:
            return None, False
        f0, x0, y0 = self._pts[-1]
        f1, x1, y1 = self._pts[-2]
        span = max(1, f0 - f1)
        vx = (x0 - x1) / span
        vy = (y0 - y1) / span
        step = frame_index - f0
        return (x0 + vx * step, y0 + vy * step), True
