from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggletracker import audio as audio_module  # noqa: E402
from juggletracker.capture import record_clip  # noqa: E402


def test_detect_impact_times_merges_nearby_transients():
    sample_rate = 16000
    pcm = np.zeros(sample_rate, dtype=np.int16)
    pcm[int(0.20 * sample_rate)] = 24000
    pcm[int(0.24 * sample_rate)] = 18000
    pcm[int(0.70 * sample_rate)] = 26000

    times = audio_module.detect_impact_times(pcm, sample_rate=sample_rate)

    assert len(times) == 2
    assert times[0] == 0.2
    assert times[1] == pytest.approx(0.7)


def test_silence_has_no_impact_events():
    assert audio_module.detect_impact_times(np.zeros(16000, dtype=np.int16)) == []


def test_registered_sound_count_advances_with_video_time():
    sounds = audio_module.RegisteredSounds([0.2, 0.7])

    assert sounds.update(0.1) == 0
    assert sounds.update(0.2) == 1
    assert sounds.update(0.6) == 1
    assert sounds.update(0.7) == 2
    assert sounds.update(1.0) == 2


def test_clip_without_audio_returns_no_events(monkeypatch):
    monkeypatch.setattr(
        audio_module.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout=b""),
    )

    assert audio_module.read_impact_times("silent.mp4") == []


def test_read_impact_times_detects_decoded_audio(monkeypatch):
    pcm = np.zeros(16000, dtype=np.int16)
    pcm[3200] = 24000
    commands = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout=pcm.tobytes())

    monkeypatch.setattr(audio_module.subprocess, "run", fake_run)

    times = audio_module.read_impact_times("with-audio.mp4")

    assert times == pytest.approx([0.2])
    assert commands[0][commands[0].index("-map") + 1] == "0:a:0"


def test_record_clip_does_not_drop_audio(monkeypatch, tmp_path):
    commands = []
    monkeypatch.setattr(
        "juggletracker.capture.subprocess.run",
        lambda command, **_kwargs: commands.append(command),
    )

    record_clip("rtsp://camera", str(tmp_path / "clip.mp4"), 2)

    assert "-an" not in commands[0]
    assert commands[0][commands[0].index("-c") + 1] == "copy"