"""Pose estimation — 17 COCO keypoints per person.

Wraps a YOLO-pose model. Returns, per detected person, a dict of
keypoint-name -> (x, y, confidence). The juggle classifier uses these to decide
whether a ball contact landed on a valid body part (foot/knee/head/shoulder) or
an illegal one (hand/wrist/elbow).
"""
from __future__ import annotations

from typing import Optional

import numpy as np

# COCO-17 keypoint ordering used by YOLO-pose.
COCO_KEYPOINTS = [
    "nose", "left_eye", "right_eye", "left_ear", "right_ear",
    "left_shoulder", "right_shoulder", "left_elbow", "right_elbow",
    "left_wrist", "right_wrist", "left_hip", "right_hip",
    "left_knee", "right_knee", "left_ankle", "right_ankle",
]

Keypoints = dict[str, tuple[float, float, float]]  # name -> (x, y, conf)


class PoseEstimator:
    def __init__(self, weights: str, conf: float = 0.3):
        from ultralytics import YOLO

        self.model = YOLO(weights)
        self.conf = conf

    def estimate(self, image: np.ndarray) -> list[Keypoints]:
        """Return a list of keypoint dicts, one per detected person."""
        results = self.model(image, conf=self.conf, verbose=False)
        people: list[Keypoints] = []
        if not results:
            return people
        r = results[0]
        if r.keypoints is None or r.keypoints.data is None:
            return people
        data = r.keypoints.data.cpu().numpy()  # [N, 17, 3]
        for person in data:
            kp: Keypoints = {}
            for i, name in enumerate(COCO_KEYPOINTS):
                x, y, c = person[i]
                kp[name] = (float(x), float(y), float(c))
            people.append(kp)
        return people


def nearest_keypoint(
    kps: Keypoints,
    point: tuple[float, float],
    min_conf: float = 0.2,
) -> tuple[Optional[str], float]:
    """Return (keypoint_name, distance_px) closest to ``point`` among confident kps."""
    px, py = point
    best_name: Optional[str] = None
    best_dist = float("inf")
    for name, (x, y, c) in kps.items():
        if c < min_conf:
            continue
        d = ((x - px) ** 2 + (y - py) ** 2) ** 0.5
        if d < best_dist:
            best_dist = d
            best_name = name
    return best_name, best_dist


def person_center(kps: Keypoints, min_conf: float = 0.2) -> Optional[tuple[float, float]]:
    """Rough torso center from shoulders/hips, for matching pose to a track box."""
    pts = [
        (x, y)
        for n, (x, y, c) in kps.items()
        if c >= min_conf and n in ("left_shoulder", "right_shoulder", "left_hip", "right_hip")
    ]
    if not pts:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (sum(xs) / len(xs), sum(ys) / len(ys))
