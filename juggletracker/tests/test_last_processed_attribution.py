import json
from pathlib import Path

from juggletracker.ha_mqtt import HAPublisher
from juggletracker.pipeline import _frigate_event_id
from juggletracker.cli import _process_inbox_clip


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
    publisher.media_base = "http://ha.local:8123/local/juggle"
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


def test_last_processed_uses_annotated_output_when_available(tmp_path):
    publisher = make_publisher()
    publisher.cfg.capture["highscore_dir"] = str(tmp_path)
    publisher._probe_codec = lambda _path: "h264"
    (tmp_path / "last_processed.mp4").write_bytes(b"overlay")
    clip = tmp_path / "clip_front_event123.mp4"
    clip.touch()

    publisher.publish_last_processed(
        str(clip), [{"person": "Art", "count": 9}], annotated=True)

    payload = _last_payload(publisher)
    assert payload["video_url"].startswith(
        "http://ha.local:8123/local/juggle/last_processed.mp4?v="
    )
    assert payload["annotated"] is True
    assert payload["processing"] is False


def test_last_processed_uses_archived_video_for_normal_run(tmp_path):
    publisher = make_publisher()
    publisher._probe_codec = lambda _path: "h264"
    clip = tmp_path / "clip_front_event123.mp4"
    clip.touch()

    publisher.publish_last_processed(
        str(clip), [{"person": "Art", "count": 9}], annotated=False)

    payload = _last_payload(publisher)
    assert "/local/processed/clip_front_event123.mp4?v=" in payload["video_url"]
    assert payload["annotated"] is False


def test_reprocess_pending_uses_unified_last_processed_topic():
    publisher = make_publisher()
    publisher.publish_last_processed_pending("clip.mp4", annotated=True)

    topic, raw_payload, retained = publisher.client.messages[-1]
    assert topic == "juggle_tracker/last_processed"
    assert retained is True
    payload = json.loads(raw_payload)
    assert payload["video_url"] == ""
    assert payload["annotated"] is True
    assert payload["processing"] is True


def test_inbox_processing_uses_shared_annotation_mode():
    class PipelineStub:
        def process(self, clip):
            return ("normal", clip)

        def process_annotated_viewable(self, clip):
            return ("annotated", clip)

    pipe = PipelineStub()
    assert _process_inbox_clip(pipe, "regular.mp4", annotate=False) == (
        "normal", "regular.mp4")
    assert _process_inbox_clip(pipe, "reprocessed.mp4", annotate=True) == (
        "annotated", "reprocessed.mp4")
