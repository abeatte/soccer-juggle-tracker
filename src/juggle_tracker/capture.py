"""Capture: record clips from the camera and iterate frames from a clip file.

Two responsibilities:

1. ``record_clip`` — grab N seconds from the RTSP main stream to an mp4. Used
   either manually or by a motion trigger (see docs/HOME_ASSISTANT.md). Uses
   ffmpeg via a stream-copy (no re-encode) so it's cheap on the CPU and keeps
   the camera's full framerate/quality.

2. ``FrameSource`` — yields frames from a clip, applying the ROI crop and
   downscale used for analysis. Yields the analysis frame plus the scale factor
   so detections can be mapped back if needed.
"""
from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from typing import Iterator, Optional

import cv2
import numpy as np


def record_clip(rtsp_url: str, out_path: str, seconds: int) -> str:
    """Record ``seconds`` from an RTSP stream to ``out_path`` via ffmpeg copy."""
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-rtsp_transport", "tcp",
        "-i", rtsp_url,
        "-t", str(seconds),
        "-c", "copy",
        "-an",
        out_path,
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return out_path


@dataclass
class Frame:
    index: int          # frame number in the clip
    t: float            # timestamp (s) within the clip
    image: np.ndarray   # analysis image (cropped + downscaled, BGR)
    roi_offset: tuple[int, int]   # (x0, y0) crop offset in the downscaled full frame
    scale: float        # analysis_px / source_px


class FrameSource:
    """Iterate a clip's frames as analysis-ready images."""

    def __init__(
        self,
        clip_path: str,
        roi: list[float],
        infer_long_edge: int,
    ):
        self.clip_path = clip_path
        self.roi = roi
        self.infer_long_edge = infer_long_edge
        self.cap = cv2.VideoCapture(clip_path)
        if not self.cap.isOpened():
            raise FileNotFoundError(f"Cannot open clip: {clip_path}")
        self.fps = self.cap.get(cv2.CAP_PROP_FPS) or 25.0
        self.width = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.frame_count = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT))

    def _prep(self, img: np.ndarray) -> tuple[np.ndarray, tuple[int, int], float]:
        h, w = img.shape[:2]
        # Downscale full frame so long edge == infer_long_edge.
        scale = self.infer_long_edge / float(max(h, w))
        scale = min(scale, 1.0)
        if scale < 1.0:
            img = cv2.resize(img, (int(w * scale), int(h * scale)))
        dh, dw = img.shape[:2]
        # Apply ROI crop (fractions of the downscaled frame).
        x0 = int(self.roi[0] * dw)
        y0 = int(self.roi[1] * dh)
        x1 = int(self.roi[2] * dw)
        y1 = int(self.roi[3] * dh)
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(dw, x1), min(dh, y1)
        crop = img[y0:y1, x0:x1]
        return crop, (x0, y0), scale

    def __iter__(self) -> Iterator[Frame]:
        idx = 0
        while True:
            ok, img = self.cap.read()
            if not ok:
                break
            crop, offset, scale = self._prep(img)
            yield Frame(
                index=idx,
                t=idx / self.fps,
                image=crop,
                roi_offset=offset,
                scale=scale,
            )
            idx += 1
        self.cap.release()

    def release(self) -> None:
        if self.cap.isOpened():
            self.cap.release()
