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


def _median(vals) -> float:
    """Median of a small sequence (used to smooth the ball-y signal)."""
    s = sorted(vals)
    n = len(s)
    if n == 0:
        return 0.0
    m = n // 2
    return s[m] if n % 2 else 0.5 * (s[m - 1] + s[m])


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
    valid_keypoints: set[str] = field(default_factory=lambda: set(VALID_DEFAULT))
    illegal_keypoints: set[str] = field(default_factory=lambda: set(ILLEGAL_DEFAULT))
    lost_frames_reset: int = 15   # ball missing this many frames -> streak lost
    # Minimum frames between two counted contacts. A real juggle cycle lasts
    # well over a handful of frames at 25 fps, so anything closer is almost
    # certainly the same bounce detected twice (noise) -> reject it.
    min_contact_gap_frames: int = 6
    # When a contact frame has no fresh pose, reuse the last-seen keypoints if
    # they're at most this many frames old (pose runs every `person_stride`
    # frames). Beyond that the contact is left unclassified rather than blindly
    # counted.
    kp_staleness_frames: int = 6

    # ---- internal state ----
    _y_hist: deque = field(default_factory=lambda: deque(maxlen=7))
    _sm_hist: deque = field(default_factory=lambda: deque(maxlen=3))
    _last_ball_xy: Optional[tuple[float, float]] = None
    _missing: int = 0
    _streak: int = 0
    _streak_start: int = 0
    _last_contact_y: Optional[float] = None
    _since_contact_min: Optional[float] = None
    _last_contact_frame: int = -1000000
    _last_kps: Optional[Keypoints] = None
    _last_kps_frame: int = -1000000
    _med_w: int = 3
    _completed: list[StreakEvent] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Median-smoothing window over the raw ball-y signal: odd, >= 3, and no
        # larger than the configured smoothing window. A median filter (not a
        # mean) rejects single-frame ball-position spikes without smearing the
        # arc peaks that mark contacts.
        w = max(3, int(self.smooth_window))
        if w % 2 == 0:
            w -= 1
        self._med_w = w
        self._y_hist = deque(maxlen=max(w, 3))
        self._sm_hist = deque(maxlen=3)
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
        ground touch check is skipped for that frame."""
        # Remember the most recent real pose so a contact landing on a
        # pose-skipped frame can still be classified (within staleness).
        if keypoints is not None:
            self._last_kps = keypoints
            self._last_kps_frame = frame_index

        if ball_xy is None:
            self._missing += 1
            if self._missing >= self.lost_frames_reset and self._streak > 0:
                return self._end_streak("lost", frame_index)
            return None

        self._missing = 0
        self._last_ball_xy = ball_xy
        _, y = ball_xy
        self._y_hist.append(y)
        if len(self._y_hist) < self._med_w:
            return None

        # Median-smooth the recent raw y-signal, then measure velocity on the
        # SMOOTHED signal so a single noisy sample can't fake an arc bottom
        # (the old code computed `smoothed` but never used it — velocity ran on
        # raw samples, letting jitter create/hide contacts).
        sm = _median(list(self._y_hist)[-self._med_w:])
        self._sm_hist.append(sm)
        self._since_contact_min = (
            sm if self._since_contact_min is None
            else min(self._since_contact_min, sm))
        if len(self._sm_hist) < 3:
            return None

        prev_vy = self._sm_hist[-2] - self._sm_hist[-3]
        vy = self._sm_hist[-1] - self._sm_hist[-2]

        event: Optional[StreakEvent] = None
        # Local maximum in smoothed y (bottom of arc): descending -> ascending.
        if prev_vy > 0 and vy <= 0:
            contact_y = self._sm_hist[-2]  # the turning-point sample
            too_soon = (frame_index - self._last_contact_frame
                        < self.min_contact_gap_frames)
            if not too_soon and self._is_real_arc(contact_y):
                self._last_contact_frame = frame_index
                event = self._classify_contact(frame_index, contact_y,
                                               keypoints, ground_y)
                self._last_contact_y = contact_y
                self._since_contact_min = contact_y  # measure the next arc fresh
        return event

    # -----------------------------------------------------------------
    def _is_real_arc(self, contact_y: float) -> bool:
        """Reject micro-oscillations: require a real arc since the last contact."""
        if self._last_contact_y is None:
            return True
        # Peak height (min smoothed-y) reached since the last contact must clear
        # min_arc_px. Tracked incrementally in `_since_contact_min`.
        peak_y = (self._since_contact_min
                  if self._since_contact_min is not None else contact_y)
        return (contact_y - peak_y) >= self.min_arc_px

    def _classify_contact(
        self,
        frame_index: int,
        contact_y: float,
        keypoints: Optional[Keypoints],
        ground_y: Optional[float] = None,
    ) -> Optional[StreakEvent]:
        # Ground touch — only when we have the juggler's feet this frame.
        if ground_y is not None and contact_y >= ground_y:
            if self._streak > 0:
                return self._end_streak("ground", frame_index)
            return None

        # Use this frame's pose, or the most recent one if it's still fresh
        # (pose runs every `person_stride` frames, so contacts often land on a
        # pose-skipped frame).
        kps = keypoints
        if kps is None and self._last_kps is not None and (
                frame_index - self._last_kps_frame <= self.kp_staleness_frames):
            kps = self._last_kps

        if kps is None or self._last_ball_xy is None:
            # No pose to classify with — ambiguous. Don't count, don't reset.
            # (The old code blindly did `self._streak += 1` here, inflating
            # counts on every pose-skipped contact frame.)
            return None

        name, dist = nearest_keypoint(kps, (self._last_ball_xy[0], contact_y))
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
        self._since_contact_min = None
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
