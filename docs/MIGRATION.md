# Migration Runbook

This runbook migrates the current production deployment to the repository's
new layout without creating a new Home Assistant instance or losing existing
state.

## Current production layout

The existing live services are outside this repository:

```text
/home/abeatte/homeassistant/config    Home Assistant configuration and state
/home/abeatte/frigate/config          Frigate configuration
/home/abeatte/frigate/storage         Frigate recordings and snapshots
/home/abeatte/mosquitto/config        Mosquitto configuration
/home/abeatte/mosquitto/data          Mosquitto persistence
/home/abeatte/mosquitto/log           Mosquitto logs
/srv/juggle_inbox                     shared clip inbox
/srv/juggle_processed                  processed clips
/srv/juggle_highscores                 high-score videos
```

Do not delete the existing directories. Docker runs images; these directories
are bind-mounted data and configuration.

The target runtime is:

```text
Mosquitto              Docker, controlled from this repository
Frigate                Docker, controlled from this repository
Frigate inbox bridge   Docker, built from this repository
Home Assistant         Docker, using /home/abeatte/homeassistant/config
Soccer Juggle worker   native systemd, not Docker
```

## 1. Back up the current system

Run these commands before stopping anything:

```bash
cd /home/abeatte/projects/soccer-juggle-tracker
mkdir -p ~/soccer-juggle-migration-backup-$(date +%Y%m%d_%H%M%S)
BACKUP=~/soccer-juggle-migration-backup-$(date +%Y%m%d_%H%M%S)

cp /home/abeatte/homeassistant/config/configuration.yaml "$BACKUP/"
cp /home/abeatte/homeassistant/config/secrets.yaml "$BACKUP/" 2>/dev/null || true
cp /home/abeatte/frigate/config/config.yml "$BACKUP/frigate-config.yml"
cp /home/abeatte/mosquitto/config/mosquitto.conf "$BACKUP/mosquitto.conf"
cp config.yaml "$BACKUP/tracker-config.yaml"
```

Also verify the existing data is present:

```bash
ls -ld /home/abeatte/homeassistant/config
ls -ld /home/abeatte/frigate/storage
ls -ld /home/abeatte/mosquitto/data
ls -ld /srv/juggle_inbox /srv/juggle_processed /srv/juggle_highscores
```

## 2. Prepare the repository environment files

Fill the ignored files with the values from the working production setup:

```bash
$EDITOR frigate/.env
$EDITOR soccer_juggler/.env
$EDITOR homeassistant/.env
$EDITOR mosquitto/.env
```

Important values:

- `frigate/.env`: direct camera RTSP URLs, Frigate MQTT values, bridge MQTT
  values, and `/srv/juggle_inbox`.
- `soccer_juggler/.env`: the direct front-yard RTSP URL, `127.0.0.1`, MQTT
  port `1883`, MQTT credentials, and the three `/srv` directories.
- `homeassistant/.env`: currently no required values; high-score mounting is
  fixed to `/srv/juggle_highscores`.
- `mosquitto/.env`: currently no required values; Mosquitto uses port `1883`.

The native soccer worker should use the direct camera URL, for example:

```dotenv
JUGGLE_RTSP_MAIN=rtsp://USER:PASSWORD@CAMERA_IP:554/h264Preview_01_main
JUGGLE_MQTT_HOST=127.0.0.1
JUGGLE_MQTT_PORT=1883
```

Do not put the Frigate internal restream URL in `JUGGLE_RTSP_MAIN`.

## 3. Prepare the live Home Assistant configuration

The repository Compose file must mount the existing production config, not the
repository's example `homeassistant/configs` directory. In
`homeassistant/docker-compose.yml`, use:

```yaml
volumes:
  - /home/abeatte/homeassistant/config:/config
  - /etc/localtime:/etc/localtime:ro
  - /srv/juggle_highscores:/config/www/juggle:ro
```

Copy the tracker package into the live configuration:

```bash
mkdir -p /home/abeatte/homeassistant/config/packages
cp homeassistant/packages/juggle_tracker.yaml \
   /home/abeatte/homeassistant/config/packages/juggle_tracker.yaml
```

In the live `/home/abeatte/homeassistant/config/configuration.yaml`:

1. Remove the old inline `mqtt.sensor` definition for `Juggle Last Session`.
2. Remove the old `/media/juggle_inbox` allowlist entry.
3. Ensure this exists:

```yaml
homeassistant:
  packages: !include_dir_named packages
```

The package contains the corrected MQTT sensor, template sensors, and
notifications. Home Assistant no longer records clips or mounts the inbox.

Create missing include files only if the live configuration does not already
have them:

```bash
touch /home/abeatte/homeassistant/config/automations.yaml
touch /home/abeatte/homeassistant/config/scripts.yaml
touch /home/abeatte/homeassistant/config/scenes.yaml
```

## 4. Stop the old deployment

Leave `matter-server` running. Stop only the current Mosquitto, Frigate, and
Home Assistant projects using their existing production directories:

```bash
systemctl --user disable --now juggle-tracker.service

cd /home/abeatte/homeassistant
# Record the exact command used by the existing deployment if it differs.
docker compose down

cd /home/abeatte/frigate
docker compose down

cd /home/abeatte/mosquitto
docker compose down
```

Do not use `docker compose down -v`. Do not delete `config`, `storage`, `data`,
or `log` directories.

Confirm the old containers are stopped:

```bash
docker ps
```

## 5. Validate and start the new Docker stacks

Start Mosquitto first so the shared `mosquitto_default` network exists:

```bash
cd /home/abeatte/projects/soccer-juggle-tracker/mosquitto
docker compose config
docker compose up -d
docker compose ps
```

Start Frigate and build the inbox bridge:

```bash
cd /home/abeatte/projects/soccer-juggle-tracker/frigate
docker compose config
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 frigate-inbox-bridge
```

The bridge should report that it subscribed to `frigate/events`. It uses
Frigate's internal API at `http://frigate:5000`, so no API token is needed.

Start Home Assistant using the repository Compose file, after verifying its
`/config` bind mount points to `/home/abeatte/homeassistant/config`:

```bash
cd /home/abeatte/projects/soccer-juggle-tracker/homeassistant
docker compose config
docker compose up -d
docker compose ps
docker compose logs --tail=100 homeassistant
```

Do not start a soccer Docker Compose service. That optional deployment has been
removed. The soccer worker runs natively through systemd.

## 6. Start the native soccer worker

From the repository root:

```bash
cd /home/abeatte/projects/soccer-juggle-tracker
python3 -m py_compile src/juggle_tracker/config.py
source .venv/bin/activate
python -m juggle_tracker.cli doctor
deploy/install-systemd.sh
```

Check it:

```bash
systemctl --user status juggle-tracker.service
journalctl --user -u juggle-tracker.service -n 100 --no-pager
```

Expected status/log text includes:

```text
Watching /srv/juggle_inbox for new clips
```

## 7. Smoke-test each connection

### Mosquitto

```bash
cd /home/abeatte/projects/soccer-juggle-tracker/mosquitto
docker compose ps
docker compose logs --tail=50 mosquitto
```

### Frigate

Open `http://<host-ip>:8971` and verify the cameras are receiving frames.
Alternatively:

```bash
cd /home/abeatte/projects/soccer-juggle-tracker/frigate
docker compose ps
docker compose logs --tail=100 frigate
```

### Bridge MQTT subscription

Use the narrow event topic. Do not subscribe to all of `frigate/#` while testing,
because Frigate publishes binary JPEG snapshots on that tree.

```bash
mosquitto_sub -h 127.0.0.1 -p 1883 -t 'frigate/events' -v
```

### End-to-end clip test

1. Keep the bridge logs open:

   ```bash
   cd /home/abeatte/projects/soccer-juggle-tracker/frigate
   docker compose logs -f frigate-inbox-bridge
   ```

2. Walk into the configured front-yard camera view.
3. Wait for the person event to end.
4. Look for an `Exported Frigate event` message.
5. Check the shared directories:

   ```bash
   ls -lh /srv/juggle_inbox /srv/juggle_processed
   ```

6. Watch the native worker process the file:

   ```bash
   journalctl --user -u juggle-tracker.service -f
   ```

The expected path is:

```text
Camera -> Frigate -> MQTT frigate/events -> inbox bridge
      -> /srv/juggle_inbox -> native soccer worker
      -> SQLite + MQTT juggle_tracker/* -> Home Assistant
```

The inbox file may move to `/srv/juggle_processed` quickly after processing.

### Home Assistant verification

Open Home Assistant and verify:

- `sensor.juggle_last_session`
- per-person high-score sensors
- the Juggle Inbox Queue sensor
- the Delete Selected Inbox Clip control
- Frigate camera/person entities

The delete control operates over MQTT through the soccer worker; Home Assistant
does not need the inbox mounted.

## 8. Rollback

If the new deployment fails:

```bash
systemctl --user disable --now juggle-tracker.service

cd /home/abeatte/projects/soccer-juggle-tracker/frigate
docker compose down
cd ../mosquitto
docker compose down
cd ../homeassistant
docker compose down
```

Restore the old Compose projects from their original directories:

```bash
cd /home/abeatte/homeassistant && docker compose up -d
cd /home/abeatte/mosquitto && docker compose up -d
cd /home/abeatte/frigate && docker compose up -d
```

Restore the backed-up Home Assistant configuration only if it was modified
incorrectly. Do not delete the repository or the `/srv` data directories.
