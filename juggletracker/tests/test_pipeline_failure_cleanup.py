from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import juggletracker.pipeline as pipeline_module  # noqa: E402
from juggletracker.db import Database  # noqa: E402
from juggletracker.pipeline import ClipCancelled, Pipeline  # noqa: E402


class FakeFrameSource:
    fps = 25.0
    frame_count = 2

    def __init__(self):
        self.released = False

    def __iter__(self):
        image = SimpleNamespace(shape=(2, 2, 3))
        for index in range(2):
            yield SimpleNamespace(index=index, image=image)

    def release(self):
        self.released = True


class FakeCounter:
    def __init__(self):
        self.calls = 0

    def update(self, *_args, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            return SimpleNamespace(
                count=9,
                ended_reason="end",
                start_frame=0,
                end_frame=0,
                left_count=0,
                right_count=0,
                header_count=0,
            )
        return None

    def flush(self, _frame_count):
        return None


class FakePublisher:
    def __init__(self):
        self.statuses = []
        self.synced_people = None

    def publish_status(self, status, **_kwargs):
        self.statuses.append(status)

    def publish_queues(self):
        pass

    def sync_all(self, people):
        self.synced_people = people


@pytest.mark.parametrize("cancel", [False, True])
def test_processing_failure_rolls_back_partial_session(tmp_path, monkeypatch,
                                                       cancel):
    source = FakeFrameSource()
    monkeypatch.setattr(
        pipeline_module, "FrameSource", lambda *_args, **_kwargs: source
    )
    db = Database(str(tmp_path / "juggle.db"))
    publisher = FakePublisher()
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.cfg = SimpleNamespace(
        roi=[], processing={"infer_long_edge": 10, "person_stride": 1}
    )
    pipeline.db = db
    pipeline.ha = publisher
    pipeline.detector = SimpleNamespace(
        detect_track=lambda _image: SimpleNamespace(ball=None, persons=[])
    )
    pipeline.ball_fallback = SimpleNamespace(enabled=False)
    pipeline.pose = SimpleNamespace(estimate=lambda _image: [])
    pipeline.thermal = SimpleNamespace(maybe_wait=lambda: None)
    pipeline._ground_margin = 10.0
    pipeline._cur_clip = None
    pipeline._cancel_clip = None
    pipeline._cur_total = 0
    pipeline._cur_frame = 0
    pipeline._cur_pct = 0.0
    pipeline._new_ball_tracker = lambda: SimpleNamespace(
        last_xy=None, update=lambda *_args: (None, False)
    )
    pipeline._new_counter = lambda _height: FakeCounter()
    detect_count = 0

    def detect_track(_image):
        nonlocal detect_count
        detect_count += 1
        if detect_count == 2 and not cancel:
            raise RuntimeError("detector failed")
        return SimpleNamespace(ball=None, persons=[])

    pipeline.detector.detect_track = detect_track

    clip_path = "clip_front_yard_event-1.mp4"
    if cancel:
        def request_cancel(*_args, **_kwargs):
            pipeline._cancel_clip = os.path.basename(clip_path)

        error_type = ClipCancelled
        trace_sink = request_cancel
    else:
        error_type = RuntimeError
        trace_sink = None

    with pytest.raises(error_type):
        pipeline.process(clip_path, trace_sink=trace_sink)

    assert db.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0
    assert db.high_scores()["Unknown Juggler"] == 0
    assert source.released
    assert pipeline._cur_clip is None
    assert pipeline._cancel_clip is None
    assert publisher.statuses[-1] == "idle"
    db.close()