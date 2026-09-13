# Soccer Juggler

Soccer Juggler is the batch computer-vision worker. It watches the shared
inbox for clips recorded by Home Assistant, detects the ball and pose, matches
known people, counts valid juggles, stores results in SQLite, and publishes
MQTT discovery/state messages for Home Assistant.

## Native setup (recommended)

Run these commands from the repository root so the existing package and model
paths remain unchanged:

```bash
./setup.sh
cp config.example.yaml config.yaml
$EDITOR config.yaml
source .venv/bin/activate
python -m juggle_tracker.cli doctor
python -m juggle_tracker.cli enroll --name "Kid1"
```

Set `capture.inbox_dir` to the same host directory Home Assistant writes to
(normally `/srv/juggle_inbox`) and set the MQTT host, user, and password in
`config.yaml`. For the current systemd deployment:

```bash
deploy/install-systemd.sh
systemctl --user status juggle-tracker.service
```

The service consumes clips from the inbox, moves completed clips to the
processed directory, writes `data/juggle.db`, and saves annotated high scores
under the configured high-score directory.

## Container setup

The included compose file is an optional alternative to systemd. It expects a
configured root-level `config.yaml`, model weights under `models/`, and host
directories matching that config:

```bash
mkdir -p inbox processed data highscores
docker compose -f soccer_juggler/docker-compose.yml build
docker compose -f soccer_juggler/docker-compose.yml up -d
```

Set `home_assistant.mqtt_host` in the root `config.yaml` to the broker address
reachable from the container. The native systemd worker and this compose
service should not run at the same time because both consume the same inbox.

## Verify and inspect logs

```bash
python -m juggle_tracker.cli doctor
python -m juggle_tracker.cli scores
journalctl --user -u juggle-tracker.service -f
tail -f worker.log
```

For the container, use `docker compose -f soccer_juggler/docker-compose.yml
logs -f soccer-juggler`. Drop a test MP4 into the configured inbox and verify
that it is processed, SQLite scores change, and `juggle_tracker/#` MQTT topics
appear. Home Assistant should then show the last-session and high-score
sensors.

## Stop and troubleshoot

```bash
deploy/install-systemd.sh uninstall
docker compose -f soccer_juggler/docker-compose.yml down
```

Start with `doctor` for ffmpeg, model, inbox, RTSP, and MQTT failures. Check
`docs/TUNING.md` for ball/pose accuracy problems and `docs/DEPLOY.md` for CPU
and thermal limits on the target laptop.