from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import cv2
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggletracker.pipeline import Pipeline  # noqa: E402


class FakeWriter:
    def __init__(self):
        self.frames = []

    def write(self, image):
        self.frames.append(image)


def test_overlay_includes_registered_sound_count(monkeypatch):
    labels = []
    monkeypatch.setattr(
        cv2, "putText", lambda image, text, *_args, **_kwargs: labels.append(text) or image
    )
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.ball_fallback = SimpleNamespace(enabled=False)
    counter = SimpleNamespace(
        unposed_contact_age=100,
        current_streak=2,
        current_left_count=1,
        current_right_count=1,
        current_header_count=0,
    )
    writer = FakeWriter()

    pipeline._draw(
        np.zeros((180, 320, 3), dtype=np.uint8),
        SimpleNamespace(persons=[], ball=None),
        ball_xy=None,
        ball_bridged=False,
        kps=None,
        counter=counter,
        active_track=None,
        writer=writer,
        path="unused.mp4",
        sound_count=3,
    )

    assert "Sounds: 3" in labels
    assert len(writer.frames) == 1