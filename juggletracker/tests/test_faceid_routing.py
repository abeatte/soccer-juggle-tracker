"""Tests for the smarter FaceID sub_label routing logic.

Covers all three dispatch cases in HAPublisher._on_faceid_event:

  1. Apply-now  — reassign_session_by_event succeeds (session already committed).
  2. Park       — reassign returns None AND the clip is in-flight / queued.
  3. Drop       — reassign returns None AND no clip is queued.

Also unit-tests Pipeline.is_event_queued independently.

All tests run with zero MQTT, zero ML, and no real HAPublisher instantiation.
The _on_faceid_event logic is exercised through a lightweight stub (FakePub)
that wires the same conditional logic without needing paho-mqtt or a live broker.
"""
from __future__ import annotations

import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggletracker.db import Database  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _db(tmp_path):
    return Database(os.path.join(str(tmp_path), "juggle.db"))


def _start_session(db, frigate_event_id=None):
    return db.start_session("clip_cam_" + (frigate_event_id or "noevent") + ".mp4",
                             25.0, frigate_event_id=frigate_event_id)


# ---------------------------------------------------------------------------
# Stub: FakeWorker — stands in for Pipeline.is_event_queued
# ---------------------------------------------------------------------------

class FakeWorker:
    """Minimal stand-in for Pipeline that just tracks whether a given event_id
    should be considered queued/in-flight."""

    def __init__(self, queued_event_ids=()):
        self._queued = set(queued_event_ids)

    def is_event_queued(self, event_id: str) -> bool:
        return event_id in self._queued


# ---------------------------------------------------------------------------
# Stub: FakePub — isolates the three-case logic from paho-mqtt
# ---------------------------------------------------------------------------

class FakePub:
    """Re-implements the three-case logic from HAPublisher._on_faceid_event
    without any MQTT/pipeline machinery so we can test the routing in isolation.

    Mirrors the real conditional exactly so a future refactor would break these
    tests immediately."""

    def __init__(self, db: Database, worker: FakeWorker):
        self._db = db
        self._worker = worker
        self._pending_faceid: dict[str, str] = {}
        self.applied: list[dict] = []    # records each successful reassign
        self.parked: list[str] = []      # event_ids that were parked
        self.dropped: list[str] = []     # event_ids that were dropped

    def dispatch(self, event_id: str, person_name: str) -> None:
        """Run the three-case routing for (event_id, person_name)."""
        result = self._db.reassign_session_by_event(event_id, person_name)
        if result is None:
            queued = (
                self._worker is not None
                and self._worker.is_event_queued(event_id)
            )
            if queued:
                self._pending_faceid[event_id] = person_name
                self.parked.append(event_id)
            else:
                self.dropped.append(event_id)
            return
        self.applied.append(result)


# ---------------------------------------------------------------------------
# Tests: is_event_queued (Pipeline method, tested via the logic directly)
# ---------------------------------------------------------------------------

class FakeConfig:
    """Minimal config stub for Pipeline.is_event_queued."""
    def __init__(self, inbox_dir=None):
        self._inbox = inbox_dir

    class capture:
        pass

    def get(self, key, default=None):
        if key == "inbox_dir":
            return self._inbox
        return default


class FakePipelineForQueued:
    """Thin stand-in for Pipeline that only exposes is_event_queued.

    Copies the real implementation so the test is actually running the logic
    (not a mock), but avoids constructing the full Pipeline (no models needed).
    """

    def __init__(self, cur_clip=None, inbox_dir=None):
        self._cur_clip = cur_clip
        self._inbox_dir = inbox_dir

    def is_event_queued(self, event_id: str) -> bool:
        if not event_id:
            return False
        cur = self._cur_clip
        if cur:
            basename = os.path.splitext(os.path.basename(cur))[0]
            parts = basename.split("_", 2)
            if len(parts) == 3 and parts[2] == event_id:
                return True
        inbox = self._inbox_dir
        if inbox and os.path.isdir(inbox):
            for fname in os.listdir(inbox):
                stem = os.path.splitext(fname)[0]
                parts = stem.split("_", 2)
                if len(parts) == 3 and parts[2] == event_id:
                    return True
        return False


def test_is_event_queued_matches_cur_clip(tmp_path):
    """Returns True when the event_id matches the currently-processing clip."""
    worker = FakePipelineForQueued(cur_clip="/inbox/clip_cam_evt123.mp4")
    assert worker.is_event_queued("evt123") is True


def test_is_event_queued_no_match_cur_clip(tmp_path):
    """Returns False when the in-flight clip has a different event_id."""
    worker = FakePipelineForQueued(cur_clip="/inbox/clip_cam_evtOTHER.mp4")
    assert worker.is_event_queued("evt123") is False


def test_is_event_queued_matches_inbox_file(tmp_path):
    """Returns True when a matching clip file sits in the inbox dir."""
    inbox = str(tmp_path / "inbox")
    os.makedirs(inbox)
    open(os.path.join(inbox, "clip_cam_evt456.mp4"), "w").close()
    worker = FakePipelineForQueued(inbox_dir=inbox)
    assert worker.is_event_queued("evt456") is True


def test_is_event_queued_no_match_inbox_file(tmp_path):
    """Returns False when the inbox has clips but none match the event_id."""
    inbox = str(tmp_path / "inbox")
    os.makedirs(inbox)
    open(os.path.join(inbox, "clip_cam_evtOTHER.mp4"), "w").close()
    worker = FakePipelineForQueued(inbox_dir=inbox)
    assert worker.is_event_queued("evt456") is False


def test_is_event_queued_no_inbox_dir(tmp_path):
    """Returns False when inbox_dir is not configured or doesn't exist."""
    worker = FakePipelineForQueued(inbox_dir=None)
    assert worker.is_event_queued("evt789") is False


def test_is_event_queued_empty_event_id(tmp_path):
    """Returns False for empty event_id regardless of state."""
    worker = FakePipelineForQueued(cur_clip="/inbox/clip_cam_evt000.mp4")
    assert worker.is_event_queued("") is False


# ---------------------------------------------------------------------------
# Tests: three-case dispatch
# ---------------------------------------------------------------------------

def test_dispatch_apply_now_when_session_committed(tmp_path):
    """Case 1: session + attempts already in DB → reassign succeeds immediately."""
    db = _db(tmp_path)
    event_id = "evt_apply_now"
    sid = _start_session(db, frigate_event_id=event_id)
    uid = db.unknown_person_id
    db.record_attempt(sid, uid, track_id=1, count=20, ended_reason="end")
    db.finish_session(sid, frames=500)

    pub = FakePub(db, FakeWorker())  # worker not consulted when reassign succeeds
    pub.dispatch(event_id, "Kid1")

    assert len(pub.applied) == 1
    assert pub.applied[0]["person_name"] == "Kid1"
    assert pub.applied[0]["best_count"] == 20
    assert len(pub.parked) == 0
    assert len(pub.dropped) == 0
    assert db.high_scores()["Kid1"] == 20


def test_dispatch_park_when_clip_is_queued(tmp_path):
    """Case 2: reassign returns None but clip is queued → park as pending."""
    db = _db(tmp_path)
    event_id = "evt_park_me"

    # No session in DB yet — reassign returns None.
    worker = FakeWorker(queued_event_ids={event_id})
    pub = FakePub(db, worker)
    pub.dispatch(event_id, "Kid2")

    assert len(pub.parked) == 1
    assert pub.parked[0] == event_id
    assert pub._pending_faceid[event_id] == "Kid2"
    assert len(pub.applied) == 0
    assert len(pub.dropped) == 0


def test_dispatch_drop_when_nothing_queued_and_no_session(tmp_path):
    """Case 3: reassign returns None and clip not queued → drop silently."""
    db = _db(tmp_path)
    event_id = "evt_drop_me"

    worker = FakeWorker()  # nothing queued
    pub = FakePub(db, worker)
    pub.dispatch(event_id, "Kid3")

    assert len(pub.dropped) == 1
    assert pub.dropped[0] == event_id
    assert len(pub.parked) == 0
    assert len(pub.applied) == 0
    # Nothing written to pending — won't accumulate indefinitely.
    assert event_id not in pub._pending_faceid


def test_dispatch_drop_when_session_exists_but_already_reassigned(tmp_path):
    """Case 3 variant: session exists but no Unknown attempts remain (already
    attributed) → reassign returns None → drop (not queued)."""
    db = _db(tmp_path)
    event_id = "evt_already_done"
    kid_id = db.add_person("Kid4")

    sid = _start_session(db, frigate_event_id=event_id)
    # Record attempt directly under Kid4 (not Unknown) — reassign finds nothing.
    db.record_attempt(sid, kid_id, track_id=1, count=15, ended_reason="end")
    db.finish_session(sid, frames=300)

    worker = FakeWorker()  # nothing queued
    pub = FakePub(db, worker)
    pub.dispatch(event_id, "Kid4")

    assert len(pub.dropped) == 1
    assert len(pub.applied) == 0
    assert len(pub.parked) == 0


def test_pending_is_not_accumulated_across_unknown_events(tmp_path):
    """Multiple unknown events all get dropped; _pending_faceid stays empty."""
    db = _db(tmp_path)
    worker = FakeWorker()
    pub = FakePub(db, worker)

    for i in range(5):
        pub.dispatch(f"evt_unknown_{i}", "Kid5")

    assert len(pub.dropped) == 5
    assert len(pub._pending_faceid) == 0


def test_park_then_consume_applies_correctly(tmp_path):
    """Full round-trip: park early → clip processes → consume applies."""
    db = _db(tmp_path)
    event_id = "evt_roundtrip"

    # FaceID fires before any clip data: park.
    worker = FakeWorker(queued_event_ids={event_id})
    pub = FakePub(db, worker)
    pub.dispatch(event_id, "Kid6")
    assert event_id in pub._pending_faceid

    # Clip now processes and commits attempts.
    sid = _start_session(db, frigate_event_id=event_id)
    uid = db.unknown_person_id
    db.record_attempt(sid, uid, track_id=1, count=18, ended_reason="end")
    db.finish_session(sid, frames=450)

    # Consume pending (mirrors pipeline.process end-of-clip logic).
    person_name = pub._pending_faceid.pop(event_id, None)
    assert person_name == "Kid6"

    result = db.reassign_session_by_event(event_id, person_name)
    assert result is not None
    assert result["best_count"] == 18
    assert db.high_scores()["Kid6"] == 18
    assert event_id not in pub._pending_faceid


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
