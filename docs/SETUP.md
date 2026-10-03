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

## 2. Configure

```bash
cp config.example.yaml config.yaml
$EDITOR config.yaml
```

Set at minimum:
- `camera.rtsp_main` — `rtsp://USER:PASS@CAM_IP:554/h264Preview_01_main`
- `home_assistant.mqtt_host / mqtt_user / mqtt_password`
- `roi` — crop to the play area once you see a debug video (start full-frame)

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

## 3. Register known people

Person identification is handled by the **FaceID Community integration** in
Home Assistant. See
[`HOME_ASSISTANT.md` — FaceID](HOME_ASSISTANT.md#faceid--async-person-identification)
for full setup steps.

**Short version:**

1. Install the **Frigate FaceID** HACS integration in Home Assistant.
2. Add each person in FaceID's HA configuration panel with 5–20 face photos.
3. Set `home_assistant.faceid_enabled: true` in `config.yaml` and restart the
   worker.

> Person management happens entirely in the FaceID HA panel — there
> is no code-level limit on the number of people you can track.

## 4. Get clips in

**Manual:** copy any `.mp4` into `inbox/`, or grab one from the camera:

```bash
python -m juggle_tracker.cli record --seconds 30
```

**Automatic (recommended):** run the Frigate inbox bridge. It listens for
completed person events, downloads the event clip from Frigate, and writes it
to the shared `inbox/` directory. Home Assistant is not involved in video
capture; see [`DEPLOY.md`](DEPLOY.md) and [`HOME_ASSISTANT.md`](HOME_ASSISTANT.md).

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

The native `systemd` worker is the supported runtime on the target CPU because
it has the lowest overhead and simplest access to the host inbox.

## Tuning accuracy

Process a real clip with `--debug-video` and watch:
- **Ball not detected** → lower `models.ball_conf`, raise `infer_long_edge`.
- **Overcounting jitter** → raise `juggle.min_arc_px`.
- **Hand touches counted** → raise `juggle.contact_radius_px` accuracy by
  improving pose (larger `infer_long_edge`) or check `illegal_keypoints`.
- **Dropped ball not ending streak** → lower `juggle.lost_frames_reset`.
- **Wrong/Unknown person** → add more face photos in FaceID's HA panel;
  if FaceID isn't firing at all, check FaceID logs in HA. After correcting a
  clip's FaceID label, select that archived clip in **Queue & Reprocess** and
  run it again to update its attribution.

## Publishing to your private GitHub repo (later)

```bash
git add -A && git commit -m "feat: initial juggle tracker"   # (already committed)
git remote add origin git@github.com:<you>/soccer-juggle-tracker.git
git push -u origin main
```
