#!/usr/bin/env python3
"""Publish host machine telemetry to Home Assistant over MQTT discovery.

Mirrors the conventions of the soccer-juggle-tracker HAPublisher:
  * HA MQTT **discovery** so entities auto-appear (no HA YAML editing)
  * retained discovery configs under  <discovery_prefix>/sensor/<node>/<key>/config
  * a single retained JSON state blob at  <node>/state  (value_json.<key>)
  * an availability topic + Last-Will so HA shows the device offline on crash
  * paho-mqtt CallbackAPIVersion.VERSION2 (paho >= 2.0)

Reads are pure stdlib from /proc and /sys/class/hwmon (no lm-sensors dependency).
On this 2012 MacBook Pro the CPU temp comes from the `coretemp` hwmon and the
fan RPM from the `applesmc` hwmon. Docker container stats are optional.

Config is via environment variables (see config.example.env):
  MQTT_HOST MQTT_PORT MQTT_USER MQTT_PASS
  NODE_ID DEVICE_NAME DISCOVERY_PREFIX INTERVAL DOCKER_CONTAINERS
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Optional

import paho.mqtt.client as mqtt

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
MQTT_HOST = os.environ.get("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER", "")
MQTT_PASS = os.environ.get("MQTT_PASS", "")
NODE_ID = os.environ.get("NODE_ID", "frigate_box")
DEVICE_NAME = os.environ.get("DEVICE_NAME", "Frigate Box (MacBook Pro)")
DISCOVERY_PREFIX = os.environ.get("DISCOVERY_PREFIX", "homeassistant")
INTERVAL = float(os.environ.get("INTERVAL", "15"))
# Comma-separated docker container names to report CPU/mem for (blank = none).
DOCKER_CONTAINERS = [
    c.strip() for c in os.environ.get("DOCKER_CONTAINERS", "frigate").split(",") if c.strip()
]

STATE_TOPIC = f"{NODE_ID}/state"
AVAIL_TOPIC = f"{NODE_ID}/availability"

# --------------------------------------------------------------------------- #
# Sensor catalogue: key -> discovery config fragment
# `state`/`avail` topics + device block are filled in at announce time.
# --------------------------------------------------------------------------- #
SENSORS: dict[str, dict] = {
    "cpu_percent": {
        "name": "CPU Usage", "unit_of_measurement": "%",
        "icon": "mdi:cpu-64-bit", "state_class": "measurement",
    },
    "cpu_temp_c": {
        "name": "CPU Temperature", "unit_of_measurement": "°C",
        "device_class": "temperature", "state_class": "measurement",
    },
    "fan_rpm": {
        "name": "Fan Speed", "unit_of_measurement": "rpm",
        "icon": "mdi:fan", "state_class": "measurement",
    },
    "load_1": {"name": "Load Average 1m", "icon": "mdi:chart-line", "state_class": "measurement"},
    "load_5": {"name": "Load Average 5m", "icon": "mdi:chart-line", "state_class": "measurement"},
    "load_15": {"name": "Load Average 15m", "icon": "mdi:chart-line", "state_class": "measurement"},
    "mem_used_percent": {
        "name": "Memory Used", "unit_of_measurement": "%",
        "icon": "mdi:memory", "state_class": "measurement",
    },
    "disk_used_percent": {
        "name": "Disk Used", "unit_of_measurement": "%",
        "icon": "mdi:harddisk", "state_class": "measurement",
    },
    "uptime_hours": {
        "name": "Uptime", "unit_of_measurement": "h",
        "icon": "mdi:clock-outline", "state_class": "total_increasing",
    },
}
# Per-container sensors are added dynamically below.


def _device() -> dict:
    return {
        "identifiers": [NODE_ID],
        "name": DEVICE_NAME,
        "model": "i7-3740QM Ivy Bridge / Ubuntu",
        "manufacturer": "DIY",
    }


# --------------------------------------------------------------------------- #
# Metric collection (all best-effort; return None on any failure)
# --------------------------------------------------------------------------- #
_prev_cpu: Optional[tuple[int, int]] = None  # (idle, total)


def read_cpu_percent() -> Optional[float]:
    """Percent busy since the previous call, from /proc/stat aggregate line."""
    global _prev_cpu
    try:
        with open("/proc/stat") as f:
            parts = f.readline().split()
        vals = list(map(int, parts[1:]))
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle + iowait
        total = sum(vals)
        if _prev_cpu is None:
            _prev_cpu = (idle, total)
            return None  # need a delta; skip first sample
        d_idle = idle - _prev_cpu[0]
        d_total = total - _prev_cpu[1]
        _prev_cpu = (idle, total)
        if d_total <= 0:
            return None
        return round(100.0 * (1.0 - d_idle / d_total), 1)
    except Exception:
        return None


def read_loadavg() -> tuple[Optional[float], Optional[float], Optional[float]]:
    try:
        a, b, c = open("/proc/loadavg").read().split()[:3]
        return float(a), float(b), float(c)
    except Exception:
        return None, None, None


def _hwmon_by_name() -> dict[str, Path]:
    out: dict[str, Path] = {}
    base = Path("/sys/class/hwmon")
    if not base.exists():
        return out
    for hw in base.glob("hwmon*"):
        try:
            out[(hw / "name").read_text().strip()] = hw
        except Exception:
            continue
    return out


def read_cpu_temp() -> Optional[float]:
    """Max core temperature (°C) from the coretemp hwmon."""
    hw = _hwmon_by_name()
    node = hw.get("coretemp") or hw.get("k10temp") or hw.get("cpu_thermal")
    temps: list[float] = []
    if node:
        for t in node.glob("temp*_input"):
            try:
                temps.append(int(t.read_text()) / 1000.0)
            except Exception:
                pass
    if not temps:
        # Fallback: /sys/class/thermal
        for tz in Path("/sys/class/thermal").glob("thermal_zone*/temp"):
            try:
                temps.append(int(tz.read_text()) / 1000.0)
            except Exception:
                pass
    return round(max(temps), 1) if temps else None


def read_fan_rpm() -> Optional[int]:
    """Max fan RPM from the applesmc hwmon (MacBook)."""
    hw = _hwmon_by_name()
    node = hw.get("applesmc")
    rpms: list[int] = []
    # Search applesmc first, then any hwmon exposing fan*_input.
    candidates = [node] if node else list(_hwmon_by_name().values())
    for n in candidates:
        if not n:
            continue
        for f in n.glob("fan*_input"):
            try:
                rpms.append(int(f.read_text()))
            except Exception:
                pass
        if rpms:
            break
    return max(rpms) if rpms else None


def read_mem_used_percent() -> Optional[float]:
    try:
        info = {}
        for line in open("/proc/meminfo"):
            k, v = line.split(":")
            info[k.strip()] = int(v.strip().split()[0])  # kB
        total = info["MemTotal"]
        avail = info.get("MemAvailable", info["MemFree"])
        return round(100.0 * (total - avail) / total, 1)
    except Exception:
        return None


def read_disk_used_percent(path: str = "/") -> Optional[float]:
    try:
        u = shutil.disk_usage(path)
        return round(100.0 * u.used / u.total, 1)
    except Exception:
        return None


def read_uptime_hours() -> Optional[float]:
    try:
        return round(float(open("/proc/uptime").read().split()[0]) / 3600.0, 1)
    except Exception:
        return None


_DOCKER_OK = shutil.which("docker") is not None


def read_docker_stats() -> dict[str, float]:
    """Return {<name>_cpu_percent, <name>_mem_mb} for configured containers."""
    result: dict[str, float] = {}
    if not (_DOCKER_OK and DOCKER_CONTAINERS):
        return result
    try:
        proc = subprocess.run(
            ["docker", "stats", "--no-stream", "--format",
             "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}", *DOCKER_CONTAINERS],
            capture_output=True, text=True, timeout=15,
        )
        for line in proc.stdout.strip().splitlines():
            try:
                name, cpu, mem = line.split("\t")
                slug = name.strip().lower().replace("-", "_")
                result[f"{slug}_cpu_percent"] = float(cpu.strip().rstrip("%"))
                # MemUsage looks like "512MiB / 15GiB" — take the first token.
                used = mem.split("/")[0].strip()
                num = float("".join(ch for ch in used if (ch.isdigit() or ch == ".")))
                unit = "".join(ch for ch in used if ch.isalpha()).lower()
                mb = num * (1024 if unit.startswith("gi") else 1 if unit.startswith("mi")
                            else 1000 if unit.startswith("g") else 1)
                result[f"{slug}_mem_mb"] = round(mb, 1)
            except Exception:
                continue
    except Exception:
        return result
    return result


def collect() -> dict:
    l1, l5, l15 = read_loadavg()
    state = {
        "cpu_percent": read_cpu_percent(),
        "cpu_temp_c": read_cpu_temp(),
        "fan_rpm": read_fan_rpm(),
        "load_1": l1, "load_5": l5, "load_15": l15,
        "mem_used_percent": read_mem_used_percent(),
        "disk_used_percent": read_disk_used_percent(),
        "uptime_hours": read_uptime_hours(),
    }
    state.update(read_docker_stats())
    # Drop keys whose value is None so HA doesn't get "null".
    return {k: v for k, v in state.items() if v is not None}


# --------------------------------------------------------------------------- #
# MQTT
# --------------------------------------------------------------------------- #
def announce(client: mqtt.Client, keys: list[str]) -> None:
    """Publish retained discovery config for every known + dynamic sensor key."""
    dev = _device()
    for key in keys:
        meta = SENSORS.get(key)
        if meta is None:  # dynamic (e.g. frigate_cpu_percent) — infer a sane config
            if key.endswith("_cpu_percent"):
                meta = {"name": key.replace("_cpu_percent", "").title() + " CPU",
                        "unit_of_measurement": "%", "icon": "mdi:cpu-64-bit",
                        "state_class": "measurement"}
            elif key.endswith("_mem_mb"):
                meta = {"name": key.replace("_mem_mb", "").title() + " Memory",
                        "unit_of_measurement": "MB", "icon": "mdi:memory",
                        "state_class": "measurement"}
            else:
                meta = {"name": key}
        payload = {
            **meta,
            "unique_id": f"{NODE_ID}_{key}",
            "state_topic": STATE_TOPIC,
            "availability_topic": AVAIL_TOPIC,
            "value_template": f"{{{{ value_json.{key} }}}}",
            "device": dev,
        }
        client.publish(f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/{key}/config",
                       json.dumps(payload), retain=True)


def main() -> None:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id=f"{NODE_ID}_{int(time.time())}")
    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.will_set(AVAIL_TOPIC, "offline", retain=True)
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=int(max(30, INTERVAL * 2)))
    client.loop_start()
    client.publish(AVAIL_TOPIC, "online", retain=True)

    running = {"v": True}

    def _stop(*_):
        running["v"] = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    read_cpu_percent()  # prime the CPU delta
    announced: set[str] = set()
    try:
        while running["v"]:
            state = collect()
            new_keys = [k for k in state if k not in announced]
            if new_keys:
                # (re)announce the full known catalogue + any new dynamic keys
                announce(client, list(SENSORS.keys()) + [k for k in state if k not in SENSORS])
                announced.update(state.keys())
            client.publish(STATE_TOPIC, json.dumps(state), retain=True)
            # sleep in small steps so SIGTERM is responsive
            slept = 0.0
            while running["v"] and slept < INTERVAL:
                time.sleep(0.5)
                slept += 0.5
    finally:
        client.publish(AVAIL_TOPIC, "offline", retain=True)
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
