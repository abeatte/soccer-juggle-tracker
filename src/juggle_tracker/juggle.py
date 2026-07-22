"""Juggle counting state machine.

Definition being counted
------------------------
A *juggle* is a ball contact with a body part that is **not** a hand/arm, with
the ball airborne between contacts and **never** touching the ground or another
object. A hand/arm contact or a ground touch ends (resets) the current streak.

How a contact is detected
--------------------------
We track the ball's **image-y** centroid over time. In image coordinates y grows
*downward*, so the bottom of each arc (where the ball meets a foot/knee) is a
**local maximum** in y. A contact event is therefore a local maximum in the
smoothed y-signal where the ball's vertical velocity flips from + (descending)
to − (ascending), with a minimum arc height between contacts to reject jitter.

Classifying a contact
----------------------
At each contact frame we take the ball centroid and find the nearest confident
body keypoint of the juggling person:
  * valid keypoint (foot/ankle/knee/shoulder/head)  -> streak += 1
  * illegal keypoint (wrist/elbow)                   -> streak resets (hand ball)
  * nearest keypoint too far (> contact_radius_px)   -> ambiguous; ignored
Additionally, if the ball's low point sits at/below the calibrated ground line,
it's a floor touch -> streak resets.

The machine is fed one (frame_index, ball_xy_or_None, keypoints_or_None) tuple
per frame via :meth:`update`, and emits completed streaks (with the reason they
ended) as :class:`StreakEvent` objects so the pipeline can persist them.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from .pose import Keypoints, nearest_keypoint

VALID_DEFAULT = {
    "left_shoulder", "right_shoulder", "left_knee", "right_knee",
    "left_ankle", "right_ankle", "nose", "left_eye", "right_eye",
}
ILLEGAL_DEFAULT = {"left_wrist", "right_wrist", "left_elbow", "right_elbow"}


@dataclass
class StreakEvent:
    count: int
    ended_reason: str      # 'hand' | 'ground' | 'lost' | 'end'
    start_frame: int
    end_frame: int


@dataclass
class JuggleCounter:
    frame_height: int
    smooth_window: int = 5
    min_arc_px: float = 18.0
    contact_radius_px: float = 90.0
    ground_y_frac: float = 0.92
    valid_keypoints: set[str] = field(default_factory=lambda: set(VALID_DEFAULT))
    illegal_keypoints: set[str] = field(default_factory=lambda: set(ILLEGAL_DEFAULT))
    lost_frames_reset: int = 15   # ball missing this many frames -> streak lost

    # ---- internal state ----
    _y_hist: deque = field(default_factory=lambda: deque(maxlen=7))
    _f_hist: deque = field(default_factory=lambda: deque(maxlen=7))
    _last_ball_xy: Optional[tuple[float, float]] = None
    _missing: int = 0
    _streak: int = 0
    _streak_start: int = 0
    _last_contact_y: Optional[float] = None
    _prev_vy: float = 0.0
    _completed: list[StreakEvent] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._y_hist = deque(maxlen=max(3, self.smooth_window))
        self._f_hist = deque(maxlen=max(3, self.smooth_window))
        self._ground_y = self.ground_y_frac * self.frame_height
        # Allow callers to pass None to mean "use defaults".
        if not self.valid_keypoints:
            self.valid_keypoints = set(VALID_DEFAULT)
        if not self.illegal_keypoints:
            self.illegal_keypoints = set(ILLEGAL_DEFAULT)

    # -----------------------------------------------------------------
    def update(
        self,
        frame_index: int,
        ball_xy: Optional[tuple[float, float]],
        keypoints: Optional[Keypoints],
        ground_y: Optional[float] = None,
    ) -> Optional[StreakEvent]:
        """Feed one frame. Returns a StreakEvent if a streak just ended.

        ``ground_y`` is the per-frame floor line in image-y (the tracked
        juggler's feet + margin). When None (no juggler detected this frame) the
        static ``ground_y_frac`` line is used as a fallback."""
        if ball_xy is None:
            self._missing += 1
            if self._missing >= self.lost_frames_reset and self._streak > 0:
                return self._end_streak("lost", frame_index)
            return None

        self._missing = 0
        self._last_ball_xy = ball_xy
        _, y = ball_xy
        self._y_hist.append(y)
        self._f_hist.append(frame_index)

        if len(self._y_hist) < self._y_hist.maxlen:
            return None

        ys = list(self._y_hist)
        smoothed = sum(ys) / len(ys)
        # Velocity from the smoothed signal endpoints.
        vy = ys[-1] - ys[-2]

        event: Optional[StreakEvent] = None
        # Local maximum in y (bottom of arc): velocity flips descending->ascending.
        if self._prev_vy > 0 and vy <= 0:
            contact_y = ys[-2]  # the turning-point sample
            if self._is_real_arc(contact_y):
                event = self._classify_contact(frame_index, contact_y,
                                               keypoints, ground_y)
            self._last_contact_y = contact_y
        self._prev_vy = vy
        return event

    # -----------------------------------------------------------------
    def _is_real_arc(self, contact_y: float) -> bool:
        """Reject micro-oscillations: require a real arc since the last contact."""
        if self._last_contact_y is None:
            return True
        # The peak height (min y) reached between contacts must clear min_arc_px.
        peak_y = min(self._y_hist)
        return (contact_y - peak_y) >= self.min_arc_px

    def _classify_contact(
        self,
        frame_index: int,
        contact_y: float,
        keypoints: Optional[Keypoints],
        ground_y: Optional[float] = None,
    ) -> Optional[StreakEvent]:
        # Ground touch? Prefer the dynamic per-frame ground (the tracked
        # juggler's feet + margin); fall back to the static line when no juggler
        # is available this frame.
        gy = ground_y if ground_y is not None else self._ground_y
        if contact_y >= gy:
            if self._streak > 0:
                return self._end_streak("ground", frame_index)
            return None

        if keypoints is None or self._last_ball_xy is None:
            # No pose this frame — can't classify; give benefit of the doubt only
            # if we're mid-streak (keep counting), else ignore.
            if self._streak > 0:
                self._streak += 1
            return None

        name, dist = nearest_keypoint(keypoints, (self._last_ball_xy[0], contact_y))
        if name is None or dist > self.contact_radius_px:
            # Ambiguous contact — don't count, don't reset.
            return None

        if name in self.illegal_keypoints:
            if self._streak > 0:
                return self._end_streak("hand", frame_index)
            return None

        if name in self.valid_keypoints or _is_footish(name):
            if self._streak == 0:
                self._streak_start = frame_index
            self._streak += 1
        return None

    def _end_streak(self, reason: str, frame_index: int) -> StreakEvent:
        ev = StreakEvent(
            count=self._streak,
            ended_reason=reason,
            start_frame=self._streak_start,
            end_frame=frame_index,
        )
        self._completed.append(ev)
        self._streak = 0
        self._last_contact_y = None
        return ev

    def flush(self, frame_index: int) -> Optional[StreakEvent]:
        """Call at end of clip to emit any streak still in progress."""
        if self._streak > 0:
            return self._end_streak("end", frame_index)
        return None

    @property
    def current_streak(self) -> int:
        return self._streak


def _is_footish(name: str) -> bool:
    # Ankles are the closest COCO keypoints to feet; hips/knees cover thigh contacts.
    return name in ("left_ankle", "right_ankle", "left_knee", "right_knee",
                    "left_hip", "right_hip")
