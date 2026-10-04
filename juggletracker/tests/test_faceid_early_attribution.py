"""Tests for reassigning Unknown attempts after a clip finishes."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggletracker.db import Database  # noqa: E402


def _db(tmp_path):
    return Database(os.path.join(str(tmp_path), "juggle.db"))


def _start_session(db, frigate_event_id=None):
    return db.start_session("clip.mp4", 25.0, frigate_event_id=frigate_event_id)


def test_reassign_returns_none_when_session_not_yet_created(tmp_path):
    db = _db(tmp_path)
    result = db.reassign_session_by_event("evt_abc123", "Kid1")
    assert result is None


def test_reassign_returns_none_when_session_exists_but_no_attempts(tmp_path):
    db = _db(tmp_path)
    _start_session(db, frigate_event_id="evt_abc123")
    result = db.reassign_session_by_event("evt_abc123", "Kid1")
    assert result is None


def test_reassign_after_attempts_committed(tmp_path):
    db = _db(tmp_path)
    event_id = "evt_race001"
    sid = _start_session(db, frigate_event_id=event_id)
    uid = db.unknown_person_id
    db.record_attempt(sid, uid, track_id=1, count=17, ended_reason="end")
    db.record_attempt(sid, uid, track_id=1, count=5, ended_reason="ground")
    db.finish_session(sid, frames=500)

    result = db.reassign_session_by_event(event_id, "Kid1")
    assert result is not None
    assert result["person_name"] == "Kid1"
    assert result["moved_count"] == 2
    assert result["best_count"] == 17

    scores = db.high_scores()
    assert scores["Kid1"] == 17
    assert scores["Unknown Juggler"] == 0


def test_reassign_fires_new_high_flag(tmp_path):
    db = _db(tmp_path)
    event_id = "evt_newhigh"
    kid_id = db.add_person("Kid2")

    sid = _start_session(db, frigate_event_id=event_id)
    uid = db.unknown_person_id
    prior = _start_session(db)
    db.record_attempt(prior, kid_id, track_id=2, count=10, ended_reason="end")
    db.record_attempt(sid, uid, track_id=1, count=22, ended_reason="end")
    db.finish_session(sid, frames=600)

    result = db.reassign_session_by_event(event_id, "Kid2")
    assert result is not None
    assert result["new_high"] is True
    assert result["best_count"] == 22
    assert db.high_scores()["Kid2"] == 22


def test_reassign_no_new_high_when_below_existing(tmp_path):
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
