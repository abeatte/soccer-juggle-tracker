"""Tests for the FaceID early-attribution race condition fix.

Covers the two-stage path that handles FaceID firing *before*
pipeline.process() has finished writing attempts for that session:

  Stage 1 (HAPublisher._on_faceid_event):
    reassign_session_by_event returns None  →  store in _pending_faceid

  Stage 2 (pipeline.process(), end of clip):
    consume_pending_faceid(event_id)  →  pop name, call reassign_session_by_event

Tests here use the DB directly + a minimal stub for consume_pending_faceid,
so they run with zero MQTT, zero ML, and no Pipeline instantiation.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggletracker.db import Database  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _db(tmp_path):
    return Database(os.path.join(str(tmp_path), "juggle.db"))


def _start_session(db, frigate_event_id=None):
    return db.start_session("clip.mp4", 25.0, frigate_event_id=frigate_event_id)


# ---------------------------------------------------------------------------
# Stage 1: reassign_session_by_event returns None before session exists
# ---------------------------------------------------------------------------

def test_reassign_returns_none_when_session_not_yet_created(tmp_path):
    """If FaceID fires before start_session() is called, reassign returns None."""
    db = _db(tmp_path)
    result = db.reassign_session_by_event("evt_abc123", "Kid1")
    assert result is None


def test_reassign_returns_none_when_session_exists_but_no_attempts(tmp_path):
    """Session row exists but no attempts committed yet — still returns None."""
    db = _db(tmp_path)
    _start_session(db, frigate_event_id="evt_abc123")
    result = db.reassign_session_by_event("evt_abc123", "Kid1")
    assert result is None


# ---------------------------------------------------------------------------
# Stage 2: pending name is applied after attempts are committed
# ---------------------------------------------------------------------------

def test_pending_attribution_applied_after_attempts_committed(tmp_path):
    """Simulate the full race: FaceID fires first (returns None, stored),
    then clip finishes and consume_pending_faceid applies it."""
    db = _db(tmp_path)
    event_id = "evt_race001"

    # --- Stage 1: FaceID fires; session doesn't exist yet ---
    early_result = db.reassign_session_by_event(event_id, "Kid1")
    assert early_result is None, "Should return None when session not yet created"

    # Simulate _pending_faceid storage (HAPublisher internal dict).
    pending: dict[str, str] = {}
    pending[event_id] = "Kid1"

    # --- Clip now processes: session + attempts committed ---
    sid = _start_session(db, frigate_event_id=event_id)
    uid = db.unknown_person_id
    db.record_attempt(sid, uid, track_id=1, count=17, ended_reason="end")
    db.record_attempt(sid, uid, track_id=1, count=5, ended_reason="ground")
    db.finish_session(sid, frames=500)

    # --- Stage 2: consume pending + apply ---
    # consume_pending_faceid equivalent
    person_name = pending.pop(event_id, None)
    assert person_name == "Kid1"
    assert len(pending) == 0, "Entry should be consumed"

    result = db.reassign_session_by_event(event_id, person_name)
    assert result is not None, "Should succeed now that attempts exist"
    assert result["person_name"] == "Kid1"
    assert result["moved_count"] == 2
    assert result["best_count"] == 17

    scores = db.high_scores()
    assert scores["Kid1"] == 17
    assert scores["Unknown Juggler"] == 0


def test_pending_attribution_fires_new_high_flag(tmp_path):
    """new_high is True when the moved score beats the person's existing record."""
    db = _db(tmp_path)
    event_id = "evt_newhigh"
    kid_id = db.add_person("Kid2")

    sid = _start_session(db, frigate_event_id=event_id)
    uid = db.unknown_person_id
    # Give Kid2 an existing record of 10 from a prior session.
    prior = _start_session(db)
    db.record_attempt(prior, kid_id, track_id=2, count=10, ended_reason="end")
    # This session's Unknown attempts will be reassigned; best is 22 > 10.
    db.record_attempt(sid, uid, track_id=1, count=22, ended_reason="end")
    db.finish_session(sid, frames=600)

    result = db.reassign_session_by_event(event_id, "Kid2")
    assert result is not None
    assert result["new_high"] is True
    assert result["best_count"] == 22
    assert db.high_scores()["Kid2"] == 22


def test_pending_attribution_no_new_high_when_below_existing(tmp_path):
    """new_high is False when the moved score does not beat the existing record."""
    db = _db(tmp_path)
    event_id = "evt_nonewhigh"
    kid_id = db.add_person("Kid3")

    prior = _start_session(db)
    db.record_attempt(prior, kid_id, track_id=2, count=30, ended_reason="end")
    sid = _start_session(db, frigate_event_id=event_id)
    uid = db.unknown_person_id
    db.record_attempt(sid, uid, track_id=1, count=15, ended_reason="end")
    db.finish_session(sid, frames=400)

    result = db.reassign_session_by_event(event_id, "Kid3")
    assert result is not None
    assert result["new_high"] is False
    assert db.high_scores()["Kid3"] == 30


def test_consume_pending_returns_none_for_unknown_event(tmp_path):
    """Consuming an event_id that was never stored returns None."""
    pending: dict[str, str] = {"evt_other": "SomeKid"}
    result = pending.pop("evt_notexist", None)
    assert result is None
    assert len(pending) == 1, "Unrelated entry should be untouched"


def test_consume_pending_idempotent_second_call(tmp_path):
    """A pending entry is consumed exactly once — second call returns None."""
    pending: dict[str, str] = {}
    event_id = "evt_once"
    pending[event_id] = "Kid1"

    first = pending.pop(event_id, None)
    second = pending.pop(event_id, None)
    assert first == "Kid1"
    assert second is None


def test_late_faceid_path_still_works(tmp_path):
    """If FaceID fires after the clip finishes (normal/late path), reassign
    succeeds directly with no pending dict involved."""
    db = _db(tmp_path)
    event_id = "evt_late"

    sid = _start_session(db, frigate_event_id=event_id)
    uid = db.unknown_person_id
    db.record_attempt(sid, uid, track_id=1, count=12, ended_reason="end")
    db.finish_session(sid, frames=300)

    # FaceID fires after finish — direct reassign, no pending dict needed.
    result = db.reassign_session_by_event(event_id, "Kid4")
    assert result is not None
    assert result["moved_count"] == 1
    assert result["best_count"] == 12
    assert db.high_scores()["Kid4"] == 12
    assert db.high_scores()["Unknown Juggler"] == 0


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
