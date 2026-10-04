from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import juggletracker.ha_mqtt as ha_mqtt  # noqa: E402
from juggletracker.ha_mqtt import HAPublisher  # noqa: E402


class FakeClient:
    def __init__(self):
        self.messages = []

    def publish(self, topic, payload, retain=False):
        self.messages.append((topic, payload, retain))


def _publisher(inbox, processed, highscore_dir):
    publisher = HAPublisher.__new__(HAPublisher)
    publisher.enabled = True
    publisher.client = FakeClient()
    publisher.node = "juggle_tracker"
    publisher.cfg = SimpleNamespace(
        capture={
            "inbox_dir": str(inbox),
            "processed_dir": str(processed),
            "highscore_dir": str(highscore_dir),
        },
        database=SimpleNamespace(path="unused.db"),
    )
    publisher.media_base = "http://ha.local:8123/local/juggle"
    publisher._worker = None
    publisher._reprocess_selected = None
    publisher._last_select_options = None
    publisher._announce_reprocess_select = lambda _options: None
    publisher._inbox_metadata_cache = {}
    return publisher


def test_queue_publishes_metadata_for_every_clip_and_caches_media(
        tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    processed = tmp_path / "processed"
    highscore_dir = tmp_path / "highscores"
    inbox.mkdir()
    processed.mkdir()
    clips = {"a.mp4": 65.8, "b.mkv": 3601.2}
    for name in clips:
        (inbox / name).write_bytes(b"clip")

    publisher = _publisher(inbox, processed, highscore_dir)
    commands = []

    def fake_media_tool(command, **_kwargs):
        if command[0] == "ffprobe":
            return SimpleNamespace(stdout=f"{clips[Path(command[-1]).name]}\n")
        commands.append(command)
        Path(command[-1]).write_bytes(b"thumbnail")

    monkeypatch.setattr(ha_mqtt.subprocess, "run", fake_media_tool)
    publisher.publish_queues()

    payload = json.loads(next(
        raw for topic, raw, _retain in publisher.client.messages
        if topic == "juggle_tracker/inbox"
    ))
    metadata = {clip["name"]: clip for clip in payload["clips"]}
    assert set(metadata) == set(clips)
    assert metadata["a.mp4"]["duration"] == "1:05"
    assert metadata["b.mkv"]["duration"] == "1:00:01"
    for name in clips:
        assert metadata[name]["thumbnail_url"].startswith(
            "http://ha.local:8123/local/juggle/inbox_thumbnails/"
        )
    assert all(Path(entry["thumbnail_path"]).is_file()
               for entry in publisher._inbox_metadata_cache.values())

    command_count = len(commands)
    publisher.publish_queues()
    assert len(commands) == command_count


def test_per_clip_delete_command_removes_only_requested_file(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    selected = inbox / "selected.mp4"
    other = inbox / "other.mp4"
    selected.touch()
    other.touch()

    publisher = _publisher(inbox, tmp_path / "processed", tmp_path / "highscores")
    publisher.cmd_topic = "juggle_tracker/reset/set"
    publisher.reprocess_topic = "juggle_tracker/reprocess/set"
    publisher.reprocess_select_topic = "juggle_tracker/reprocess_select/set"
    publisher.annotate_processed_topic = "juggle_tracker/annotate_processed/set"
    publisher.delete_file_topic = "juggle_tracker/delete_file/set"
    publisher._worker = None
    publisher.publish_queues = lambda: None
    message = SimpleNamespace(
        topic=publisher.delete_file_topic,
        payload=b"selected.mp4",
    )

    publisher._on_message(None, None, message)

    assert not selected.exists()
    assert other.exists()


def test_per_clip_delete_requests_cancel_for_active_clip(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    clip = inbox / "active.mp4"
    clip.touch()
    publisher = _publisher(inbox, tmp_path / "processed", tmp_path / "highscores")
    requested = []
    publisher._worker = SimpleNamespace(
        request_cancel=lambda name: requested.append(name) or True
    )
    publisher.publish_queues = lambda: None

    publisher._do_delete_inbox("active.mp4")

    assert requested == ["active.mp4"]
    assert clip.exists()