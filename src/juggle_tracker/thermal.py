"""Thermal guard — pause processing when the CPU package gets too hot.

On an old laptop (this target is a 2012 i7-3740QM) sustained ML load pushes the
CPU toward its throttle point (~100 C on Ivy Bridge). Rather than let the OS
throttle mid-clip (unpredictable) or cook the machine, we read the CPU package
temperature from Linux sysfs and pause the pipeline until it cools back to a
resume threshold.

Reads (in order of preference):
  1. thermal_zone of type 'x86_pkg_temp'  (the CPU package sensor)
  2. any 'coretemp'/'Package' hwmon input
  3. the hottest thermal_zone as a fallback
Returns None when no sensor is readable (e.g. non-Linux), in which case the
guard is a no-op so it never blocks on platforms without the sysfs interface.
"""
from __future__ import annotations

import glob
import os
import time
from typing import Callable, Optional


def read_package_temp_c() -> Optional[float]:
    """Best-effort CPU package temperature in Celsius, or None if unavailable."""
    zones = []
    for zone in glob.glob("/sys/class/thermal/thermal_zone*"):
        try:
            ztype = open(os.path.join(zone, "type")).read().strip()
            milli = int(open(os.path.join(zone, "temp")).read().strip())
            zones.append((ztype, milli / 1000.0))
        except (OSError, ValueError):
            continue
    if zones:
        for ztype, val in zones:
            if ztype == "x86_pkg_temp":
                return val
    # hwmon fallback (coretemp "Package id 0")
    for label_path in glob.glob("/sys/class/hwmon/hwmon*/temp*_label"):
        try:
            label = open(label_path).read().strip().lower()
            if "package" in label:
                inp = label_path.replace("_label", "_input")
                return int(open(inp).read().strip()) / 1000.0
        except (OSError, ValueError):
            continue
    if zones:
        return max(v for _, v in zones)
    return None


class ThermalGuard:
    """Blocks until the CPU cools when it exceeds ``max_temp_c``."""

    def __init__(
        self,
        enabled: bool = True,
        max_temp_c: float = 90.0,
        resume_temp_c: float = 80.0,
        poll_seconds: float = 5.0,
        on_pause: Optional[Callable[[float], None]] = None,
    ):
        self.enabled = enabled
        self.max_temp_c = max_temp_c
        self.resume_temp_c = resume_temp_c
        self.poll_seconds = poll_seconds
        self.on_pause = on_pause
        self.total_paused_s = 0.0

    def maybe_wait(self) -> None:
        """If the CPU is over the limit, block until it cools to the resume temp."""
        if not self.enabled:
            return
        t = read_package_temp_c()
        if t is None or t < self.max_temp_c:
            return
        # Too hot — pause until we cool down (or the sensor disappears).
        while True:
            t = read_package_temp_c()
            if t is None or t <= self.resume_temp_c:
                return
            if self.on_pause:
                self.on_pause(t)
            time.sleep(self.poll_seconds)
            self.total_paused_s += self.poll_seconds

    @classmethod
    def from_config(cls, cfg, on_pause=None) -> "ThermalGuard":
        th = cfg.get("thermal") if hasattr(cfg, "get") else None
        th = th or {}
        return cls(
            enabled=bool(th.get("enabled", True)),
            max_temp_c=float(th.get("max_temp_c", 90.0)),
            resume_temp_c=float(th.get("resume_temp_c", 80.0)),
            poll_seconds=float(th.get("poll_seconds", 5.0)),
            on_pause=on_pause,
        )
