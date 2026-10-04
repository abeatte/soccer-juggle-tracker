"""Tests for reprocess queue helpers (retired reassign entities, copy-to-inbox)."""
from __future__ import annotations

import os
import sys
import threading
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggletracker.db import Database  # noqa: E402
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


def test_reprocess_copies_clip_to_inbox(tmp_path):
    processed = tmp_path / "processed"
    inbox = tmp_path / "inbox"
    processed.mkdir()
    clip_name = "clip_front_yard_event-2.mp4"
    (processed / clip_name).write_bytes(b"clip")

    publisher = HAPublisher.__new__(HAPublisher)
    publisher.enabled = True
    publisher.client = FakeClient()
    publisher.node = "juggle_tracker"
    publisher.cfg = SimpleNamespace(
        capture={"processed_dir": str(processed), "inbox_dir": str(inbox)},
        database=SimpleNamespace(path=str(tmp_path / "juggle.db")),
    )
    publisher._reprocess_selected = clip_name
    publisher.annotate_processed = False
    publisher._worker = None
    publisher.publish_queues = lambda: None
    publisher.publish_last_processed_pending = lambda *_args, **_kwargs: None
    publisher._update_reprocess_preview = lambda: None

    publisher._do_reprocess()

    assert (inbox / clip_name).read_bytes() == b"clip"


def test_reprocess_does_not_overwrite_queued_clip(tmp_path):
    processed = tmp_path / "processed"
    inbox = tmp_path / "inbox"
    processed.mkdir()
    inbox.mkdir()
    clip_name = "clip_front_yard_event-2.mp4"
    (processed / clip_name).write_bytes(b"archived clip")
    queued_clip = inbox / clip_name
    queued_clip.write_bytes(b"clip currently being processed")

    publisher = HAPublisher.__new__(HAPublisher)
    publisher.enabled = True
    publisher.client = FakeClient()
    publisher.node = "juggle_tracker"
    publisher.cfg = SimpleNamespace(
        capture={"processed_dir": str(processed), "inbox_dir": str(inbox)},
        database=SimpleNamespace(path=str(tmp_path / "juggle.db")),
    )
    publisher._reprocess_selected = clip_name
    publisher.annotate_processed = False
    publisher._worker = None
    publisher.publish_queues = lambda: None
    publisher.publish_last_processed_pending = lambda *_args, **_kwargs: None
    publisher._update_reprocess_preview = lambda: None

    publisher._do_reprocess()

    assert queued_clip.read_bytes() == b"clip currently being processed"


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
