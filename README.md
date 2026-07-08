# Soccer Juggle Tracker

Computer-vision pipeline that watches a fixed camera view (a Reolink RLC-810A
pointed at a front yard), detects and identifies known people, counts soccer
**juggles** (keepie-uppies) per person, and tracks each person's all-time high
score. Scores are published to **Home Assistant** over MQTT.

> A "juggle" = the ball contacts a body part **other than the hands/arms**
> (foot, knee, thigh, shoulder, head) between air-time, **without** touching the
> ground or any other object. A hand/arm touch or a ground touch ends the streak.

## Why batch (offline) processing

This runs on a **2016 Intel MacBook Pro** that is *also* hosting Home Assistant
and Matter in Docker. That box has **no CUDA and no Apple MPS** (MPS is
M-series only), so PyTorch runs **CPU-only** at roughly 2–5 FPS for this model
stack. Juggling is far too fast to count accurately at that framerate in real
time — the ball reverses in 2–3 frames.

So the design **decouples capture from compute**:

1. The camera (or HA) records a **short clip at full framerate (~25 fps)** when a
   person enters the yard. No fast ball contacts are lost — the *source* is full
   framerate; only the *analysis* is slower than real time, which is fine.
2. A **worker on the MacBook** processes each clip frame-by-frame at whatever
   speed the CPU allows, updates SQLite, and pushes results to Home Assistant.

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

## Status / roadmap

- [x] **Layer 1** — clip capture, person detect + track, pose overlay, debug video
- [x] **Layer 2** — ball detect/track + juggle state machine (single person)
- [x] **Layer 3** — identity (manual enrollment + face assist), per-person high scores in SQLite
- [x] **Layer 4** — MQTT → Home Assistant sensors + dashboard
- [ ] Accuracy tuning against your real footage (the multi-week part — needs sample clips)
- [ ] Auto motion-trigger wiring from HA/Reolink to the clip inbox

See [`docs/SETUP.md`](docs/SETUP.md) for full setup and
[`docs/HOME_ASSISTANT.md`](docs/HOME_ASSISTANT.md) for the HA side.

## License

Private. Not for redistribution.
