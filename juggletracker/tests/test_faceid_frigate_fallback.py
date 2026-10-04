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
    def __init__(self, api_label=None):
        self.api_label = api_label
        self.lookup_count = 0

    def lookup_frigate_sub_label(self, _event_id):
        self.lookup_count += 1
        return self.api_label


def _publisher(enabled=True):
    publisher = HAPublisher.__new__(HAPublisher)
    publisher._faceid_enabled = enabled
    publisher.cfg = SimpleNamespace(home_assistant={
        "frigate_api_url": "http://frigate.local:5000/",
    })
    return publisher


def test_lookup_frigate_sub_label_reads_event_api(monkeypatch):
    publisher = _publisher()
    requests = []

    def fake_urlopen(request, timeout):
        requests.append((request.full_url, timeout))
        return FakeResponse({"id": "event-1", "sub_label": "Artie"})

    monkeypatch.setattr(ha_mqtt, "urlopen", fake_urlopen)

    assert publisher.lookup_frigate_sub_label("event-1") == "Artie"
    assert requests == [
        ("http://frigate.local:5000/api/events/event-1", 4)
    ]


def test_lookup_frigate_sub_label_accepts_list_payload(monkeypatch):
    publisher = _publisher()

    def fake_urlopen(request, timeout):
        return FakeResponse({"id": "event-1", "sub_label": ["Artie", 0.91]})

    monkeypatch.setattr(ha_mqtt, "urlopen", fake_urlopen)

    assert publisher.lookup_frigate_sub_label("event-1") == "Artie"


def test_lookup_frigate_sub_label_skips_when_disabled(monkeypatch):
    publisher = _publisher(enabled=False)

    def boom(*_args, **_kwargs):
        raise AssertionError("Frigate should not be queried when FaceID is off")

    monkeypatch.setattr(ha_mqtt, "urlopen", boom)
    assert publisher.lookup_frigate_sub_label("event-1") is None


def test_end_of_processing_queries_frigate_and_reassigns_unknown(tmp_path):
    db = Database(str(tmp_path / "juggle.db"))
    event_id = "event-2"
    session_id = db.start_session(
        f"/inbox/clip_front_yard_{event_id}.mp4", 25.0,
        frigate_event_id=event_id,
    )
    db.record_attempt(session_id, db.unknown_person_id, 1, 14, "end")
    ha = FakeHA(api_label="Artie")

    person_name, result = _apply_faceid_attribution(db, ha, event_id)

    assert person_name == "Artie"
    assert result is not None
    assert result["moved_count"] == 1
    assert db.high_scores()["Artie"] == 14
    assert db.high_scores()["Unknown Juggler"] == 0
    assert ha.lookup_count == 1
    db.close()


def test_missing_frigate_label_leaves_unknown(tmp_path):
    db = Database(str(tmp_path / "juggle.db"))
    event_id = "event-3"
    session_id = db.start_session("clip_front_yard_event-3.mp4", 25.0,
                                  frigate_event_id=event_id)
    db.record_attempt(session_id, db.unknown_person_id, 1, 8, "end")
    ha = FakeHA(api_label=None)

    person_name, result = _apply_faceid_attribution(db, ha, event_id)

    assert person_name is None
    assert result is None
    assert db.high_scores()["Unknown Juggler"] == 8
    assert ha.lookup_count == 1
    db.close()
