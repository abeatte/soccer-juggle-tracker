# Soccer Juggle Tracker
  <img src="icon.jpg" width="350">
Computer-vision pipeline that watches a fixed camera view (a Reolink RLC-810A
pointed at a front yard), detects and identifies known people, counts soccer
**juggles** (keepie-uppies) per person, and tracks each person's all-time high
score. Scores are published to **Home Assistant** over MQTT.

> A "juggle" = the ball contacts a body part **other than the hands/arms**
> (foot, knee, thigh, shoulder, head) between air-time, **without** touching the
> ground or any other object. A hand/arm touch or a ground touch ends the streak.

## Why batch (offline) processing

This runs on a **2016 Intel MacBook Pro running Ubuntu** that is *also* hosting
Home Assistant and Matter in Docker. That box has **no GPU** usable for ML
(Intel integrated graphics; no CUDA), so PyTorch runs **CPU-only** at roughly
2–5 FPS for this model stack. Juggling is far too fast to count accurately at
that framerate in real time — the ball reverses in 2–3 frames.

> Because it's an **Intel** CPU on Linux, OpenVINO (Intel's inference runtime)
> can recover much of that speed (commonly ~2–3x) — **but only where AVX2 is
> available**. The confirmed target here is a 2012 i7-3740QM (Ivy Bridge, **no
> AVX2**), so it runs the `.pt` models under a low-power profile instead; see
> [`docs/DEPLOY.md`](docs/DEPLOY.md). Either way, batch keeps accuracy guaranteed.

So the design **decouples capture from compute**:

1. Frigate records a **short clip at full framerate (~25 fps)** when a person
  enters the yard. Its inbox bridge exports the completed event clip without
  involving Home Assistant. No fast ball contacts are lost — the *source* is
  full framerate; only the *analysis* is slower than real time, which is fine.
2. The native `systemd` worker on the MacBook processes each clip frame-by-frame
  at whatever speed the CPU allows, updates SQLite, and pushes results to Home
  Assistant.

Result: accuracy is preserved, HA stays responsive, and scores appear a few
minutes after a session instead of instantly. To get **as close to real time as
possible** on this hardware, the pipeline uses nano models, frame downscaling,
optional frame-skip for the person/pose stages (never for the ball), and a
cropped region of interest. See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Quick start

```bash
# 1. Install (creates a venv, installs deps, downloads models)
./setup.sh

# 2. Configure — copy the example and edit RTSP URL + MQTT creds
cp config.example.yaml config.yaml
$EDITOR config.yaml

# 2b. Preflight: verify ffmpeg, models, RTSP, MQTT, inbox, webcam
python -m juggle_tracker.cli doctor

# 3. Enroll your known people (one-time; ~10-20 face shots each)
source .venv/bin/activate
python -m juggle_tracker.cli enroll --name "Kid1"
python -m juggle_tracker.cli enroll --name "Kid2"
# ...up to 4

# 4a. Process a single recorded clip
python -m juggle_tracker.cli process /path/to/clip.mp4 --debug-video out.mp4

# 4b. OR run the batch worker that watches a folder for new clips
python -m juggle_tracker.cli watch

# Live scoreboard
python -m juggle_tracker.cli scores
```

## Service layout

The repository is organized into four independently operated services:

| Folder | Independent responsibility | Connects through |
|---|---|---|
| [`frigate/`](frigate/) | Watches the Reolink streams, detects people, records video, and publishes camera events | MQTT to Mosquitto; camera RTSP input |
| [`homeassistant/`](homeassistant/) | Provides the UI, notifications, and MQTT-discovered sensors | MQTT to Mosquitto; high-score media |
| [`soccer_juggler/`](soccer_juggler/) | Processes inbox clips, counts per-person juggles, stores SQLite results, and publishes scores | Shared inbox/processed directories; MQTT to Mosquitto |
| [`mosquitto/`](mosquitto/) | Routes MQTT events and retained state between the other services | TCP port `1883`; Docker network `mosquitto_default` |

Each folder has its own setup, lifecycle, log, and verification instructions.
They can be stopped independently: Frigate can keep recording, Home Assistant
can keep serving its UI, Mosquitto can keep routing messages, and the soccer
worker can be paused without losing the other services. The complete path is:

```text
Reolink camera -> Frigate -> MQTT/Mosquitto -> Frigate inbox bridge
Frigate event clip -> shared inbox MP4 -> native systemd worker -> SQLite + MQTT
MQTT/Mosquitto -> Home Assistant sensors, dashboard, notifications, replays
```

Start Mosquitto before Frigate, then start Home Assistant and the native
`systemd` soccer worker. The Frigate inbox bridge must be running before the
worker can receive clips.

## Pipeline stages

```
clip.mp4 ─▶ capture ─▶ detect (person + ball)  ┐
                    ─▶ pose (17 keypoints)      ├─▶ juggle state machine ─▶ SQLite ─▶ MQTT ─▶ Home Assistant
                    ─▶ track (ByteTrack)        │        ▲
                    ─▶ identity (face match) ───┘        └─ nearest-keypoint contact classifier
```

| Module | File | Job |
|---|---|---|
| Config | `src/juggle_tracker/config.py` | Load/validate `config.yaml` |
| Capture | `src/juggle_tracker/capture.py` | RTSP clip recording + frame iteration |
| Detect | `src/juggle_tracker/detect.py` | YOLO person (cls 0) + sports ball (cls 32) |
| Pose | `src/juggle_tracker/pose.py` | YOLO-pose 17 keypoints |
| Track | `src/juggle_tracker/track.py` | Stable per-person track IDs (ByteTrack) |
| Identity | `src/juggle_tracker/identity.py` | Enroll + match known faces to tracks |
| Juggle | `src/juggle_tracker/juggle.py` | Ball-trajectory contact state machine |
| DB | `src/juggle_tracker/db.py` | People, sessions, high scores (SQLite) |
| HA/MQTT | `src/juggle_tracker/ha_mqtt.py` | MQTT discovery + state publish |
| Pipeline | `src/juggle_tracker/pipeline.py` | Orchestrate a clip end-to-end |

Run `python -m juggle_tracker.cli doctor` any time to check the environment
(ffmpeg, model weights, RTSP reachability, MQTT broker, inbox permissions,
`/dev/video0`).

## Status / roadmap

- [x] **Layer 1** — clip capture, person detect + track, pose overlay, debug video
- [x] **Layer 2** — ball detect/track + juggle state machine (single person)
- [x] **Layer 3** — identity (manual enrollment + face assist), per-person high scores in SQLite
- [x] **Layer 4** — MQTT → Home Assistant sensors + dashboard
- [ ] Accuracy tuning against your real footage (the multi-week part — needs sample clips)
- [ ] Auto motion-trigger wiring from HA/Reolink to the clip inbox

## Docs

- [`docs/SETUP.md`](docs/SETUP.md) — install, configure, enroll, run
- [`docs/DEPLOY.md`](docs/DEPLOY.md) — native systemd deployment, shared inbox, OpenVINO
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — why batch, the frame pipeline, the juggle state machine
- [`docs/HOME_ASSISTANT.md`](docs/HOME_ASSISTANT.md) — MQTT discovery, dashboard, automations
- [`docs/TUNING.md`](docs/TUNING.md) — first-clip calibration + ongoing regression tuning

## Auto-run + Home Assistant wiring

Target OS: **Ubuntu** on a 2016 Intel MacBook Pro (CPU-only), alongside HA +
Matter. See [`docs/DEPLOY.md`](docs/DEPLOY.md) for the full picture.

- **Run the worker as a background service** (systemd, low CPU/IO priority so
  HA/Matter stay responsive):
  ```bash
  deploy/install-systemd.sh            # user service: start now + at boot
  deploy/install-systemd.sh --system   # system service (sudo), runs as you
  deploy/install-systemd.sh uninstall  # stop + remove
  ```
- **Intel-CPU acceleration** (biggest speedup toward real time): export the
  models to OpenVINO once, then point `config.yaml` at the exported dirs:
  ```bash
  python tools/export_openvino.py      # (or ./setup.sh --openvino)
  ```
- **Home Assistant package** with the extra sensors + automations (new-high-score
  TTS, notifications, and nightly leaderboard):
  copy [`homeassistant/packages/juggle_tracker.yaml`](homeassistant/packages/juggle_tracker.yaml)
  into `<config>/packages/`. The per-person high-score sensors themselves appear
  automatically via MQTT discovery.
- **Measure accuracy over time**: `python tools/eval.py ground_truth.csv` scores a
  labelled clip set against your hand counts (sandboxed — never touches real
  scores). See [`docs/TUNING.md`](docs/TUNING.md).

## License

Private. Not for redistribution.

## Bonus: host telemetry in Home Assistant

The same box also runs Home Assistant and Frigate. The
[`machinetelemetry/`](machinetelemetry/) folder holds a tiny companion daemon
that publishes live host stats — CPU %, **CPU temperature**, **fan RPM**, load,
memory, disk, uptime, and Frigate's container CPU/mem — to Home Assistant over
MQTT discovery (same pattern as `ha_mqtt.py`). It reuses this repo's `.venv` and
installs as its own `systemd --user` service. See
[`machinetelemetry/README.md`](machinetelemetry/README.md).
