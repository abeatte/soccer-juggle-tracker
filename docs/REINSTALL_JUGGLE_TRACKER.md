# Reinstall Runbook — Soccer Juggle Tracker

Use this when reinstalling the full stack on the production machine (Ubuntu,
2016 Intel MacBook Pro) from scratch, or after a clean OS reinstall. This
assumes the repository already exists at
`/home/abeatte/projects/soccer-juggle-tracker` and the `/srv` data directories
are intact.

For a first-time migration from an old non-repo deployment, see
[`docs/MIGRATION.md`](MIGRATION.md) instead.

---

## Prerequisites

- Repository cloned at `/home/abeatte/projects/soccer-juggle-tracker`
- Docker and Docker Compose installed
- Python 3.9+ available (`python3 --version`)
- ffmpeg installed (`ffmpeg -version`)
- systemd user manager running with lingering enabled (`loginctl enable-linger $USER`)
- `/srv/juggle_inbox`, `/srv/juggle_processed`, `/srv/juggle_highscores` exist and
  are writable by your user

---

## 1. Back up existing state

Run this before touching anything:

```bash
BACKUP=~/soccer-juggle-reinstall-backup-$(date +%Y%m%d_%H%M%S)
mkdir -p "$BACKUP"

cp /home/abeatte/homeassistant/config/configuration.yaml "$BACKUP/" 2>/dev/null || true
cp /home/abeatte/homeassistant/config/secrets.yaml "$BACKUP/" 2>/dev/null || true
cp /home/abeatte/projects/soccer-juggle-tracker/juggletracker/config.yaml "$BACKUP/tracker-config.yaml" 2>/dev/null || true

ls -ld /home/abeatte/homeassistant/config
ls -ld /home/abeatte/frigate/storage 2>/dev/null || true
ls -ld /home/abeatte/mosquitto/data 2>/dev/null || true
ls -ld /srv/juggle_inbox /srv/juggle_processed /srv/juggle_highscores
```

---

## 2. Prepare the .env files

These files are gitignored and never deployed by `servicer.sh`. They must be
created manually on the target machine.

### `~/juggletracker/.env`

This is loaded by the systemd unit as the `EnvironmentFile`. Copy from the
example and fill in your values:

```bash
cp /home/abeatte/projects/soccer-juggle-tracker/juggletracker/.env_example \
   ~/juggletracker/.env
$EDITOR ~/juggletracker/.env
```

Required values:

```dotenv
JUGGLE_MQTT_USER=<mosquitto username>
JUGGLE_MQTT_PASSWORD=<mosquitto password>

# Direct camera RTSP URL — NOT the Frigate internal restream URL
JUGGLE_RTSP_MAIN=rtsp://frigate:<PASSWORD>@<CAMERA_IP>:554/h264Preview_01_main

JUGGLE_MQTT_HOST=127.0.0.1
JUGGLE_MQTT_PORT=1883

JUGGLE_INBOX_DIR=/srv/juggle_inbox
JUGGLE_PROCESSED_DIR=/srv/juggle_processed
JUGGLE_HIGHSCORE_DIR=/srv/juggle_highscores
```

### `~/frigate/.env`

```bash
cp /home/abeatte/projects/soccer-juggle-tracker/frigate/.env_example \
   ~/frigate/.env
$EDITOR ~/frigate/.env
```

Required values:

```dotenv
FRIGATE_MQTT_HOST=mosquitto
FRIGATE_MQTT_USER=<mosquitto username>
FRIGATE_MQTT_PASSWORD=<mosquitto password>

# Direct camera URLs (main + sub for each camera)
FRIGATE_RTSP_FRONT_MAIN=rtsp://frigate:<PASSWORD>@<IP>:554/h264Preview_01_main
FRIGATE_RTSP_FRONT_SUB=rtsp://frigate:<PASSWORD>@<IP>:554/h264Preview_01_sub
# go2rtc re-streamed inputs (used inside the Frigate container)
FRIGATE_RTSP_FRONT_MAIN_INPUT=rtsp://<IP>:8554/front_yard
FRIGATE_RTSP_FRONT_SUB_INPUT=rtsp://<IP>:8554/front_yard_sub

FRIGATE_RTSP_BACK_MAIN=rtsp://frigate:<PASSWORD>@<IP>:554/h264Preview_01_main
FRIGATE_RTSP_BACK_SUB=rtsp://frigate:<PASSWORD>@<IP>:554/h264Preview_01_sub
FRIGATE_RTSP_BACK_MAIN_INPUT=rtsp://<IP>:8554/back_yard
FRIGATE_RTSP_BACK_SUB_INPUT=rtsp://<IP>:8554/back_yard_sub

# Inbox bridge credentials
BRIDGE_MQTT_USER=<mosquitto username>
BRIDGE_MQTT_PASSWORD=<mosquitto password>
JUGGLE_INBOX_HOST=/srv/juggle_inbox
FRIGATE_CAMERA_FILTER=front_yard
FRIGATE_ZONE_FILTER=grass
```

There are no `.env` files required for mosquitto or homeassistant.

---

## 3. Prepare the Home Assistant configuration

Verify `homeassistant/docker-compose.yml` mounts the live config (not the
repo's example directory):

```yaml
volumes:
  - /home/abeatte/homeassistant/config:/config
  - /etc/localtime:/etc/localtime:ro
  - /srv/juggle_highscores:/config/www/juggle:ro
```

Copy the tracker package into the live HA config:

```bash
mkdir -p /home/abeatte/homeassistant/config/packages
cp /home/abeatte/projects/soccer-juggle-tracker/homeassistant/packages/juggle_tracker.yaml \
   /home/abeatte/homeassistant/config/packages/juggle_tracker.yaml
```

Ensure `configuration.yaml` contains:

```yaml
homeassistant:
  packages: !include_dir_named packages
```

Create empty include stubs if they do not already exist:

```bash
touch /home/abeatte/homeassistant/config/automations.yaml
touch /home/abeatte/homeassistant/config/scripts.yaml
touch /home/abeatte/homeassistant/config/scenes.yaml
```

---

## 4. Deploy source files to live directories

```bash
cd /home/abeatte/projects/soccer-juggle-tracker

./servicer.sh --deploy mosquitto
./servicer.sh --deploy frigate
./servicer.sh --deploy homeassistant
./servicer.sh --deploy juggletracker
./servicer.sh --deploy machinetelemetry
```

Each deploy rsyncs only the allowlisted source files into the target directory
(`~/mosquitto`, `~/frigate`, `~/homeassistant`, `~/juggletracker`,
`~/machinetelemetry`). It never deletes files that only exist in the target, and
always skips `.env` files.

---

## 5. Set up the juggletracker Python environment

```bash
cd ~/juggletracker
./setup.sh
```

This creates `.venv`, installs dependencies, and downloads YOLO model weights
into `models/`. It does not require internet access if the models are already
present.

---

## 6. Start services in order

Mosquitto must be up before Frigate or the soccer worker can connect.

### Mosquitto

```bash
./servicer.sh --start mosquitto
```

Verify:

```bash
./servicer.sh --check mosquitto
```

### Frigate + inbox bridge

```bash
./servicer.sh --start frigate
```

Verify the bridge subscribed to `frigate/events`:

```bash
cd ~/frigate && docker compose logs --tail=50 frigate-inbox-bridge
```

### Home Assistant

```bash
./servicer.sh --start homeassistant
```

### Soccer juggle worker

```bash
./servicer.sh --start juggletracker
```

This runs `python -m juggletracker.cli doctor` (connectivity preflight) then
installs and starts the `juggle-tracker.service` systemd user unit.

Expected log output:

```text
Watching /srv/juggle_inbox for new clips
```

### Machine telemetry (optional)

```bash
./servicer.sh --start machinetelemetry
```

---

## 7. Smoke test

### Mosquitto reachable

```bash
mosquitto_sub -h 127.0.0.1 -p 1883 -t 'frigate/events' -v
```

### Frigate cameras live

Open `http://<host-ip>:8971` and confirm the front/back yard cameras are
receiving frames.

### End-to-end clip test

1. Open the bridge log:
   ```bash
   cd ~/frigate && docker compose logs -f frigate-inbox-bridge
   ```
2. Walk into the front-yard camera view and wait for the person event to end.
3. Confirm `Exported Frigate event` appears in the bridge log.
4. Confirm the clip lands in the inbox:
   ```bash
   ls -lh /srv/juggle_inbox /srv/juggle_processed
   ```
5. Watch the worker process it:
   ```bash
   journalctl --user -u juggle-tracker.service -f
   ```

### Home Assistant sensors

Open Home Assistant and verify:

- `sensor.juggle_last_session`
- Per-person high-score sensors
- Juggle Inbox Queue sensor
- Inbox Queue cards with clip thumbnails, durations, and per-clip delete buttons
- Frigate camera/person entities

---

## 8. Verify all services at once

```bash
./servicer.sh --check all
```

---

## Rollback

If anything fails, stop the new services and restore the old ones:

```bash
systemctl --user disable --now juggle-tracker.service

cd /home/abeatte/projects/soccer-juggle-tracker
./servicer.sh --shut-down all

cd /home/abeatte/homeassistant && docker compose up -d
cd /home/abeatte/mosquitto && docker compose up -d
cd /home/abeatte/frigate && docker compose up -d
```

Do not delete the repository or the `/srv` data directories.
