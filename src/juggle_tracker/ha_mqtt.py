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
import sqlite3
import subprocess
import time
from typing import Optional

from . import config as cfgmod
from .db import UNKNOWN_NAME

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
        # Listen for "reset high score" button presses from HA. The button
        # publishes the target person's slug to this shared command topic.
        self.cmd_topic = f"{self.node}/reset/set"
        # Calibration control: editable config numbers + Apply/Revert buttons.
        self.calib_enabled = bool(self.cfg.calibration.get("enabled", True))
        self.apply_topic = f"{self.node}/apply/set"
        self.revert_topic = f"{self.node}/revert/set"
        self.client.on_message = self._on_message
        self.client.subscribe(self.cmd_topic)
        if self.calib_enabled:
            self.client.subscribe(f"{self.node}/config/+/set")
            self.client.subscribe(self.apply_topic)
            self.client.subscribe(self.revert_topic)
            self.announce_calibration()

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
            "icon": "mdi:help-circle-outline" if name == UNKNOWN_NAME else "mdi:soccer",
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
        # Always publish the attributes so `video_url` is present on every
        # profile — a non-empty string when a replay clip exists, else "". This
        # lets HA cards show/hide strictly on replay presence
        # (attribute video_url != ""). The version query param busts the browser
        # cache each time the clip is overwritten with a new record.
        if has_clip:
            ver = int(updated or time.time())
            url = f"{self.media_base.rstrip('/')}/{slug}.mp4?v={ver}"
        else:
            ver = int(updated) if updated else 0
            url = ""
        self.client.publish(
            f"{self.node}/{slug}/high_attr",
            json.dumps({"high_score": high_score, "video_url": url,
                        "updated": ver}),
            retain=True,
        )

    def announce_reset_button(self, name: str) -> None:
        """Publish MQTT discovery for a per-person 'reset high score' button."""
        if not self.enabled:
            return
        slug = _slug(name)
        topic = f"{self.prefix}/button/{self.node}/{slug}_reset/config"
        payload = {
            "name": f"Reset {name} High Score",
            "unique_id": f"{self.node}_{slug}_reset",
            "command_topic": self.cmd_topic,
            "payload_press": slug,          # tells the worker which person to reset
            "icon": "mdi:trophy-broken",
            "availability_topic": self.avail_topic,
            "device": self._device(),
        }
        self.client.publish(topic, json.dumps(payload), retain=True)

    def _on_message(self, client, userdata, msg) -> None:
        """Route inbound MQTT commands (reset / calibration set / apply / revert)."""
        try:
            topic = msg.topic
            payload = msg.payload.decode().strip()
            if topic == self.cmd_topic:
                if payload:
                    self._reset_person(payload)
            elif not self.calib_enabled:
                return
            elif topic == self.apply_topic:
                self._apply_and_restart()
            elif topic == self.revert_topic:
                self._revert_calibration()
            else:
                # {node}/config/{slug}/set
                parts = topic.split("/")
                if len(parts) == 4 and parts[1] == "config" and parts[3] == "set":
                    self._handle_config_set(parts[2], payload)
        except Exception as exc:  # never let a bad message kill the loop
            print(f"  [mqtt] error handling {msg.topic}: {exc}", flush=True)

    def _reset_person(self, slug: str) -> None:
        """Zero a person's high score, delete their replay clip, and re-publish.

        Runs in the MQTT network thread, so it uses its own short-lived SQLite
        connection rather than sharing the pipeline's."""
        name = None
        pid = None
        try:
            conn = sqlite3.connect(self.cfg.database.path, timeout=5)
            try:
                conn.row_factory = sqlite3.Row
                for r in conn.execute("SELECT id, name FROM people"):
                    if _slug(r["name"]) == slug:
                        pid, name = int(r["id"]), r["name"]
                        break
                if pid is None:
                    print(f"  [reset] no person matches slug '{slug}'", flush=True)
                    return
                conn.execute(
                    "UPDATE people SET high_score = 0, high_clip = NULL, "
                    "high_clip_at = NULL WHERE id = ?", (pid,)
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            print(f"  [reset] DB update failed for '{slug}': {exc}", flush=True)
            return
        # Delete the replay clip so the iframe/link disappears.
        hs_dir = self.cfg.capture.get("highscore_dir", "highscores")
        try:
            os.remove(os.path.join(hs_dir, f"{slug}.mp4"))
        except OSError:
            pass
        # Re-publish retained state (0) + empty video_url so HA updates now.
        self.publish_high(name, 0, has_clip=False)
        print(f"  [reset] {name}: high score cleared and replay removed",
              flush=True)

    # ------------------------------------------------------------------
    def announce_calibration(self) -> None:
        """Discovery for editable calibration `number` entities + Apply/Revert
        buttons, seeded from the current (merged) config. All grouped under the
        device with entity_category 'config' so they tuck into the device's
        configuration section."""
        if not self.enabled:
            return
        dev = self._device()
        for spec in cfgmod.TUNABLE_PARAMS:
            slug = spec["slug"]
            self.client.publish(
                f"{self.prefix}/number/{self.node}/cfg_{slug}/config",
                json.dumps({
                    "name": f"Juggle {spec['name']}",
                    "unique_id": f"{self.node}_cfg_{slug}",
                    "state_topic": f"{self.node}/config/{slug}",
                    "command_topic": f"{self.node}/config/{slug}/set",
                    "min": spec["min"], "max": spec["max"], "step": spec["step"],
                    "mode": "box",
                    "icon": spec["icon"],
                    "entity_category": "config",
                    "availability_topic": self.avail_topic,
                    "device": dev,
                }), retain=True)
            # Seed the current value so HA shows what actually produced results.
            val = cfgmod.get_by_path(self.cfg.raw, spec["path"], spec["min"])
            self.client.publish(f"{self.node}/config/{slug}", val, retain=True)
        # Apply & Restart button.
        self.client.publish(
            f"{self.prefix}/button/{self.node}/apply_restart/config",
            json.dumps({
                "name": "Apply Calibration & Restart",
                "unique_id": f"{self.node}_apply_restart",
                "command_topic": self.apply_topic,
                "payload_press": "apply",
                "icon": "mdi:restart",
                "entity_category": "config",
                "availability_topic": self.avail_topic,
                "device": dev,
            }), retain=True)
        # Revert-to-defaults button.
        self.client.publish(
            f"{self.prefix}/button/{self.node}/revert_defaults/config",
            json.dumps({
                "name": "Revert Calibration to Defaults",
                "unique_id": f"{self.node}_revert_defaults",
                "command_topic": self.revert_topic,
                "payload_press": "revert",
                "icon": "mdi:backup-restore",
                "entity_category": "config",
                "availability_topic": self.avail_topic,
                "device": dev,
            }), retain=True)

    def _handle_config_set(self, slug: str, payload: str) -> None:
        """Persist an edited calibration value to the overrides file (no restart
        — it activates on the next Apply & Restart)."""
        spec = cfgmod.param_for_slug(slug)
        if spec is None:
            print(f"  [calib] unknown param '{slug}'", flush=True)
            return
        try:
            value = cfgmod.clamp_param(spec, float(payload))
        except (TypeError, ValueError):
            print(f"  [calib] bad value '{payload}' for {slug}", flush=True)
            return
        try:
            cfgmod.set_override(self.cfg.overrides_path, spec["path"], value)
        except Exception as exc:
            print(f"  [calib] failed to save override for {slug}: {exc}",
                  flush=True)
            return
        # Reflect the accepted (clamped) value back to HA.
        self.client.publish(f"{self.node}/config/{slug}", value, retain=True)
        print(f"  [calib] {slug} -> {value} (saved; press Apply & Restart to "
              f"activate)", flush=True)

    def _apply_and_restart(self) -> None:
        print("  [calib] Apply pressed; restarting worker to load new config...",
              flush=True)
        self._restart_service()

    def _revert_calibration(self) -> None:
        removed = cfgmod.clear_overrides(self.cfg.overrides_path)
        print(f"  [calib] revert to defaults "
              f"({'removed overrides' if removed else 'no overrides file'}); "
              f"restarting...", flush=True)
        self._restart_service()

    def _restart_service(self) -> None:
        """Restart the worker so config (incl. overrides) is reloaded. Falls
        back to process exit (systemd Restart=always brings it back)."""
        svc = self.cfg.calibration.get("service_name", "juggle-tracker.service")
        try:
            self.client.publish(self.avail_topic, "offline", retain=True)
        except Exception:
            pass
        try:
            subprocess.Popen(["systemctl", "--user", "restart", svc])
        except Exception as exc:
            print(f"  [calib] 'systemctl --user restart {svc}' failed ({exc}); "
                  f"exiting so systemd Restart=always relaunches", flush=True)
            os._exit(0)

    # ------------------------------------------------------------------
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
            self.announce_reset_button(name)
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
