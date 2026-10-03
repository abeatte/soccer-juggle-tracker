import json
from pathlib import Path
from types import SimpleNamespace

import juggletracker.ha_mqtt as ha_mqtt
from juggletracker.ha_mqtt import HAPublisher


class FakeClient:
    def __init__(self):
        self.messages = []

    def publish(self, topic, payload, retain=False):
        self.messages.append((topic, payload, retain))


def make_publisher(tmp_path, filename, codec):
    processed = tmp_path / "processed"
    processed.mkdir()
    (processed / filename).touch()
    publisher = HAPublisher.__new__(HAPublisher)
    publisher.enabled = True
    publisher.client = FakeClient()
    publisher.node = "juggle_tracker"
    publisher.cfg = SimpleNamespace(capture={
        "processed_dir": str(processed),
        "highscore_dir": str(tmp_path / "highscores"),
    })
    publisher.media_base = "http://ha.local:8123/local/juggle"
    publisher._reprocess_selected = filename
    publisher._reprocess_attributions = {filename: ["Artie"]}
    publisher._probe_codec = lambda _path: codec
    return publisher


def test_h264_mp4_preview_uses_read_only_processed_url(tmp_path, monkeypatch):
    publisher = make_publisher(tmp_path, "clip #1.mp4", "h264")

    def unexpected_ffmpeg(*args, **kwargs):
        raise AssertionError("H.264 MP4 should be served directly")

    monkeypatch.setattr(ha_mqtt.subprocess, "run", unexpected_ffmpeg)
    publisher._update_reprocess_preview()

    payload = json.loads(publisher.client.messages[-1][1])
    assert payload["video_url"].startswith(
        "http://ha.local:8123/local/processed/clip%20%231.mp4?v="
    )
    assert payload["attributed_to"] == "Artie"
    assert not (tmp_path / "highscores" / "reprocess_preview.mp4").exists()


def test_non_h264_preview_falls_back_to_mp4_transcode(tmp_path, monkeypatch):
    publisher = make_publisher(tmp_path, "clip.mkv", "hevc")
    commands = []

    def fake_ffmpeg(command, **kwargs):
        commands.append(command)
        Path(command[-1]).write_bytes(b"preview")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(ha_mqtt.subprocess, "run", fake_ffmpeg)
    publisher._update_reprocess_preview()

    payload = json.loads(publisher.client.messages[-1][1])
    assert "libx264" in commands[0]
    assert commands[0][-3:-1] == ["-f", "mp4"]
    assert payload["video_url"].startswith(
        "http://ha.local:8123/local/juggle/reprocess_preview.mp4?v="
    )
    assert (tmp_path / "highscores" / "reprocess_preview.mp4").read_bytes() == b"preview"


def test_probe_codec_returns_none_when_ffprobe_is_unavailable(monkeypatch):
    def missing_ffprobe(*args, **kwargs):
        raise FileNotFoundError("ffprobe")

    monkeypatch.setattr(ha_mqtt.subprocess, "run", missing_ffprobe)
    assert HAPublisher._probe_codec("clip.mp4") is None


def test_probe_codec_reads_video_codec_from_ffprobe(monkeypatch):
    commands = []

    def fake_ffprobe(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(stdout="h264\n")

    monkeypatch.setattr(ha_mqtt.subprocess, "run", fake_ffprobe)
    assert HAPublisher._probe_codec("clip.mp4") == "h264"
    assert commands[0][0] == "ffprobe"