"""Tests for reassigning a person's current high score to another person.

Covers the pure DB logic in ``db.reassign_current_high`` — no MQTT, no ML, no
filesystem. Run: python -m pytest tests/  (or) python tests/test_db_reassign.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggletracker.db import Database, reassign_current_high  # noqa: E402


def _db(tmp_path):
    return Database(os.path.join(str(tmp_path), "juggle.db"))


def _session(db):
    return db.start_session("clip.mp4", 25.0)


def test_reassign_moves_best_attempt_and_recomputes_both(tmp_path):
    """Unknown's 23 (really the kid's) moves to Artie and becomes Artie's best;
    Unknown falls back to its next-best remaining attempt."""
    db = _db(tmp_path)
    unknown = db.unknown_person_id
    artie = db.add_person("Artie")
    sid = _session(db)
    # Unknown has a misattributed 23 plus a genuine 12 (a real unknown kid).
    db.record_attempt(sid, unknown, 1, 23, "end")
    db.record_attempt(sid, unknown, 2, 12, "ground")
    db.record_attempt(sid, artie, 3, 10, "hand")
    assert db.high_scores()["Unknown Juggler"] == 23
    assert db.high_scores()["Artie"] == 10

    res = reassign_current_high(db.conn, unknown, artie)
    db.conn.commit()

    assert res is not None
    assert res["moved_count"] == 23
    assert res["tgt_old_high"] == 10
    assert res["tgt_new_high"] == 23
    assert res["src_new_high"] == 12  # Unknown's next-best remains
    scores = db.high_scores()
    assert scores["Artie"] == 23
    assert scores["Unknown Juggler"] == 12
    # The moved attempt now belongs to Artie.
    row = db.conn.execute(
        "SELECT person_id FROM attempts WHERE count = 23").fetchone()
    assert int(row["person_id"]) == artie


def test_reassign_when_moved_score_does_not_beat_target(tmp_path):
    """Moving a 15 to someone who already has 20 leaves the target's best (20)
    intact but still credits the attempt and drops the source's high score."""
    db = _db(tmp_path)
    unknown = db.unknown_person_id
    art = db.add_person("Art")
    sid = _session(db)
    db.record_attempt(sid, unknown, 1, 15, "end")
    db.record_attempt(sid, art, 2, 20, "end")

    res = reassign_current_high(db.conn, unknown, art)
    db.conn.commit()

    assert res["moved_count"] == 15
    assert res["tgt_old_high"] == 20
    assert res["tgt_new_high"] == 20
    assert res["src_new_high"] == 0  # Unknown had only the one attempt
    assert db.high_scores()["Art"] == 20
    assert db.high_scores()["Unknown Juggler"] == 0


def test_reassign_source_with_no_attempts_returns_none(tmp_path):
    db = _db(tmp_path)
    empty = db.add_person("Karen")
    other = db.add_person("Jessica")
    assert reassign_current_high(db.conn, empty, other) is None


def test_reassign_unknown_person_id_returns_none(tmp_path):
    db = _db(tmp_path)
    real = db.add_person("Artie")
    assert reassign_current_high(db.conn, real, 999999) is None
    assert reassign_current_high(db.conn, 999999, real) is None


def test_reassign_ties_break_to_most_recent_clip(tmp_path):
    """When the source has two attempts tied at its max, the most recent one is
    the one moved (its clip is the freshest)."""
    db = _db(tmp_path)
    unknown = db.unknown_person_id
    artie = db.add_person("Artie")
    sid = _session(db)
    db.record_attempt(sid, unknown, 1, 18, "end")
    db.record_attempt(sid, unknown, 2, 18, "end")  # later row, same count
    later = db.conn.execute(
        "SELECT MAX(id) AS mx FROM attempts WHERE person_id = ?",
        (unknown,)).fetchone()["mx"]

    res = reassign_current_high(db.conn, unknown, artie)
    db.conn.commit()

    assert res["moved_attempt_id"] == int(later)
    assert res["moved_count"] == 18
    # Unknown still has the other 18.
    assert db.high_scores()["Unknown Juggler"] == 18
    assert db.high_scores()["Artie"] == 18


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
