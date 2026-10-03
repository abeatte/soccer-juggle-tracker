"""Tests for the reprocess flow's prior-attribution seeding.

The faceid_labels table has been removed. On reprocess, _do_reprocess() now
derives the previously-attributed person from the attempts/people tables via
clip_attributions_from_path() and pre-loads _pending_faceid so the re-run
attributes to the right person without needing FaceID to re-fire.
"""
from __future__ import annotations

import os
import sys
import threading
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggletracker.db import Database, UNKNOWN_NAME  # noqa: E402
from juggletracker.ha_mqtt import HAPublisher  # noqa: E402


class FakeClient:
    def __init__(self):
        self.messages = []

    def publish(self, topic, payload, retain=False):
        self.messages.append((topic, payload, retain))


def test_retired_reassign_discovery_is_cleared():
    publisher = HAPublisher.__new__(HAPublisher)
    publisher.client = FakeClient()
    publisher.prefix = "homeassistant"
    publisher.node = "juggle_tracker"

    publisher._retire_reassign_entities()

    assert [topic for topic, payload, retain in publisher.client.messages
            if payload == "" and retain] == [
        "homeassistant/button/juggle_tracker/reassign/config",
        "homeassistant/select/juggle_tracker/reassign_source/config",
        "homeassistant/select/juggle_tracker/reassign_target/config",
        "juggle_tracker/reassign_source",
        "juggle_tracker/reassign_target",
    ]


def test_reprocess_seeds_pending_faceid_from_prior_attribution(tmp_path):
    """When a clip was previously attributed to a known person, _do_reprocess()
    should pre-load that person into _pending_faceid so the re-run applies the
    same attribution without needing FaceID to re-fire."""
    processed = tmp_path / "processed"
    inbox = tmp_path / "inbox"
    processed.mkdir()
    clip_name = "clip_front_yard_event-2.mp4"
    (processed / clip_name).write_bytes(b"clip")

    db_path = str(tmp_path / "juggle.db")
    db = Database(db_path)
    session_id = db.start_session(str(processed / clip_name), 25.0,
                                  frigate_event_id="event-2")
    art_id = db.add_person("Art")
    db.record_attempt(session_id, art_id, 1, 10, "end")
    db.close()

    publisher = HAPublisher.__new__(HAPublisher)
    publisher.enabled = True
    publisher.client = FakeClient()
    publisher.node = "juggle_tracker"
    publisher.cfg = SimpleNamespace(
        capture={"processed_dir": str(processed), "inbox_dir": str(inbox)},
        database=SimpleNamespace(path=db_path),
    )
    publisher._reprocess_selected = clip_name
    publisher.annotate_processed = False
    publisher._pending_faceid = {}
    publisher._worker = None
    publisher.publish_queues = lambda: None
    publisher.publish_reprocess_pending = lambda *_args, **_kwargs: None
    publisher._update_reprocess_preview = lambda: None

    publisher._do_reprocess()

    assert (inbox / clip_name).read_bytes() == b"clip"
    assert publisher._pending_faceid["event-2"] == "Art"


def test_reprocess_skips_pending_when_only_unknown(tmp_path):
    """If the prior run only has Unknown Juggler attempts (never attributed),
    _do_reprocess() should leave _pending_faceid empty — the Frigate API
    fallback in _apply_faceid_attribution will query for a label at end-of-clip."""
    processed = tmp_path / "processed"
    inbox = tmp_path / "inbox"
    processed.mkdir()
    clip_name = "clip_front_yard_event-5.mp4"
    (processed / clip_name).write_bytes(b"clip")

    db_path = str(tmp_path / "juggle.db")
    db = Database(db_path)
    session_id = db.start_session(str(processed / clip_name), 25.0,
                                  frigate_event_id="event-5")
    db.record_attempt(session_id, db.unknown_person_id, 1, 6, "end")
    db.close()

    publisher = HAPublisher.__new__(HAPublisher)
    publisher.enabled = True
    publisher.client = FakeClient()
    publisher.node = "juggle_tracker"
    publisher.cfg = SimpleNamespace(
        capture={"processed_dir": str(processed), "inbox_dir": str(inbox)},
        database=SimpleNamespace(path=db_path),
    )
    publisher._reprocess_selected = clip_name
    publisher.annotate_processed = False
    publisher._pending_faceid = {}
    publisher._worker = None
    publisher.publish_queues = lambda: None
    publisher.publish_reprocess_pending = lambda *_args, **_kwargs: None
    publisher._update_reprocess_preview = lambda: None

    publisher._do_reprocess()

    assert (inbox / clip_name).read_bytes() == b"clip"
    # Nothing seeded — Frigate API will handle attribution at end-of-clip.
    assert publisher._pending_faceid == {}


def test_queue_refresh_uses_a_thread_owned_sqlite_connection(tmp_path):
    db_path = str(tmp_path / "juggle.db")
    db = Database(db_path)
    session_id = db.start_session("/processed/clip_camera_event-3.mp4", 25.0)
    art_id = db.add_person("Art")
    db.record_attempt(session_id, art_id, 1, 7, "end")

    processed = tmp_path / "processed"
    inbox = tmp_path / "inbox"
    processed.mkdir()
    inbox.mkdir()
    (processed / "clip_camera_event-3.mp4").touch()

    publisher = HAPublisher.__new__(HAPublisher)
    publisher.enabled = True
    publisher.client = FakeClient()
    publisher.node = "juggle_tracker"
    publisher.cfg = SimpleNamespace(
        capture={"processed_dir": str(processed), "inbox_dir": str(inbox)},
        database=SimpleNamespace(path=db_path),
    )
    publisher._worker = SimpleNamespace(db=db)
    publisher._last_select_options = None
    publisher._last_inbox_options = None
    publisher._reprocess_selected = None
    publisher._update_reprocess_preview = lambda: None
    publisher._announce_reprocess_select = lambda _options: None
    publisher._announce_inbox_select = lambda _options: None

    errors = []

    def refresh():
        try:
            publisher.publish_queues()
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=refresh)
    thread.start()
    thread.join()

    assert errors == []
    assert publisher._reprocess_filename_to_option[
        "clip_camera_event-3.mp4"
    ] == "Art | clip_camera_event-3.mp4"
    db.close()
