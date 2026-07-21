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

from . import config as cfgmod
from .db import UNKNOWN_NAME

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
        # Inbox/processed queue sensors + reprocess-a-clip control.
        self.reprocess_topic = f"{self.node}/reprocess/set"
        self.reprocess_select_topic = f"{self.node}/reprocess_select/set"
        # "Annotate on reprocess" switch: when ON, a reprocess also writes a
        # browser-playable annotated replay (overlay drawn in the same detection
        # pass — no extra inference). The live value is remembered here and
        # captured per-clip at button-press time; it resets to the config
        # default on restart (like the calibration numbers).
        self.reprocess_annotate_topic = f"{self.node}/reprocess_annotate/set"
        self._reprocess_annotate = bool(
            self.cfg.capture.get("reprocess_annotate_default", False)
        )
        self._reprocess_selected: Optional[str] = None
        self._last_select_options: Optional[list] = None
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
        self.client.subscribe(self.reprocess_annotate_topic)
        self.client.subscribe(self.inbox_select_topic)
        self.client.subscribe(self.delete_topic)
        self.announce_queues()
        self.publish_queues()

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
                self._reprocess_selected = (
                    None if (not payload or payload == SELECT_NONE) else payload
                )
                self.client.publish(f"{self.node}/reprocess_select",
                                    payload or SELECT_NONE, retain=True)
            elif topic == self.reprocess_annotate_topic:
                self._reprocess_annotate = payload.upper() in ("ON", "1", "TRUE")
                self.client.publish(
                    f"{self.node}/reprocess_annotate",
                    "ON" if self._reprocess_annotate else "OFF", retain=True)
            elif topic == self.inbox_select_topic:
                self._inbox_selected = (
                    None if (not payload or payload == SELECT_NONE) else payload
                )
                self.client.publish(f"{self.node}/inbox_select",
                                    payload or SELECT_NONE, retain=True)
            elif topic == self.delete_topic:
                self._do_delete_inbox()
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
                "command_topic": self.reprocess_topic,
                "payload_press": "reprocess",
                "icon": "mdi:reload",
                "entity_category": "config",
                "availability_topic": self.avail_topic,
                "device": dev,
            }), retain=True)
        # "Annotate on reprocess" toggle (renders as a checkbox/switch in HA).
        self.client.publish(
            f"{self.prefix}/switch/{self.node}/reprocess_annotate/config",
            json.dumps({
                "name": "Annotate On Reprocess",
                "unique_id": f"{self.node}_reprocess_annotate",
                "state_topic": f"{self.node}/reprocess_annotate",
                "command_topic": self.reprocess_annotate_topic,
                "payload_on": "ON", "payload_off": "OFF",
                "icon": "mdi:draw",
                "entity_category": "config",
                "availability_topic": self.avail_topic,
                "device": dev,
            }), retain=True)
        # Seed the toggle state so HA reflects the worker's current (default)
        # value after a restart.
        self.client.publish(
            f"{self.node}/reprocess_annotate",
            "ON" if self._reprocess_annotate else "OFF", retain=True)
        # Sensor exposing the most recent forced-annotated reprocess replay
        # (name in state, video_url in attributes). We deliberately do NOT seed
        # an empty value here, so a prior replay URL (retained on the broker)
        # survives a worker restart.
        self.client.publish(
            f"{self.prefix}/sensor/{self.node}/last_reprocessed/config",
            json.dumps({
                "name": "Juggle Last Reprocessed",
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

    def _announce_reprocess_select(self, options: list) -> None:
        """(Re)publish the reprocess `select` discovery with current options."""
        self.client.publish(
            f"{self.prefix}/select/{self.node}/reprocess_file/config",
            json.dumps({
                "name": "Juggle Reprocess File",
                "unique_id": f"{self.node}_reprocess_file",
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
        opts = [SELECT_NONE] + pfiles[:50]
        if opts != self._last_select_options:
            self._announce_reprocess_select(opts)
            self._last_select_options = opts
        # Keep the inbox-delete dropdown in sync with the live inbox contents.
        in_opts = [SELECT_NONE] + infiles[:50]
        if in_opts != self._last_inbox_options:
            self._announce_inbox_select(in_opts)
            self._last_inbox_options = in_opts

    def _do_reprocess(self) -> None:
        """Move the selected processed clip back into the inbox so the normal
        watcher re-runs it end-to-end with the current config."""
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
        try:
            os.makedirs(inbox, exist_ok=True)
            dst = os.path.join(inbox, name)
            shutil.move(src, dst)
            # Capture the "annotate this run" intent NOW (at button press) via a
            # sidecar marker the watcher consumes, so toggling the switch after
            # pressing can't change an already-queued job.
            note = ""
            if self._reprocess_annotate:
                try:
                    with open(dst + ".annotate", "w", encoding="utf-8") as fh:
                        fh.write("1")
                    note = " (annotated replay)"
                except OSError as exc:
                    note = f" (annotate marker failed: {exc})"
            print(f"  [reprocess] {name} -> inbox{note}; will re-run with "
                  f"current config", flush=True)
            self.publish_queues()  # reflect the move immediately
            # Clear the last-reprocessed replay for this new run so a card
            # guarded on `video_url != ""` hides while the run is in flight
            # (annotated) or stays hidden for a non-annotated run (which
            # produces no replay). publish_last_reprocessed() restores a real
            # URL when an annotated run finishes.
            self.publish_reprocess_pending(name, self._reprocess_annotate)
            # Reset the dropdown to "(none)" so the UI doesn't keep showing the
            # now-requeued (and no-longer-listed) file as the selection.
            self._reprocess_selected = None
            self.client.publish(f"{self.node}/reprocess_select", SELECT_NONE,
                                retain=True)
        except Exception as exc:
            print(f"  [reprocess] failed to requeue '{name}': {exc}", flush=True)

    def _do_delete_inbox(self) -> None:
        """Delete the selected inbox clip.

        If it is the clip currently being processed, ask the worker to cancel
        the in-flight run — the watcher then removes the file (and its marker)
        once the loop aborts. Otherwise the queued clip and its `.annotate`
        sidecar are removed here immediately."""
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
            # watcher delete it (+ marker + partial temp) after it aborts.
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
        """Publish the URL of the most recent forced-annotated reprocess replay.

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
