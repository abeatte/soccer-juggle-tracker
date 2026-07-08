# Setup

## 0. Prerequisites

- **Ubuntu** on the 2016 Intel MacBook Pro (x86_64).
- Python 3.9+ (`python3 --version`) and `python3-venv`.
- `ffmpeg` (for clip recording) and OpenCV runtime libs. `setup.sh` installs
  these for you via apt, or manually:
  ```bash
  sudo apt-get install -y ffmpeg python3-venv python3-pip libgl1 libglib2.0-0
  ```
- Your Reolink RLC-810A on the LAN with a **static IP / DHCP reservation**,
  firmware updated, and an **admin** user with an **alphanumeric** password
  (special characters break Reolink auth). RTSP enabled.
- For live webcam enrollment: a camera at `/dev/video0` (otherwise use `--images`).

## 1. Install

```bash
cd soccer-juggle-tracker
./setup.sh
```

This creates `.venv/`, installs pinned deps (CPU-only torch on Intel macOS),
downloads YOLO weights into `models/`, and creates `inbox/ processed/ data/
enroll_images/`.

> On the 2016 Intel MacBook Pro the first model download + first InsightFace run
> take a minute or two. Subsequent runs are cached.

## 2. Configure

```bash
cp config.example.yaml config.yaml
$EDITOR config.yaml
```

Set at minimum:
- `camera.rtsp_main` — `rtsp://USER:PASS@CAM_IP:554/h264Preview_01_main`
- `home_assistant.mqtt_host / mqtt_user / mqtt_password`
- `roi` — crop to the play area once you see a debug video (start full-frame)
- `juggle.ground_y_frac` — the image-y (fraction of height) of the ground where
  the ball rests; calibrate from a debug video

`config.yaml` is gitignored (it holds credentials).

### Preflight check

Verify the environment before enrolling/processing:

```bash
source .venv/bin/activate
python -m juggle_tracker.cli doctor
```

It checks ffmpeg, model weights, RTSP reachability, the MQTT broker, `inbox/`
permissions, and `/dev/video0`. Resolve any ✗ FAIL items (warnings are
non-blocking).

## 3. Enroll your known people (max 4)

Best results: a folder of 10–20 face photos per kid (varied angle/distance/light).

```bash
source .venv/bin/activate
python -m juggle_tracker.cli enroll --name "Kid1" --images enroll_images/kid1
python -m juggle_tracker.cli enroll --name "Kid2" --images enroll_images/kid2
```

Or capture live from the Mac's webcam (SPACE = grab, Q = done):

```bash
python -m juggle_tracker.cli enroll --name "Kid1"
```

Enrolled embeddings live in SQLite (`data/juggle.db`). The raw images in
`enroll_images/` are gitignored — they're PII of your kids.

## 4. Get clips in

**Manual:** copy any `.mp4` into `inbox/`, or grab one from the camera:

```bash
python -m juggle_tracker.cli record --seconds 30
```

**Automatic (recommended):** have Home Assistant / the Reolink integration record
a clip on person detection and drop it in `inbox/`. See
[`HOME_ASSISTANT.md`](HOME_ASSISTANT.md#auto-recording-clips-on-person-detection).

## 5. Process

Single clip, with a debug overlay video to eyeball accuracy:

```bash
python -m juggle_tracker.cli process inbox/clip_123.mp4 --debug-video out.mp4
```

Batch worker (watches `inbox/`, processes new stable files, moves them to
`processed/`):

```bash
python -m juggle_tracker.cli watch
```

Scoreboard:

```bash
python -m juggle_tracker.cli scores
```

## 6. Run the worker as a background service (systemd)

On Ubuntu, run the batch worker as a systemd service so it survives crashes and
starts at boot, at low CPU/IO priority so HA/Matter stay responsive:

```bash
deploy/install-systemd.sh            # user service: start now + at boot
# or a boot-without-login system service (uses sudo, runs as you):
deploy/install-systemd.sh --system
```

Manage / inspect:

```bash
systemctl --user status juggle-tracker.service
journalctl --user -u juggle-tracker.service -f
deploy/install-systemd.sh uninstall
```

Prefer Docker instead? See [`DEPLOY.md`](DEPLOY.md) — on Linux it runs with no VM
overhead and shares one `inbox/` volume with Home Assistant.

## Tuning accuracy

Process a real clip with `--debug-video` and watch:
- **Ball not detected** → lower `models.ball_conf`, raise `infer_long_edge`.
- **Overcounting jitter** → raise `juggle.min_arc_px`.
- **Hand touches counted** → raise `juggle.contact_radius_px` accuracy by
  improving pose (larger `infer_long_edge`) or check `illegal_keypoints`.
- **Ground touches missed/false** → recalibrate `juggle.ground_y_frac`.
- **Wrong/Unknown person** → add more enrollment images; adjust
  `identity.match_threshold` (lower = more lenient).

## Publishing to your private GitHub repo (later)

```bash
git add -A && git commit -m "feat: initial juggle tracker"   # (already committed)
git remote add origin git@github.com:<you>/soccer-juggle-tracker.git
git push -u origin main
```
