"""Export completed Frigate person events as clips for the soccer worker."""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

import paho.mqtt.client as mqtt


LOG = logging.getLogger("frigate-inbox-bridge")
EVENT_TOPIC = "frigate/events"
SAFE_PART = re.compile(r"[^A-Za-z0-9_.-]+")


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Event:
    event_id: str
    camera: str


class InboxBridge:
    def __init__(self) -> None:
        self.mqtt_host = os.environ.get("MQTT_HOST", "mosquitto")
        self.mqtt_port = env_int("MQTT_PORT", 1883)
        self.mqtt_user = os.environ.get("MQTT_USER", "")
        self.mqtt_password = os.environ.get("MQTT_PASSWORD", "")
        self.frigate_url = os.environ.get("FRIGATE_URL", "http://frigate:5000").rstrip("/")
        self.inbox_dir = os.environ.get("INBOX_DIR", "/inbox")
        self.event_label = os.environ.get("EVENT_LABEL", "person")
        self.cameras = {
            value.strip()
            for value in os.environ.get("CAMERA_FILTER", "").split(",")
            if value.strip()
        }
        self.zones = {
            value.strip()
            for value in os.environ.get("ZONE_FILTER", "").split(",")
            if value.strip()
        }
        self.cooldown = env_int("COOLDOWN_SECONDS", 120)
        self.retries = env_int("DOWNLOAD_RETRIES", 12)
        self.retry_seconds = env_int("DOWNLOAD_RETRY_SECONDS", 5)
        self.events: queue.Queue[Event] = queue.Queue()
        self.seen: set[str] = set()
        self.last_export: dict[str, float] = {}

    def on_connect(self, client, _userdata, _flags, reason_code, _properties=None) -> None:
        if getattr(reason_code, "is_failure", False):
            LOG.error("MQTT connection failed: %s", reason_code)
            return
        client.subscribe(EVENT_TOPIC, qos=1)
        LOG.info("Subscribed to %s", EVENT_TOPIC)

    def on_message(self, _client, _userdata, message) -> None:
        try:
            payload = json.loads(message.payload)
        except (UnicodeDecodeError, json.JSONDecodeError):
            LOG.warning("Ignoring non-JSON Frigate event")
            return

        if payload.get("type") != "end":
            return
        after = payload.get("after") or {}
        if after.get("label") != self.event_label:
            return
        camera = after.get("camera", "")
        event_id = after.get("id", "")
        if not event_id or (self.cameras and camera not in self.cameras):
            return
        if self.zones:
            entered_zones = set(after.get("entered_zones") or [])
            current_zones = set(after.get("current_zones") or [])
            if not self.zones & (entered_zones | current_zones):
                LOG.debug(
                    "Skipping event %s — zones %s not in filter %s",
                    event_id,
                    entered_zones | current_zones,
                    self.zones,
                )
                return
        if event_id in self.seen:
            return
        now = time.monotonic()
        if now - self.last_export.get(camera, 0) < self.cooldown:
            LOG.info("Skipping %s event %s during %ss cooldown", camera, event_id, self.cooldown)
            self.seen.add(event_id)
            return
        self.seen.add(event_id)
        self.events.put(Event(event_id=event_id, camera=camera))

    def download_clip(self, event: Event) -> None:
        url = f"{self.frigate_url}/api/events/{event.event_id}/clip.mp4"
        headers = {"Accept": "video/mp4"}

        os.makedirs(self.inbox_dir, exist_ok=True)
        camera = SAFE_PART.sub("_", event.camera)
        event_id = SAFE_PART.sub("_", event.event_id)
        destination = os.path.join(self.inbox_dir, f"clip_{camera}_{event_id}.mp4")
        if os.path.exists(destination):
            LOG.info("Clip already exists: %s", destination)
            return

        for attempt in range(1, self.retries + 1):
            temporary = None
            try:
                request = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(request, timeout=30) as response:
                    with tempfile.NamedTemporaryFile(
                        mode="wb", dir=self.inbox_dir, prefix=".frigate-", delete=False
                    ) as output:
                        temporary = output.name
                        while chunk := response.read(1024 * 1024):
                            output.write(chunk)
                if os.path.getsize(temporary) == 0:
                    raise RuntimeError("Frigate returned an empty clip")
                os.replace(temporary, destination)
                os.chmod(destination, 0o644)
                self.last_export[event.camera] = time.monotonic()
                LOG.info("Exported Frigate event %s to %s", event.event_id, destination)
                return
            except (OSError, urllib.error.URLError, urllib.error.HTTPError, RuntimeError) as exc:
                if temporary:
                    try:
                        os.unlink(temporary)
                    except FileNotFoundError:
                        pass
                if attempt == self.retries:
                    LOG.error("Could not export event %s after %s attempts: %s", event.event_id, attempt, exc)
                    return
                LOG.warning("Clip %s is not ready (%s); retrying in %ss", event.event_id, exc, self.retry_seconds)
                time.sleep(self.retry_seconds)

    def run(self) -> None:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="frigate-inbox-bridge")
        if self.mqtt_user:
            client.username_pw_set(self.mqtt_user, self.mqtt_password)
        client.on_connect = self.on_connect
        client.on_message = self.on_message
        LOG.info("Connecting to MQTT %s:%s; Frigate API %s", self.mqtt_host, self.mqtt_port, self.frigate_url)
        client.connect(self.mqtt_host, self.mqtt_port, keepalive=60)
        client.loop_start()
        try:
            while True:
                self.download_clip(self.events.get())
        finally:
            client.loop_stop()
            client.disconnect()


if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    InboxBridge().run()