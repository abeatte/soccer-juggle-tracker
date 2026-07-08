"""Detection & tracking.

Uses one Ultralytics detector for both **person** (COCO class 0) and
**sports ball** (COCO class 32). Person boxes are tracked across frames with
Ultralytics' built-in ByteTrack, giving stable ``track_id`` values while a
person is on screen. The ball is *not* tracked with an ID (there is normally one
ball in a one-kid-at-a-time scenario) — we just take the highest-confidence ball
detection each frame and hand its centroid to the juggle state machine.

All CPU-friendly knobs (thread count, image size, confidence) come from config.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

PERSON_CLASS = 0
BALL_CLASS = 32  # "sports ball" in COCO


@dataclass
class PersonBox:
    track_id: int
    xyxy: tuple[float, float, float, float]
    conf: float

    @property
    def centroid(self) -> tuple[float, float]:
        x0, y0, x1, y1 = self.xyxy
        return ((x0 + x1) / 2.0, (y0 + y1) / 2.0)


@dataclass
class BallDet:
    xy: tuple[float, float]   # centroid
    r: float                  # approx radius (px)
    conf: float


@dataclass
class FrameDetections:
    persons: list[PersonBox] = field(default_factory=list)
    ball: Optional[BallDet] = None


def _set_threads(n: int) -> None:
    try:
        import torch

        if n and n > 0:
            torch.set_num_threads(n)
        else:
            # Leave 2 cores for Home Assistant / Matter.
            cores = os.cpu_count() or 4
            torch.set_num_threads(max(1, cores - 2))
    except Exception:
        pass


class Detector:
    """Person+ball detector with per-person ByteTrack tracking."""

    def __init__(self, weights: str, person_conf: float, ball_conf: float,
                 torch_threads: int = 0):
        from ultralytics import YOLO

        _set_threads(torch_threads)
        self.model = YOLO(weights)
        self.person_conf = person_conf
        self.ball_conf = ball_conf
        # Lowest of the two thresholds so YOLO returns both classes; we filter after.
        self._infer_conf = min(person_conf, ball_conf)

    def detect_track(self, image: np.ndarray) -> FrameDetections:
        """Run detection+tracking on one frame (persist tracker state across calls)."""
        results = self.model.track(
            image,
            persist=True,
            classes=[PERSON_CLASS, BALL_CLASS],
            conf=self._infer_conf,
            tracker="bytetrack.yaml",
            verbose=False,
        )
        out = FrameDetections()
        if not results:
            return out
        r = results[0]
        if r.boxes is None:
            return out

        best_ball: Optional[BallDet] = None
        for box in r.boxes:
            cls = int(box.cls[0])
            conf = float(box.conf[0])
            xyxy = tuple(float(v) for v in box.xyxy[0].tolist())
            if cls == PERSON_CLASS and conf >= self.person_conf:
                tid = int(box.id[0]) if box.id is not None else -1
                out.persons.append(PersonBox(track_id=tid, xyxy=xyxy, conf=conf))
            elif cls == BALL_CLASS and conf >= self.ball_conf:
                x0, y0, x1, y1 = xyxy
                cand = BallDet(
                    xy=((x0 + x1) / 2.0, (y0 + y1) / 2.0),
                    r=max(x1 - x0, y1 - y0) / 2.0,
                    conf=conf,
                )
                if best_ball is None or cand.conf > best_ball.conf:
                    best_ball = cand
        out.ball = best_ball
        return out
