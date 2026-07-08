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
        self.client.connect(
            cfg.home_assistant.get("mqtt_host", "127.0.0.1"),
            int(cfg.home_assistant.get("mqtt_port", 1883)),
            keepalive=30,
        )
        self.client.loop_start()

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
            "unit_of_measurement": "juggles",
            "icon": "mdi:soccer",
            "state_class": "measurement",
            "device": self._device(),
        }
        self.client.publish(topic, json.dumps(payload), retain=True)

    def publish_high(self, name: str, high_score: int) -> None:
        if not self.enabled:
            return
        self.client.publish(f"{self.node}/{_slug(name)}/high", high_score, retain=True)

    def publish_session(self, results: list[dict]) -> None:
        """Publish a summary of the just-processed clip."""
        if not self.enabled:
            return
        payload = {"ts": time.time(), "results": results}
        self.client.publish(f"{self.node}/last_session", json.dumps(payload), retain=True)

    def fire_new_high_score(self, name: str, score: int) -> None:
        if not self.enabled:
            return
        self.client.publish(
            f"{self.node}/event/new_high_score",
            json.dumps({"person": name, "score": score, "ts": time.time()}),
        )

    def sync_all(self, high_scores: dict[str, int]) -> None:
        """(Re)announce and publish every enrolled person's high score."""
        for name, score in high_scores.items():
            self.announce_person(name)
            self.publish_high(name, score)

    def close(self) -> None:
        if self.client is not None:
            self.client.loop_stop()
            self.client.disconnect()
