from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import juggletracker.ha_mqtt as ha_mqtt  # noqa: E402
from juggletracker.db import Database  # noqa: E402
from juggletracker.ha_mqtt import HAPublisher  # noqa: E402
from juggletracker.pipeline import _apply_faceid_attribution  # noqa: E402


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return self.payload


class FakeHA:
    def __init__(self, api_label=None, pending_label=None):
        self.api_label = api_label
        self.pending_label = pending_label
        self.lookup_count = 0

    def consume_pending_faceid(self, _event_id):
        return self.pending_label

    def lookup_frigate_sub_label(self, _event_id):
        self.lookup_count += 1
        return self.api_label


def test_lookup_frigate_sub_label_reads_event_api(monkeypatch):
    publisher = HAPublisher.__new__(HAPublisher)
    publisher._faceid_enabled = True
    publisher.cfg = SimpleNamespace(home_assistant={
        "frigate_api_url": "http://frigate.local:5000/",
    })
    requests = []

    def fake_urlopen(request, timeout):
        requests.append((request.full_url, timeout))
        return FakeResponse({"id": "event-1", "sub_label": "Artie"})

    monkeypatch.setattr(ha_mqtt, "urlopen", fake_urlopen)

    assert publisher.lookup_frigate_sub_label("event-1") == "Artie"
    assert requests == [
        ("http://frigate.local:5000/api/events/event-1", 4)
    ]


def test_faceid_handler_ignores_reserved_topics_and_non_object_payloads(capsys):
    publisher = HAPublisher.__new__(HAPublisher)

    publisher._on_faceid_event("frigate/front_yard/person/active", b"1")
    publisher._on_faceid_event(
        "frigate/front_yard/person/event-1", b'"not-an-object"')

    assert capsys.readouterr().out == ""


def test_end_of_processing_queries_frigate_and_reassigns_unknown(tmp_path):
    db = Database(str(tmp_path / "juggle.db"))
    event_id = "event-2"
    session_id = db.start_session(
        f"/inbox/clip_front_yard_{event_id}.mp4", 25.0,
        frigate_event_id=event_id,
    )
    db.record_attempt(session_id, db.unknown_person_id, 1, 14, "end")
    ha = FakeHA(api_label="Artie")

    person_name, result = _apply_faceid_attribution(db, ha, session_id, event_id)

    assert person_name == "Artie"
    assert result is not None
    assert result["moved_count"] == 1
    # Attribution is now recorded in attempts/people, not a separate cache table.
    assert db.high_scores()["Artie"] == 14
    assert db.high_scores()["Unknown Juggler"] == 0
    assert ha.lookup_count == 1
    db.close()


def test_cached_or_pending_label_prevents_frigate_lookup(tmp_path):
    db = Database(str(tmp_path / "juggle.db"))
    event_id = "event-3"
    session_id = db.start_session("clip_front_yard_event-3.mp4", 25.0,
                                  frigate_event_id=event_id)
    db.record_attempt(session_id, db.unknown_person_id, 1, 8, "end")
    # Pre-load the pending dict (simulates FaceID firing before clip finished).
    ha = FakeHA(api_label="Artie", pending_label="Art")

    person_name, result = _apply_faceid_attribution(db, ha, session_id, event_id)

    assert person_name == "Art"
    assert result is not None
    assert db.high_scores()["Art"] == 8
    # Frigate API was not queried because pending_label was available.
    assert ha.lookup_count == 0
    db.close()


def test_already_attributed_session_skips_frigate_lookup(tmp_path):
    db = Database(str(tmp_path / "juggle.db"))
    event_id = "event-4"
    session_id = db.start_session("clip_front_yard_event-4.mp4", 25.0,
                                  frigate_event_id=event_id)
    artie_id = db.add_person("Artie")
    db.record_attempt(session_id, artie_id, 1, 11, "end")
    ha = FakeHA(api_label="Art")

    person_name, result = _apply_faceid_attribution(db, ha, session_id, event_id)

    assert person_name is None
    assert result is None
    assert ha.lookup_count == 0
    db.close()
