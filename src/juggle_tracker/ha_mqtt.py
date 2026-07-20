"""Home Assistant integration over MQTT.

Publishes, per enrolled person, a sensor exposing their all-time juggle high
score, using HA's MQTT **discovery** so entities appear automatically with no
YAML editing. Also publishes a "last session" sensor with the most recent
result and fires an event when a new high score is set (for TTS / notifications).

Topics (with defaults discovery_prefix=homeassistant, node_id=juggle_tracker):
  homeassistant/sensor/juggle_tracker/<person>_high/config   (discovery)
  juggle_tracker/<person>/high                                (state)
  juggle_tracker/last_session                                 (json attributes)
  juggle_tracker/event/new_high_score                         (fire-and-forget)
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Optional

try:
    import paho.mqtt.client as mqtt
except Exception:  # pragma: no cover - optional at import time
    mqtt = None


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", name.strip().lower()).strip("_")


class HAPublisher:
    def __init__(self, cfg):
        self.cfg = cfg
        self.enabled = bool(cfg.home_assistant.get("enabled", False))
        self.prefix = cfg.home_assistant.get("discovery_prefix", "homeassistant")
        self.node = cfg.home_assistant.get("node_id", "juggle_tracker")
        # Base URL (as reached from a browser on the LAN) under which HA serves
        # the high-score replay clips. With the recommended `www/juggle` mount
        # this is http://<ha-host>:8123/local/juggle .
        self.media_base = cfg.home_assistant.get(
            "media_base_url", "http://192.168.0.139:8123/local/juggle"
        )
        self.client = None
        if not self.enabled:
            return
        if mqtt is None:
            raise RuntimeError("paho-mqtt not installed but home_assistant.enabled=true")
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id=f"{self.node}_{int(time.time())}"
        )
        user = cfg.home_assistant.get("mqtt_user")
        pw = cfg.home_assistant.get("mqtt_password")
        if user:
            self.client.username_pw_set(user, pw)
        self.avail_topic = f"{self.node}/availability"
        self.status_topic = f"{self.node}/status"
        self.timing_topic = f"{self.node}/timing"
        # Last-Will so HA shows the worker offline if it dies ungracefully.
        self.client.will_set(self.avail_topic, "offline", retain=True)
        self.client.connect(
            cfg.home_assistant.get("mqtt_host", "127.0.0.1"),
            int(cfg.home_assistant.get("mqtt_port", 1883)),
            keepalive=30,
        )
        self.client.loop_start()
        # Announce the worker status/progress entities and mark online + idle.
        self.client.publish(self.avail_topic, "online", retain=True)
        self.announce_status()
        self.announce_timing()
        self.publish_status("idle")

    # ------------------------------------------------------------------
    def _device(self) -> dict:
        return {
            "identifiers": [self.node],
            "name": "Soccer Juggle Tracker",
            "model": "RLC-810A CV pipeline",
            "manufacturer": "DIY",
        }

    def announce_person(self, name: str) -> None:
        """Publish MQTT discovery config for a person's high-score sensor."""
        if not self.enabled:
            return
        slug = _slug(name)
        topic = f"{self.prefix}/sensor/{self.node}/{slug}_high/config"
        payload = {
            "name": f"{name} Juggle High Score",
            "unique_id": f"{self.node}_{slug}_high",
            "state_topic": f"{self.node}/{slug}/high",
            # Carries the replay `video_url` (+ updated ts) as sensor attributes
            # so a Lovelace card / Markdown link can point at the latest clip.
            "json_attributes_topic": f"{self.node}/{slug}/high_attr",
            "unit_of_measurement": "juggles",
            "icon": "mdi:soccer",
            "state_class": "measurement",
            "device": self._device(),
        }
        self.client.publish(topic, json.dumps(payload), retain=True)

    def publish_high(self, name: str, high_score: int, has_clip: bool = False,
                     updated: Optional[float] = None) -> None:
        if not self.enabled:
            return
        slug = _slug(name)
        self.client.publish(f"{self.node}/{slug}/high", high_score, retain=True)
        # Publish the replay attributes. The version query param busts the
        # browser cache each time the clip is overwritten with a new record.
        if has_clip:
            ver = int(updated or time.time())
            url = f"{self.media_base.rstrip('/')}/{slug}.mp4?v={ver}"
            self.client.publish(
                f"{self.node}/{slug}/high_attr",
                json.dumps({"high_score": high_score, "video_url": url,
                            "updated": ver}),
                retain=True,
            )

    def publish_session(self, results: list[dict]) -> None:
        """Publish a summary of the just-processed clip."""
        if not self.enabled:
            return
        payload = {"ts": time.time(), "results": results}
        self.client.publish(f"{self.node}/last_session", json.dumps(payload), retain=True)

    # ------------------------------------------------------------------
    def announce_status(self) -> None:
        """Discovery for the worker-state + processing-progress sensors.

        Both group under the same device as the high-score sensors (matching
        `identifiers`) so a glance at the device shows idle/processing/cooldown
        and a 0-100% progress bar (Gauge card in HA)."""
        if not self.enabled:
            return
        dev = self._device()
        self.client.publish(
            f"{self.prefix}/sensor/{self.node}/worker_state/config",
            json.dumps({
                "name": "Juggle Worker State",
                "unique_id": f"{self.node}_worker_state",
                "state_topic": self.status_topic,
                "value_template": "{{ value_json.state }}",
                "json_attributes_topic": self.status_topic,
                "icon": "mdi:cog-play",
                "availability_topic": self.avail_topic,
                "device": dev,
            }), retain=True)
        self.client.publish(
            f"{self.prefix}/sensor/{self.node}/progress/config",
            json.dumps({
                "name": "Juggle Processing Progress",
                "unique_id": f"{self.node}_progress",
                "state_topic": self.status_topic,
                "value_template": "{{ value_json.progress | default(0) }}",
                "unit_of_measurement": "%",
                "icon": "mdi:progress-clock",
                "state_class": "measurement",
                "availability_topic": self.avail_topic,
                "device": dev,
            }), retain=True)

    def publish_status(self, state: str, progress: Optional[float] = None,
                       current: Optional[str] = None, frame: Optional[int] = None,
                       total: Optional[int] = None,
                       temp_c: Optional[float] = None) -> None:
        """Publish the worker's live state + progress (retained JSON)."""
        if not self.enabled:
            return
        payload: dict = {"state": state, "progress": progress, "ts": time.time()}
        if current is not None:
            payload["clip"] = os.path.basename(current)
        if frame is not None:
            payload["frame"] = frame
        if total:
            payload["total_frames"] = total
        if temp_c is not None:
            payload["temp_c"] = round(temp_c, 1)
        self.client.publish(self.status_topic, json.dumps(payload), retain=True)

    # ------------------------------------------------------------------
    def announce_timing(self) -> None:
        """Discovery for last / average clip processing-time sensors."""
        if not self.enabled:
            return
        dev = self._device()
        common = {
            "device_class": "duration",
            "unit_of_measurement": "s",
            "state_class": "measurement",
            "availability_topic": self.avail_topic,
            "device": dev,
        }
        self.client.publish(
            f"{self.prefix}/sensor/{self.node}/last_process_time/config",
            json.dumps({
                "name": "Juggle Last Process Time",
                "unique_id": f"{self.node}_last_process_time",
                "state_topic": self.timing_topic,
                "value_template": "{{ value_json.last_seconds | default(0) }}",
                "json_attributes_topic": self.timing_topic,
                "icon": "mdi:timer-outline",
                **common,
            }), retain=True)
        self.client.publish(
            f"{self.prefix}/sensor/{self.node}/avg_process_time/config",
            json.dumps({
                "name": "Juggle Average Process Time",
                "unique_id": f"{self.node}_avg_process_time",
                "state_topic": self.timing_topic,
                "value_template": "{{ value_json.avg_seconds | default(0) }}",
                "icon": "mdi:timer-sand",
                **common,
            }), retain=True)

    def publish_timing(self, last_seconds: float, avg_seconds: float,
                       count: int) -> None:
        """Publish last + average clip processing time (retained JSON)."""
        if not self.enabled:
            return
        self.client.publish(
            self.timing_topic,
            json.dumps({
                "last_seconds": last_seconds,
                "avg_seconds": avg_seconds,
                "count": count,
                "ts": time.time(),
            }),
            retain=True,
        )

    def fire_new_high_score(self, name: str, score: int) -> None:
        if not self.enabled:
            return
        self.client.publish(
            f"{self.node}/event/new_high_score",
            json.dumps({"person": name, "score": score, "ts": time.time()}),
        )

    def sync_all(self, people) -> None:
        """(Re)announce and publish every enrolled person's high score.

        ``people`` is an iterable of rows/dicts with ``name``, ``high_score``
        and optional ``high_clip`` / ``high_clip_at`` (as returned by
        ``Database.list_people``)."""
        for p in people:
            name = p["name"]
            self.announce_person(name)
            self.publish_high(
                name,
                int(p["high_score"]),
                has_clip=bool(p["high_clip"]) if "high_clip" in p.keys() else False,
                updated=p["high_clip_at"] if "high_clip_at" in p.keys() else None,
            )

    def close(self) -> None:
        if self.client is not None:
            try:
                self.publish_status("offline")
                self.client.publish(self.avail_topic, "offline", retain=True)
            except Exception:
                pass
            self.client.loop_stop()
            self.client.disconnect()
