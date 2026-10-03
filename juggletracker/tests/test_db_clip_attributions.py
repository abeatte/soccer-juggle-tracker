"""Tests for processed-clip attribution labels used by the reprocess selector."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggletracker.db import Database  # noqa: E402


def _db(tmp_path):
    return Database(os.path.join(str(tmp_path), "juggle.db"))


def test_clip_attributions_uses_latest_session_for_basename(tmp_path):
    db = _db(tmp_path)
    unknown = db.unknown_person_id
    artie = db.add_person("Artie")
    old_session = db.start_session("/inbox/clip_cam_event.mp4", 25.0)
    db.record_attempt(old_session, unknown, 1, 12, "end")
    latest_session = db.start_session("/other/clip_cam_event.mp4", 25.0)
    db.record_attempt(latest_session, artie, 2, 18, "ground")

    assert db.clip_attributions(["clip_cam_event.mp4", "not_processed.mp4"]) == {
        "clip_cam_event.mp4": ["Artie"]
    }


def test_clip_attributions_reports_session_with_no_attempts(tmp_path):
    db = _db(tmp_path)
    db.start_session("/processed/quiet_clip.mp4", 25.0)

    assert db.clip_attributions(["quiet_clip.mp4"]) == {"quiet_clip.mp4": []}