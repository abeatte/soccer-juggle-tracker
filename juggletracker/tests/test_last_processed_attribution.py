import json

from juggletracker.ha_mqtt import HAPublisher
from juggletracker.pipeline import _frigate_event_id


class FakeClient:
    def __init__(self):
        self.messages = []

    def publish(self, topic, payload, retain=False):
        self.messages.append((topic, payload, retain))


def make_publisher():
    publisher = HAPublisher.__new__(HAPublisher)
    publisher.enabled = True
    publisher.client = FakeClient()
    publisher.node = "juggle_tracker"
    publisher.cfg = type("Cfg", (), {"capture": {}, "home_assistant": {}})()
    publisher._last_processed_event_id = None
    publisher._last_processed_payload = None
    publisher._recent_faceid_attribution = None
    publisher._probe_codec = lambda _path: None
    return publisher


def _last_payload(publisher):
    return json.loads(publisher.client.messages[-1][1])


def test_frigate_event_id_handles_underscored_camera_names():
    assert _frigate_event_id("clip_back_yard_event123.mp4") == "event123"
    assert _frigate_event_id("clip_front_event456.mp4") == "event456"
    assert _frigate_event_id("other_clip.mp4") is None


def test_last_processed_tracks_faceid_attribution_after_publish():
    publisher = make_publisher()
    publisher.publish_last_processed(
        "/processed/clip_back_yard_event123.mp4",
        [{"person": "Unknown Juggler", "count": 4}],
    )

    publisher.note_last_processed_attribution("event123", "Kid One")

    payload = _last_payload(publisher)
    assert payload["attributed_to"] == "Kid One"


def test_last_processed_uses_faceid_attribution_before_publish():
    publisher = make_publisher()
    publisher.note_last_processed_attribution("event123", "Kid One")
    publisher.publish_last_processed(
        "/processed/clip_back_yard_event123.mp4",
        [{"person": "Unknown Juggler", "count": 4}],
    )

    payload = _last_payload(publisher)
    assert payload["attributed_to"] == "Kid One"
