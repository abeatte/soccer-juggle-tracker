from __future__ import annotations

import json
import os
import queue
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "frigate", "inbox-bridge"))

from juggletracker.db import Database, load_setting, save_setting  # noqa: E402
from juggletracker.ha_mqtt import HAPublisher  # noqa: E402
import juggletracker.cli as cli  # noqa: E402
import juggletracker.pipeline as pipeline_module  # noqa: E402
from bridge import EVENT_TOPIC, Event, InboxBridge  # noqa: E402


class FakeMqttClient:
    def __init__(self):
        self.calls = []
        self.published = []

    def subscribe(self, topic, qos=0):
        self.calls.append(("subscribe", topic, qos))

    def unsubscribe(self, topic):
        self.calls.append(("unsubscribe", topic))

    def publish(self, topic, payload, retain=False):
        self.published.append((topic, payload, retain))


def test_tracking_switch_setting_persists(tmp_path):
    path = str(tmp_path / "juggle.db")
    db = Database(path)
    db.close()

    assert load_setting(path, "tracking_enabled", "ON") == "ON"
    save_setting(path, "tracking_enabled", "OFF")
    assert load_setting(path, "tracking_enabled", "ON") == "OFF"


def test_ha_switch_command_persists_and_publishes_state(tmp_path):
    path = str(tmp_path / "juggle.db")
    Database(path).close()
    publisher = HAPublisher.__new__(HAPublisher)
    publisher.enabled = True
    publisher.cfg = SimpleNamespace(database=SimpleNamespace(
        get=lambda _key, _default=None: path
    ))
    publisher.tracking_enabled = True
    publisher.tracking_enabled_topic = "juggle_tracker/tracking_enabled"
    publisher.tracking_enabled_command_topic = "juggle_tracker/tracking_enabled/set"
    publisher.cmd_topic = "juggle_tracker/reset/set"
    publisher.reprocess_topic = "juggle_tracker/reprocess/set"
    publisher.reprocess_select_topic = "juggle_tracker/reprocess_select/set"
    publisher.annotate_processed_topic = "juggle_tracker/annotate_processed/set"
    publisher.delete_file_topic = "juggle_tracker/delete_file/set"
    publisher.calib_enabled = False
    publisher.client = FakeMqttClient()

    publisher._on_message(None, None, SimpleNamespace(
        topic=publisher.tracking_enabled_command_topic,
        payload=b"OFF",
    ))

    assert publisher.tracking_enabled is False
    assert load_setting(path, "tracking_enabled", "ON") == "OFF"
    assert publisher.client.published == [
        (publisher.tracking_enabled_topic, "OFF", True)
    ]


def test_bridge_waits_for_enabled_state_and_unsubscribes_when_off(monkeypatch):
    monkeypatch.setenv("CAMERA_FILTER", "")
    monkeypatch.setenv("ZONE_FILTER", "")
    bridge = InboxBridge()
    client = FakeMqttClient()

    bridge.on_connect(client, None, None, 0)

    assert client.calls == [("subscribe", bridge.enabled_topic, 1)]
    assert not bridge.enabled

    bridge.on_message(client, None, SimpleNamespace(
        topic=bridge.enabled_topic, payload=b"ON"
    ))
    assert bridge.enabled
    assert client.calls[-1] == ("subscribe", EVENT_TOPIC, 1)

    event = {"type": "end", "after": {
        "label": "person", "camera": "front_yard", "id": "event-1"
    }}
    bridge.on_message(client, None, SimpleNamespace(
        topic=EVENT_TOPIC, payload=json.dumps(event).encode()
    ))
    assert bridge.events.get_nowait() == Event("event-1", "front_yard")

    bridge.on_message(client, None, SimpleNamespace(
        topic=bridge.enabled_topic, payload=b"OFF"
    ))
    assert not bridge.enabled
    assert client.calls[-1] == ("unsubscribe", EVENT_TOPIC)
    with pytest.raises(queue.Empty):
        bridge.events.get_nowait()

    bridge.on_message(client, None, SimpleNamespace(
        topic=EVENT_TOPIC, payload=json.dumps(event).encode()
    ))
    assert bridge.events.empty()


def test_bridge_does_not_download_while_disabled(monkeypatch, tmp_path):
    bridge = InboxBridge()
    bridge.inbox_dir = str(tmp_path / "inbox")
    bridge.enabled = False

    def unexpected_download(*_args, **_kwargs):
        raise AssertionError("disabled bridge attempted to download a clip")

    monkeypatch.setattr("bridge.urllib.request.urlopen", unexpected_download)

    bridge.download_clip(Event("event-2", "front_yard"))

    assert not os.path.exists(bridge.inbox_dir)


def test_watcher_leaves_inbox_clips_untouched_when_disabled(monkeypatch, tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    clip = inbox / "clip_front_yard_event-3.mp4"
    clip.write_bytes(b"video")

    class FakePublisher:
        tracking_enabled = False
        statuses = []

        def publish_queues(self):
            pass

        def publish_status(self, state, **_kwargs):
            self.statuses.append(state)

    class FakePipeline:
        def __init__(self, _cfg):
            self.ha = FakePublisher()
            self.closed = False

        def close(self):
            self.closed = True

    pipeline = FakePipeline(None)
    monkeypatch.setattr(cli, "load_config", lambda _path: SimpleNamespace(
        capture=SimpleNamespace(inbox_dir=str(inbox))
    ))
    monkeypatch.setattr(pipeline_module, "Pipeline", lambda _cfg: pipeline)
    monkeypatch.setattr(cli, "_is_stable", lambda _path: pytest.fail(
        "disabled watcher must not inspect clips for processing"
    ))
    monkeypatch.setattr(cli.time, "sleep", lambda _seconds: (_ for _ in ()).throw(
        KeyboardInterrupt()
    ))

    assert cli._watch(SimpleNamespace(config="config.yaml", interval=0)) == 0

    assert clip.exists()
    assert pipeline.ha.statuses == ["disabled"]
    assert pipeline.closed


def test_watcher_rechecks_switch_after_stability_wait(monkeypatch, tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    clip = inbox / "clip_front_yard_event-4.mp4"
    clip.write_bytes(b"video")

    class FakePublisher:
        tracking_enabled = True
        statuses = []

        def publish_queues(self):
            pass

        def publish_status(self, state, **_kwargs):
            self.statuses.append(state)

    class FakePipeline:
        def __init__(self, _cfg):
            self.ha = FakePublisher()
            self.closed = False

        def close(self):
            self.closed = True

    pipeline = FakePipeline(None)
    monkeypatch.setattr(cli, "load_config", lambda _path: SimpleNamespace(
        capture=SimpleNamespace(inbox_dir=str(inbox))
    ))
    monkeypatch.setattr(pipeline_module, "Pipeline", lambda _cfg: pipeline)

    def turn_off_during_stability_check(_path):
        pipeline.ha.tracking_enabled = False
        return True

    monkeypatch.setattr(cli, "_is_stable", turn_off_during_stability_check)
    monkeypatch.setattr(cli.time, "sleep", lambda _seconds: (_ for _ in ()).throw(
        KeyboardInterrupt()
    ))

    assert cli._watch(SimpleNamespace(config="config.yaml", interval=0)) == 0

    assert clip.exists()
    assert pipeline.ha.statuses == ["idle", "disabled"]
    assert pipeline.closed
