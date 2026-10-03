from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggletracker.cli import _process_inbox_clip  # noqa: E402


class FakePipeline:
    def __init__(self, annotate_processed: bool):
        self.ha = SimpleNamespace(annotate_processed=annotate_processed)
        self.calls: list[tuple[str, str]] = []

    def process_annotated_viewable(self, clip: str) -> str:
        self.calls.append(("annotated", clip))
        return "annotated result"

    def process(self, clip: str) -> str:
        self.calls.append(("normal", clip))
        return "normal result"


def test_global_annotation_routes_inbox_clip_to_annotated_processing():
    pipe = FakePipeline(annotate_processed=True)

    result = _process_inbox_clip(pipe, "/inbox/reprocessed.mp4", True)

    assert result == "annotated result"
    assert pipe.calls == [("annotated", "/inbox/reprocessed.mp4")]


def test_global_annotation_off_routes_inbox_clip_to_normal_processing():
    pipe = FakePipeline(annotate_processed=False)

    result = _process_inbox_clip(pipe, "/inbox/ordinary.mp4", False)

    assert result == "normal result"
    assert pipe.calls == [("normal", "/inbox/ordinary.mp4")]