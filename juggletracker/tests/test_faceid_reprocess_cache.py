from __future__ import annotations

import os
import sys
import threading
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggletracker.db import Database  # noqa: E402
from juggletracker.ha_mqtt import HAPublisher  # noqa: E402


class FakeClient:
    def publish(self, topic, payload, retain=False):
        pass


def test_latest_faceid_label_is_persisted_for_reprocessing(tmp_path):
    db = Database(str(tmp_path / "juggle.db"))
    db.remember_faceid_label("event-1", "Artie")
    db.remember_faceid_label("event-1", "Art")

    assert db.faceid_label("event-1") == "Art"
    assert db.faceid_label("missing-event") is None
    db.close()


def test_reprocess_uses_saved_faceid_label(tmp_path):
    processed = tmp_path / "processed"
    inbox = tmp_path / "inbox"
    processed.mkdir()
    clip_name = "clip_front_yard_event-2.mp4"
    (processed / clip_name).write_bytes(b"clip")

    db_path = str(tmp_path / "juggle.db")
    db = Database(db_path)
    db.remember_faceid_label("event-2", "Art")
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
