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
import shutil
import sqlite3
import subprocess
import time
from typing import Optional
from urllib.parse import quote

from . import config as cfgmod
from .db import Database, UNKNOWN_NAME

try:
    import paho.mqtt.client as mqtt
except Exception:  # pragma: no cover - optional at import time
    mqtt = None


# Placeholder option for the reprocess dropdown meaning "nothing selected".
# Always kept as the first option so there's a valid state to reset to after a
# reprocess requeues (and thus de-lists) the chosen file — otherwise HA would
# keep showing the now-missing filename as selected.
SELECT_NONE = "(none)"


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", name.strip().lower()).strip("_")


def _event_id_from_filename(filename: str) -> Optional[str]:
    stem = os.path.splitext(os.path.basename(filename))[0]
    parts = stem.rsplit("_", 1)
    if len(parts) == 2 and parts[0].startswith("clip_"):
        return parts[1]
    return None


def _build_reprocess_options(
    filenames: list[str], attributions: dict[str, list[str]],
) -> tuple[list[str], dict[str, str], dict[str, str]]:
    """Build selector labels and maps between each label and its filename."""
    options = [SELECT_NONE]
    option_to_filename: dict[str, str] = {}
    filename_to_option: dict[str, str] = {}
    for filename in filenames:
        if filename not in attributions:
            person = "no session"
        elif attributions[filename]:
            person = ", ".join(attributions[filename])
        else:
            person = "no juggle attempts"
        option = f"{person} | {filename}"
        options.append(option)
        option_to_filename[option] = filename
        filename_to_option[filename] = option
    return options, option_to_filename, filename_to_option


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
        self._last_processed_event_id: Optional[str] = None
        self._last_processed_payload: Optional[dict] = None
        self._recent_faceid_attribution: Optional[tuple[str, str]] = None
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
        # Inbox/processed queue sensors + reprocess-a-clip control.
        self.reprocess_topic = f"{self.node}/reprocess/set"
        self.reprocess_select_topic = f"{self.node}/reprocess_select/set"
        # Global annotation switch: its live state is consulted for every clip
        # the inbox watcher starts processing, including reprocesses.
        self.annotate_processed_topic = f"{self.node}/annotate_processed/set"
        self.annotate_processed = bool(self.cfg.capture.get(
            "annotate_processed_default",
            self.cfg.capture.get("reprocess_annotate_default", False),
        ))
        self._reprocess_selected: Optional[str] = None
        self._last_select_options: Optional[list] = None
        self._reprocess_option_to_filename: dict[str, str] = {}
        self._reprocess_filename_to_option: dict[str, str] = {}
        self._reprocess_attributions: dict[str, list[str]] = {}
        # Delete-a-clip-from-the-inbox control. Deleting the clip that is
        # currently being processed also cancels the in-flight run.
        self.inbox_select_topic = f"{self.node}/inbox_select/set"
        self.delete_topic = f"{self.node}/delete/set"
        self._inbox_selected: Optional[str] = None
        self._last_inbox_options: Optional[list] = None
        # Back-reference to the Pipeline (set via bind_worker); used to cancel
        # the in-flight clip when the operator deletes the file being processed.
        self._worker = None
        self.client.subscribe(self.reprocess_topic)
        self.client.subscribe(self.reprocess_select_topic)
        self.client.subscribe(self.annotate_processed_topic)
        self.client.subscribe(self.inbox_select_topic)
        self.client.subscribe(self.delete_topic)
        self.announce_queues()
        self.publish_queues()
        self._retire_reassign_entities()
        # FaceID async re-attribution: subscribe to Frigate sub_label updates.
        # FaceID (HACS) publishes to frigate/<camera>/person/<event_id> with
        # a JSON payload containing a "sub_label" field (the recognised name).
        # Topic pattern: frigate/+/person/+ (wildcard camera + event_id).
        # Disabled by default; set home_assistant.faceid_enabled: true in config
        # to activate. This allows testing without FaceID installed.
        self._faceid_enabled = bool(
            cfg.home_assistant.get("faceid_enabled", False)
        )
        # Pending FaceID attributions: event_id -> person_name.
        # Populated when a sub_label message arrives before pipeline.process()
        # has finished (or even started) writing attempts for that session.
        # Consumed by pipeline.process() at the end of each clip via
        # consume_pending_faceid(), which applies the attribution before HA
        # scores are published so the correct person is credited immediately.
        self._pending_faceid: dict[str, str] = {}
        if self._faceid_enabled:
            self.client.subscribe("frigate/+/person/+", qos=1)
            print("  [faceid] subscribed to frigate/+/person/+ sub_label events",
                  flush=True)

    # ------------------------------------------------------------------
    def _device(self) -> dict:
        return {
            "identifiers": [self.node],
            "name": "Soccer Juggle Tracker",
            "model": "RLC-810A CV pipeline",
            "manufacturer": "DIY",
        }

    def bind_worker(self, worker) -> None:
        """Attach the Pipeline so inbound commands can reach into a running clip
        (used by the inbox-delete handler to cancel the in-flight file)."""
        self._worker = worker
        self.publish_queues()

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
            elif topic == self.reprocess_topic:
                self._do_reprocess()
            elif topic == self.reprocess_select_topic:
                filename = self._reprocess_option_to_filename.get(payload)
                if filename is None and payload in self._reprocess_filename_to_option:
                    filename = payload  # Accept retained filename values from older clients.
                self._reprocess_selected = filename
                self.client.publish(f"{self.node}/reprocess_select",
                                    self._reprocess_filename_to_option.get(
                                        filename, SELECT_NONE), retain=True)
                self._update_reprocess_preview()
            elif topic == self.annotate_processed_topic:
                self.annotate_processed = payload.upper() in ("ON", "1", "TRUE")
                self.client.publish(
                    f"{self.node}/annotate_processed",
                    "ON" if self.annotate_processed else "OFF", retain=True)
            elif topic == self.inbox_select_topic:
                self._inbox_selected = (
                    None if (not payload or payload == SELECT_NONE) else payload
                )
                self.client.publish(f"{self.node}/inbox_select",
                                    payload or SELECT_NONE, retain=True)
            elif topic == self.delete_topic:
                self._do_delete_inbox()
            elif self._faceid_enabled and topic.startswith("frigate/") and "/person/" in topic:
                # FaceID sub_label event: frigate/<camera>/person/<event_id>
                self._on_faceid_event(topic, msg.payload)
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
    def _on_faceid_event(self, topic: str, raw_payload: bytes) -> None:
        """Handle a FaceID sub_label event from Frigate.

        FaceID (HACS integration) publishes to:
            frigate/<camera>/person/<event_id>
        with a JSON payload like:
            {"sub_label": "Kid1", "score": 0.87, ...}

        When a sub_label arrives for an event we already processed (and bucketed
        to Unknown Juggler), we re-attribute that session's attempts to the named
        person and republish scores to HA.

        Runs in the MQTT network thread — uses its own SQLite connection."""
        # Parse topic: frigate/<camera>/person/<event_id>
        parts = topic.split("/")
        if len(parts) != 4:
            return
        event_id = parts[3]

        try:
            payload = json.loads(raw_payload)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return

        person_name = payload.get("sub_label", "").strip()
        if not person_name:
            # FaceID fired but couldn't identify anyone — ignore.
            return

        print(f"  [faceid] event {event_id} identified as '{person_name}'",
              flush=True)

        try:
            db = Database(self.cfg.database.path)
            try:
                db.remember_faceid_label(event_id, person_name)
                result = db.reassign_session_by_event(event_id, person_name)
                if result is None:
                    # reassign_session_by_event returns None for two distinct
                    # reasons:
                    #   a) The clip is currently in-flight or queued — the
                    #      session/attempts haven't been written yet.  Park the
                    #      attribution; pipeline.process() will apply it at the
                    #      end of the clip via consume_pending_faceid().
                    #   b) Nothing is queued for this event_id.  The clip was
                    #      either never received, already fully processed with no
                    #      Unknown attempts remaining, or the event_id is
                    #      unrecognised.  Parking would leave the entry in
                    #      _pending_faceid forever, so we drop it instead.
                    queued = (
                        self._worker is not None
                        and self._worker.is_event_queued(event_id)
                    )
                    if queued:
                        self._pending_faceid[event_id] = person_name
                        print(
                            f"  [faceid] event {event_id}: clip is queued/in-flight; "
                            f"stored pending attribution for '{person_name}'",
                            flush=True,
                        )
                    else:
                        print(
                            f"  [faceid] event {event_id}: no queued clip and no "
                            f"Unknown attempts found; dropping attribution for "
                            f"'{person_name}'",
                            flush=True,
                        )
                    return
                print(
                    f"  [faceid] re-attributed {result['moved_count']} attempt(s) "
                    f"(best: {result['best_count']}) to '{person_name}' "
                    f"[session {result['session_id']}]",
                    flush=True,
                )
                self.note_last_processed_attribution(event_id, person_name)
                # Republish updated high scores for all people so HA reflects the change.
                people = db.list_people()
            finally:
                db.close()
        except Exception as exc:
            print(f"  [faceid] re-attribution failed for event {event_id}: {exc}",
                  flush=True)
            return

        self.sync_all(people)
        if result.get("new_high"):
            self.fire_new_high_score(person_name, result["best_count"])

    def consume_pending_faceid(self, event_id: Optional[str]) -> Optional[str]:
        """Pop and return a pending FaceID person_name for this event_id, or None.

        Called by ``pipeline.process()`` at the end of a clip, after attempts
        have been committed to the DB. If FaceID fired early (before the clip
        finished processing), the person_name was stored here; the pipeline
        applies the attribution and publishes scores under the correct name
        instead of Unknown Juggler."""
        if not event_id:
            return None
        return self._pending_faceid.pop(event_id, None)

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
        # Ball-fallback ON/OFF switch (boolean — not a numeric `number` entity).
        # Lives in the same config section and honors the Apply & Restart flow.
        fb_slug = cfgmod.BALL_FALLBACK_ENABLED_SLUG
        self.client.publish(
            f"{self.prefix}/switch/{self.node}/cfg_{fb_slug}/config",
            json.dumps({
                "name": "Juggle Ball CV Fallback",
                "unique_id": f"{self.node}_cfg_{fb_slug}",
                "state_topic": f"{self.node}/config/{fb_slug}",
                "command_topic": f"{self.node}/config/{fb_slug}/set",
                "payload_on": "ON", "payload_off": "OFF",
                "icon": "mdi:circle-double",
                "entity_category": "config",
                "availability_topic": self.avail_topic,
                "device": dev,
            }), retain=True)
        fb_on = bool(cfgmod.get_by_path(
            self.cfg.raw, cfgmod.BALL_FALLBACK_ENABLED_PATH, False))
        self.client.publish(f"{self.node}/config/{fb_slug}",
                            "ON" if fb_on else "OFF", retain=True)
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
        # Boolean ball-fallback switch: not in TUNABLE_PARAMS, handle explicitly.
        if slug == cfgmod.BALL_FALLBACK_ENABLED_SLUG:
            on = payload.strip().upper() in ("ON", "1", "TRUE")
            try:
                cfgmod.set_override(
                    self.cfg.overrides_path,
                    cfgmod.BALL_FALLBACK_ENABLED_PATH, on)
            except Exception as exc:
                print(f"  [calib] failed to save ball_fallback.enabled: {exc}",
                      flush=True)
                return
            self.client.publish(f"{self.node}/config/{slug}",
                                "ON" if on else "OFF", retain=True)
            print(f"  [calib] ball_fallback.enabled -> {on} (saved; press "
                  f"Apply & Restart to activate)", flush=True)
            return
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
    def _list_videos(self, directory: Optional[str]) -> list[str]:
        """Basenames of video files in a dir, newest first."""
        exts = (".mp4", ".mkv", ".mov", ".avi")
        if not directory or not os.path.isdir(directory):
            return []
        items = []
        for name in os.listdir(directory):
            if name.lower().endswith(exts):
                p = os.path.join(directory, name)
                try:
                    items.append((os.path.getmtime(p), name))
                except OSError:
                    continue
        items.sort(reverse=True)
        return [n for _, n in items]

    def announce_queues(self) -> None:
        """Discovery for the inbox-queue + processed-count sensors and the
        reprocess select + button."""
        if not self.enabled:
            return
        dev = self._device()
        self.client.publish(
            f"{self.prefix}/sensor/{self.node}/inbox_queue/config",
            json.dumps({
                "name": "Juggle Inbox Queue",
                "unique_id": f"{self.node}_inbox_queue",
                "default_entity_id": "sensor.juggle_inbox_queue",
                "state_topic": f"{self.node}/inbox",
                "value_template": "{{ value_json.count }}",
                "json_attributes_topic": f"{self.node}/inbox",
                "unit_of_measurement": "clips",
                "icon": "mdi:tray-full",
                "availability_topic": self.avail_topic,
                "device": dev,
            }), retain=True)
        self.client.publish(
            f"{self.prefix}/sensor/{self.node}/processed_count/config",
            json.dumps({
                "name": "Juggle Processed Count",
                "unique_id": f"{self.node}_processed_count",
                "default_entity_id": "sensor.juggle_processed_count",
                "state_topic": f"{self.node}/processed",
                "value_template": "{{ value_json.count }}",
                "json_attributes_topic": f"{self.node}/processed",
                "unit_of_measurement": "clips",
                "icon": "mdi:tray-arrow-down",
                "availability_topic": self.avail_topic,
                "device": dev,
            }), retain=True)
        self.client.publish(
            f"{self.prefix}/button/{self.node}/reprocess/config",
            json.dumps({
                "name": "Reprocess Selected Clip",
                "unique_id": f"{self.node}_reprocess",
                "default_entity_id": "button.reprocess_selected_clip",
                "command_topic": self.reprocess_topic,
                "payload_press": "reprocess",
                "icon": "mdi:reload",
                "entity_category": "config",
                "availability_topic": self.avail_topic,
                "device": dev,
            }), retain=True)
        # Retire the previous reprocess-only switch discovery entity.
        self.client.publish(
            f"{self.prefix}/switch/{self.node}/reprocess_annotate/config",
            "", retain=True)
        # Global annotation switch for every processed clip.
        self.client.publish(
            f"{self.prefix}/switch/{self.node}/annotate_processed/config",
            json.dumps({
                "name": "Annotate Processed Clips",
                "unique_id": f"{self.node}_annotate_processed",
                "default_entity_id": "switch.juggle_annotate_processed_clips",
                "state_topic": f"{self.node}/annotate_processed",
                "command_topic": self.annotate_processed_topic,
                "payload_on": "ON", "payload_off": "OFF",
                "icon": "mdi:draw",
                "entity_category": "config",
                "availability_topic": self.avail_topic,
                "device": dev,
            }), retain=True)
        # Seed the global toggle state so HA reflects the worker's current
        # configured value after a restart.
        self.client.publish(
            f"{self.node}/annotate_processed",
            "ON" if self.annotate_processed else "OFF", retain=True)
        # Sensor exposing the most recent annotated clip replay
        # (name in state, video_url in attributes). We deliberately do NOT seed
        # an empty value here, so a prior replay URL (retained on the broker)
        # survives a worker restart.
        self.client.publish(
            f"{self.prefix}/sensor/{self.node}/last_reprocessed/config",
            json.dumps({
                "name": "Juggle Last Annotated Clip",
                "unique_id": f"{self.node}_last_reprocessed",
                "state_topic": f"{self.node}/last_reprocessed",
                "value_template": "{{ value_json.name | default('none') }}",
                "json_attributes_topic": f"{self.node}/last_reprocessed",
                "icon": "mdi:movie-open-play",
                "availability_topic": self.avail_topic,
                "device": dev,
            }), retain=True)
        # Start the reprocess dropdown with a clean (nothing-selected) state.
        self.client.publish(f"{self.node}/reprocess_select", SELECT_NONE,
                            retain=True)
        # Delete-selected-inbox-clip button (destructive — the HA card should
        # attach a tap confirmation). If the selected clip is the one currently
        # being processed, this also cancels the in-flight run.
        self.client.publish(
            f"{self.prefix}/button/{self.node}/delete_inbox/config",
            json.dumps({
                "name": "Delete Selected Inbox Clip",
                "unique_id": f"{self.node}_delete_inbox",
                "command_topic": self.delete_topic,
                "payload_press": "delete",
                "icon": "mdi:trash-can",
                "entity_category": "config",
                "availability_topic": self.avail_topic,
                "device": dev,
            }), retain=True)
        # Start the inbox dropdown with a clean (nothing-selected) state.
        self.client.publish(f"{self.node}/inbox_select", SELECT_NONE,
                            retain=True)
        # Retire the previous thumbnail grab-time control.
        self.client.publish(
            f"{self.prefix}/number/{self.node}/thumb_seconds/config", "", retain=True)
        # Playable preview sensor for the selected reprocess clip.
        self.client.publish(
            f"{self.prefix}/sensor/{self.node}/reprocess_thumb/config",
            json.dumps({
                "name": "Juggle Reprocess Preview",
                "unique_id": f"{self.node}_reprocess_thumb",
                "default_entity_id": "sensor.juggle_reprocess_preview",
                "state_topic": f"{self.node}/reprocess_thumb",
                "value_template": "{{ value_json.name | default('none') }}",
                "json_attributes_topic": f"{self.node}/reprocess_thumb",
                "icon": "mdi:movie-open-play",
                "availability_topic": self.avail_topic,
                "device": dev,
            }), retain=True)
        self.client.publish(
            f"{self.node}/reprocess_thumb",
            json.dumps({"name": "none", "video_url": "",
                        "attributed_to": ""}), retain=True)
        # "Last processed clip" sensor — updated after EVERY clip (initial or
        # reprocess) so you can watch a queue drain. The video_url attribute is
        # populated only when the clip is browser-playable H.264 (a cheap copy,
        # no transcode); HEVC clips still report name + juggle counts.
        self.client.publish(
            f"{self.prefix}/sensor/{self.node}/last_processed/config",
            json.dumps({
                "name": "Juggle Last Processed",
                "unique_id": f"{self.node}_last_processed",
                "state_topic": f"{self.node}/last_processed",
                "value_template": "{{ value_json.name | default('none') }}",
                "json_attributes_topic": f"{self.node}/last_processed",
                "icon": "mdi:filmstrip",
                "availability_topic": self.avail_topic,
                "device": dev,
            }), retain=True)
        self.client.publish(
            f"{self.prefix}/sensor/{self.node}/last_processed_person/config",
            json.dumps({
                "name": "Juggle Last Processed Person",
                "unique_id": f"{self.node}_last_processed_person",
                "state_topic": f"{self.node}/last_processed",
                "value_template": "{{ value_json.attributed_to | default('Unknown Juggler') }}",
                "icon": "mdi:account-check",
                "availability_topic": self.avail_topic,
                "device": dev,
            }), retain=True)

    def _announce_reprocess_select(self, options: list) -> None:
        """(Re)publish the reprocess `select` discovery with current options."""
        self.client.publish(
            f"{self.prefix}/select/{self.node}/reprocess_file/config",
            json.dumps({
                "name": "Juggle Reprocess File",
                "unique_id": f"{self.node}_reprocess_file",
                "default_entity_id": "select.juggle_reprocess_file",
                "state_topic": f"{self.node}/reprocess_select",
                "command_topic": self.reprocess_select_topic,
                "options": options,
                "icon": "mdi:file-refresh",
                "entity_category": "config",
                "availability_topic": self.avail_topic,
                "device": self._device(),
            }), retain=True)

    def _announce_inbox_select(self, options: list) -> None:
        """(Re)publish the inbox-delete `select` discovery with current options."""
        self.client.publish(
            f"{self.prefix}/select/{self.node}/inbox_file/config",
            json.dumps({
                "name": "Juggle Inbox File",
                "unique_id": f"{self.node}_inbox_file",
                "state_topic": f"{self.node}/inbox_select",
                "command_topic": self.inbox_select_topic,
                "options": options,
                "icon": "mdi:file-remove",
                "entity_category": "config",
                "availability_topic": self.avail_topic,
                "device": self._device(),
            }), retain=True)

    def publish_queues(self) -> None:
        """Publish inbox + processed listings and refresh the reprocess select.

        Called by the watcher each cycle. State is a count; the filenames ride
        as a `files` attribute (capped to keep the MQTT payload small)."""
        if not self.enabled:
            return
        infiles = self._list_videos(self.cfg.capture.get("inbox_dir"))
        pfiles = self._list_videos(self.cfg.capture.get("processed_dir"))
        self.client.publish(
            f"{self.node}/inbox",
            json.dumps({"count": len(infiles), "files": infiles[:100]}),
            retain=True)
        self.client.publish(
            f"{self.node}/processed",
            json.dumps({"count": len(pfiles), "files": pfiles[:100]}),
            retain=True)
        # Keep the reprocess dropdown in sync. Always include the "(none)"
        # placeholder as the first option so there's a valid "nothing selected"
        # state to fall back to after a reprocess requeues the chosen file.
        attributions = (
            Database.clip_attributions_from_path(
                self.cfg.database.path, pfiles[:50]
            ) if self._worker is not None else {}
        )
        opts, option_to_filename, filename_to_option = _build_reprocess_options(
            pfiles[:50], attributions)
        self._reprocess_option_to_filename = option_to_filename
        self._reprocess_filename_to_option = filename_to_option
        self._reprocess_attributions = attributions
        if opts != self._last_select_options:
            self._announce_reprocess_select(opts)
            self._last_select_options = opts
            if self._reprocess_selected:
                selected_option = filename_to_option.get(self._reprocess_selected)
                if selected_option is None:
                    self._reprocess_selected = None
                    self._update_reprocess_preview()
                    selected_option = SELECT_NONE
                self.client.publish(f"{self.node}/reprocess_select",
                                    selected_option, retain=True)
        # Keep the inbox-delete dropdown in sync with the live inbox contents.
        in_opts = [SELECT_NONE] + infiles[:50]
        if in_opts != self._last_inbox_options:
            self._announce_inbox_select(in_opts)
            self._last_inbox_options = in_opts

    def _do_reprocess(self) -> None:
        """Copy the selected processed clip into the inbox so the watcher re-runs
        it end-to-end with the current config.

        The clip is COPIED (not moved), so the original stays safe in the
        processed folder — deleting or cancelling the inbox copy can never lose
        the clip. On completion the watcher discards the throwaway inbox copy
        (the archived original is authoritative)."""
        sel = self._reprocess_selected
        if not sel or sel == SELECT_NONE:
            print("  [reprocess] no clip selected", flush=True)
            return
        name = os.path.basename(sel)  # guard against path traversal
        processed = self.cfg.capture.get("processed_dir")
        inbox = self.cfg.capture.get("inbox_dir")
        src = os.path.join(processed, name)
        if not os.path.isfile(src):
            print(f"  [reprocess] '{name}' not found in processed", flush=True)
            return
        event_id = _event_id_from_filename(name)
        pending_label = None
        if event_id:
            try:
                db = Database(self.cfg.database.path)
                try:
                    pending_label = db.faceid_label(event_id)
                finally:
                    db.close()
            except Exception as exc:
                print(f"  [reprocess] FaceID label lookup failed for {event_id}: "
                      f"{exc}", flush=True)
        try:
            if pending_label and event_id:
                self._pending_faceid[event_id] = pending_label
            os.makedirs(inbox, exist_ok=True)
            dst = os.path.join(inbox, name)
            shutil.copy2(src, dst)
            note = ""
            if self.annotate_processed:
                note = " (annotated replay enabled)"
            print(f"  [reprocess] {name} -> inbox{note}; will re-run with "
                  f"current config", flush=True)
            self.publish_queues()  # reflect the move immediately
            # Clear the last-reprocessed replay for this new run so a card
            # guarded on `video_url != ""` hides while the run is in flight
            # (annotated) or stays hidden for a non-annotated run (which
            # produces no replay). publish_last_reprocessed() restores a real
            # URL when an annotated run finishes.
            self.publish_reprocess_pending(name, self.annotate_processed)
            # Reset the dropdown to "(none)" so the UI doesn't keep showing the
            # now-requeued (and no-longer-listed) file as the selection.
            self._reprocess_selected = None
            self.client.publish(f"{self.node}/reprocess_select", SELECT_NONE,
                                retain=True)
            self._update_reprocess_preview()  # clear the stale preview
        except Exception as exc:
            if (pending_label and event_id
                    and self._pending_faceid.get(event_id) == pending_label):
                self._pending_faceid.pop(event_id, None)
            print(f"  [reprocess] failed to requeue '{name}': {exc}", flush=True)

    def _do_delete_inbox(self) -> None:
        """Delete the selected inbox clip.

        If it is the clip currently being processed, ask the worker to cancel
        the in-flight run — the watcher then removes the file once the loop
        aborts. Otherwise the queued clip is removed here immediately."""
        sel = self._inbox_selected
        if not sel or sel == SELECT_NONE:
            print("  [delete] no inbox clip selected", flush=True)
            return
        name = os.path.basename(sel)  # guard against path traversal
        inbox = self.cfg.capture.get("inbox_dir")

        cancelled = False
        if self._worker is not None:
            try:
                cancelled = bool(self._worker.request_cancel(name))
            except Exception as exc:
                print(f"  [delete] cancel request failed: {exc}", flush=True)

        if cancelled:
            # In flight: the frame loop is still reading the file, so let the
            # watcher delete it (+ partial temp) after it aborts.
            print(f"  [delete] {name} is processing -> requested cancel",
                  flush=True)
        else:
            removed = False
            for p in (os.path.join(inbox, name),
                      os.path.join(inbox, name + ".annotate")):
                try:
                    os.remove(p)
                    if not p.endswith(".annotate"):
                        removed = True
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    print(f"  [delete] failed to remove {p}: {exc}", flush=True)
            print(f"  [delete] {'removed queued clip' if removed else 'clip not found'}"
                  f": {name}", flush=True)

        self.publish_queues()  # reflect the removal immediately
        self._inbox_selected = None
        self.client.publish(f"{self.node}/inbox_select", SELECT_NONE,
                            retain=True)

    # ------------------------------------------------------------------
    def _retire_reassign_entities(self) -> None:
        """Remove discovery configs for the retired high-score reassignment UI."""
        for component, object_id in (
            ("button", "reassign"),
            ("select", "reassign_source"),
            ("select", "reassign_target"),
        ):
            self.client.publish(
                f"{self.prefix}/{component}/{self.node}/{object_id}/config",
                "", retain=True,
            )
        for key in ("reassign_source", "reassign_target"):
            self.client.publish(f"{self.node}/{key}", "", retain=True)

    @staticmethod
    def _probe_codec(path: str) -> Optional[str]:
        """Return the first video stream's codec, or None if probing fails."""
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=codec_name",
                 "-of", "default=noprint_wrappers=1:nokey=1", path],
                check=True, capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        codecs = result.stdout.strip().splitlines()
        return codecs[0].strip().lower() if codecs else None

    def _update_reprocess_preview(self) -> None:
        """Publish a browser-playable preview of the selected archived clip."""
        if not self.enabled:
            return

        def _clear() -> None:
            self.client.publish(
                f"{self.node}/reprocess_thumb",
                json.dumps({"name": "none", "video_url": "",
                            "attributed_to": ""}), retain=True)

        sel = self._reprocess_selected
        if not sel or sel == SELECT_NONE:
            _clear()
            return
        name = os.path.basename(sel)
        processed = self.cfg.capture.get("processed_dir")
        src = os.path.join(processed or "", name)
        if not os.path.isfile(src):
            _clear()
            return

        codec = self._probe_codec(src)
        ver = time.time_ns()
        attributed_to = ", ".join(self._reprocess_attributions.get(name, []))
        if codec == "h264" and name.lower().endswith(".mp4"):
            media_root = self.media_base.rstrip("/").rsplit("/", 1)[0]
            url = f"{media_root}/processed/{quote(name, safe='')}?v={ver}"
        else:
            hs_dir = self.cfg.capture.get("highscore_dir", "highscores")
            dst = os.path.join(hs_dir, "reprocess_preview.mp4")
            tmp = dst + ".part"
            try:
                os.makedirs(hs_dir, exist_ok=True)
                cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", src,
                       "-map", "0:v:0", "-an"]
                if codec == "h264":
                    cmd += ["-c:v", "copy"]
                else:
                    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                            "-pix_fmt", "yuv420p"]
                cmd += ["-movflags", "+faststart", "-f", "mp4", tmp]
                subprocess.run(cmd, check=True)
                os.replace(tmp, dst)
            except Exception as exc:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                print(f"  [preview] failed for {name}: {exc}", flush=True)
                _clear()
                return
            url = f"{self.media_base.rstrip('/')}/reprocess_preview.mp4?v={ver}"

        payload = {"name": name, "video_url": url,
                   "attributed_to": attributed_to, "ts": ver}
        self.client.publish(
            f"{self.node}/reprocess_thumb", json.dumps(payload), retain=True)
        print(f"  [preview] {name} -> {url}", flush=True)

    def publish_reprocess_pending(self, clip_name: str,
                                  annotated: bool) -> None:
        """Announce that a reprocess was just queued, clearing the replay URL.

        Published with an empty ``video_url`` (retained) so an HA card guarded on
        ``video_url != ""`` hides while an annotated run is in flight, and stays
        hidden for a non-annotated run (which produces no replay — so the last
        reprocess no longer misrepresents itself with a stale clip).
        :meth:`publish_last_reprocessed` restores a real URL when an annotated
        run completes. Keeping ``video_url`` always present (``""`` or a URL)
        after the first reprocess is what makes the attribute-based visibility
        guard reliable."""
        if not self.enabled:
            return
        self.client.publish(
            f"{self.node}/last_reprocessed",
            json.dumps({"name": os.path.basename(clip_name), "video_url": "",
                        "annotated": annotated, "processing": annotated,
                        "ts": int(time.time())}),
            retain=True)

    def publish_last_reprocessed(self, clip_name: str,
                                 ts: Optional[float] = None,
                                 annotated: bool = True) -> None:
        """Publish the URL of the most recent annotated clip replay.

        One overwritten file (<highscore_dir>/last_reprocessed.mp4) served by HA
        at /local/juggle/last_reprocessed.mp4. The ``?v=`` cache-buster forces
        the browser to reload the overwritten file each time."""
        if not self.enabled:
            return
        ver = int(ts or time.time())
        url = f"{self.media_base.rstrip('/')}/last_reprocessed.mp4?v={ver}"
        self.client.publish(
            f"{self.node}/last_reprocessed",
            json.dumps({"name": os.path.basename(clip_name), "video_url": url,
                        "annotated": annotated, "ts": ver}),
            retain=True)

    def publish_last_processed(self, clip_path: str,
                               results: list[dict]) -> None:
        """Publish the latest processed clip and its browser-playable URL."""
        if not self.enabled:
            return
        filename = os.path.basename(clip_path)
        event_id = _event_id_from_filename(filename)
        people = list(dict.fromkeys(
            str(item.get("person", "")).strip()
            for item in results if item.get("person")
        ))
        counts = [int(item.get("count", 0)) for item in results]
        codec = self._probe_codec(clip_path) if os.path.isfile(clip_path) else None
        url = ""
        if codec == "h264" and filename.lower().endswith(".mp4"):
            media_root = self.media_base.rstrip("/").rsplit("/", 1)[0]
            url = f"{media_root}/processed/{quote(filename, safe='')}?v={int(time.time())}"
        if (event_id and self._recent_faceid_attribution
                and self._recent_faceid_attribution[0] == event_id):
            people = [self._recent_faceid_attribution[1]]
            self._recent_faceid_attribution = None
        payload = {
            "name": filename,
            "video_url": url,
            "best": max(counts, default=0),
            "attempts": len(results),
            "attributed_to": ", ".join(people),
            "ts": int(time.time()),
        }
        self._last_processed_event_id = event_id
        self._last_processed_payload = payload
        self.client.publish(
            f"{self.node}/last_processed", json.dumps(payload), retain=True
        )

    def note_last_processed_attribution(self, event_id: str,
                                        person_name: str) -> None:
        """Refresh the last-processed payload if FaceID labels that same event."""
        if not self.enabled or not event_id or not person_name.strip():
            return
        person_name = person_name.strip()
        if event_id != self._last_processed_event_id or self._last_processed_payload is None:
            self._recent_faceid_attribution = (event_id, person_name)
            return
        self._last_processed_payload["attributed_to"] = person_name.strip()
        self.client.publish(
            f"{self.node}/last_processed",
            json.dumps(self._last_processed_payload), retain=True,
        )

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
        people = list(people)
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
