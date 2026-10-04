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
    def __init__(self, max_bridge_frames: int = 8, history: int = 4,
                 max_speed_px: float = 60.0, stuck_frames: int = 20,
                 stuck_px: float = 8.0):
        self.max_bridge = max(0, int(max_bridge_frames))
        self._pts: deque = deque(maxlen=max(2, history))
        self._last_frame: Optional[int] = None
        # Caps extrapolated px/frame so noisy detections can't fling the prediction.
        self.max_speed = float(max_speed_px)
        # (width, height) of the analysis frame; predictions outside it are dropped.
        self.frame_size: Optional[Tuple[int, int]] = None
        # A "ball" that barely moves for stuck_frames is a static false positive.
        self.stuck_frames = max(0, int(stuck_frames))
        self.stuck_px = float(stuck_px)
        self._recent: deque = deque(maxlen=max(1, self.stuck_frames))
        self._anchor: Optional[Tuple[float, float]] = None

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
        if ball_xy is not None and self._is_stuck(ball_xy):
            # Forget motion history so the fallback doesn't keep re-latching here.
            self._pts.clear()
            self._last_frame = None
            return None, False

        if ball_xy is not None:
            self._pts.append((frame_index, float(ball_xy[0]), float(ball_xy[1])))
            self._last_frame = frame_index
            return (float(ball_xy[0]), float(ball_xy[1])), False

        # Ball missing this frame — bridge if we can.
        pred = self.predict(frame_index)
        return pred, pred is not None

    def _is_stuck(self, xy: Tuple[float, float]) -> bool:
        if self.stuck_frames <= 0:
            return False
        x, y = float(xy[0]), float(xy[1])
        if self._anchor is not None:
            ax, ay = self._anchor
            if ((x - ax) ** 2 + (y - ay) ** 2) ** 0.5 <= self.stuck_px * 1.5:
                return True
            self._anchor = None
        self._recent.append((x, y))
        if len(self._recent) < self.stuck_frames:
            return False
        xs = [p[0] for p in self._recent]
        ys = [p[1] for p in self._recent]
        if max(xs) - min(xs) <= self.stuck_px and max(ys) - min(ys) <= self.stuck_px:
            self._anchor = (sum(xs) / len(xs), sum(ys) / len(ys))
            self._recent.clear()
            return True
        return False

    def predict(self, frame_index: int) -> Optional[Tuple[float, float]]:
        """Constant-velocity position estimate, or None outside the bridge window."""
        if self.max_bridge <= 0 or self._last_frame is None or len(self._pts) < 2:
            return None
        gap = frame_index - self._last_frame
        if gap > self.max_bridge:
            return None
        f0, x0, y0 = self._pts[-1]
        f1, x1, y1 = self._pts[-2]
        span = max(1, f0 - f1)
        vx = (x0 - x1) / span
        vy = (y0 - y1) / span
        speed = (vx * vx + vy * vy) ** 0.5
        if self.max_speed > 0 and speed > self.max_speed:
            vx, vy = vx * self.max_speed / speed, vy * self.max_speed / speed
        step = frame_index - f0
        px, py = x0 + vx * step, y0 + vy * step
        if self.frame_size is not None and not (
                0 <= px < self.frame_size[0] and 0 <= py < self.frame_size[1]):
            return None  # ball has left the frame
        return (px, py)
