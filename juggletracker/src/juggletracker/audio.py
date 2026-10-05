"""Detect brief audio transients for visual-review diagnostics."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field

import numpy as np


def detect_impact_times(
    pcm: np.ndarray,
    sample_rate: int = 16000,
    threshold: float = 0.006,
    refractory_seconds: float = 0.12,
    window_ms: int = 10,
) -> list[float]:
    """Return approximate times of short, broadband transients in int16 PCM."""
    samples = np.asarray(pcm)
    if samples.size == 0 or sample_rate <= 0:
        return []
    if np.issubdtype(samples.dtype, np.integer):
        samples = samples.astype(np.float32) / 32768.0
    else:
        samples = samples.astype(np.float32)
    samples = samples.reshape(-1)

    window_size = max(1, int(sample_rate * window_ms / 1000))
    high_pass = np.diff(samples, prepend=samples[0])
    padded_size = ((len(high_pass) + window_size - 1) // window_size) * window_size
    padded = np.pad(high_pass, (0, padded_size - len(high_pass)))
    energy = np.sqrt(np.mean(padded.reshape(-1, window_size) ** 2, axis=1))

    noise_median = float(np.median(energy))
    noise_mad = float(np.median(np.abs(energy - noise_median)))
    onset_threshold = max(threshold, noise_median + 8.0 * 1.4826 * noise_mad)
    candidates = np.flatnonzero(energy >= onset_threshold)
    if candidates.size == 0:
        return []

    window_seconds = window_size / sample_rate
    refractory_windows = max(1, int(refractory_seconds / window_seconds))
    peaks: list[int] = []
    for index in candidates:
        index = int(index)
        if peaks and index - peaks[-1] <= refractory_windows:
            if energy[index] > energy[peaks[-1]]:
                peaks[-1] = index
        else:
            peaks.append(index)
    return [index * window_seconds for index in peaks]


def read_impact_times(
    clip_path: str,
    threshold: float = 0.006,
    refractory_seconds: float = 0.12,
    sample_rate: int = 16000,
) -> list[float]:
    """Decode a clip's first audio stream to mono PCM and detect transients.

    Clips without a decodable audio stream simply produce no sound events.
    """
    try:
        result = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", clip_path, "-map", "0:a:0",
             "-vn", "-ac", "1", "-ar", str(sample_rate), "-f", "s16le",
             "pipe:1"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return []
    if result.returncode != 0:
        return []
    pcm = np.frombuffer(result.stdout, dtype=np.int16)
    return detect_impact_times(
        pcm,
        sample_rate=sample_rate,
        threshold=threshold,
        refractory_seconds=refractory_seconds,
    )


@dataclass
class RegisteredSounds:
    """Advance detected sound count in step with the video timestamp."""

    event_times: list[float]
    count: int = 0
    _next_event: int = field(default=0, init=False)

    def update(self, timestamp: float) -> int:
        while (self._next_event < len(self.event_times) and
               self.event_times[self._next_event] <= timestamp):
            self.count += 1
            self._next_event += 1
        return self.count