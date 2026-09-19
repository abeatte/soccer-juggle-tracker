# Soccer Juggler

Soccer Juggler is the batch computer-vision worker. It watches the shared
inbox for clips exported by the Frigate inbox bridge, detects the ball and pose, matches
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

Set `capture.inbox_dir` to the same host directory the Frigate inbox bridge
writes to (normally `/srv/juggle_inbox`) and set the MQTT host, user, and
password in `soccer_juggler/.env`. The `.env` values override matching entries
in the root `config.yaml` when the native service starts. The recommended
deployment is native `systemd`:

```bash
deploy/install-systemd.sh
systemctl --user status juggle-tracker.service
```

The service loads `soccer_juggler/.env` through its systemd
`EnvironmentFile`. Leave an environment value blank to keep the corresponding
value from `config.yaml`.

The service consumes clips from the inbox, moves completed clips to the
processed directory, writes `data/juggle.db`, and saves annotated high scores
under the configured high-score directory.

## Verify and inspect logs

```bash
python -m juggle_tracker.cli doctor
python -m juggle_tracker.cli scores
journalctl --user -u juggle-tracker.service -f
tail -f worker.log
```

Drop a test MP4 into the configured inbox and verify that it is processed,
SQLite scores change, and `juggle_tracker/#` MQTT topics appear. Home Assistant
should then show the last-session and high-score sensors.

## Stop and troubleshoot

```bash
deploy/install-systemd.sh uninstall
```

Start with `doctor` for ffmpeg, model, inbox, RTSP, and MQTT failures. Check
`docs/TUNING.md` for ball/pose accuracy problems and `docs/DEPLOY.md` for CPU
and thermal limits on the target laptop.