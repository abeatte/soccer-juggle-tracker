# Deployment — how it runs (Ubuntu)

The tracker is one **long-running worker** (`juggle_tracker.cli watch`) that
watches `inbox/` for clips, processes them, writes scores to SQLite, and
publishes to Home Assistant over MQTT. Clips arrive because HA's `camera.record`
(or the Reolink integration) drops `.mp4` files into that folder. That data flow
is identical no matter how you run the worker.

You're on **Ubuntu on a 2016 Intel MacBook Pro**, alongside HA and Matter in
Docker. Two good ways to run the worker:

## Pre-deploy: verify the box first (no repo needed)

Before installing anything, confirm the host can run this. Copy the single
self-contained script `deploy/box-precheck.sh` to the box (it has **no** repo or
Python dependency — stock tools only) and run it:

```bash
# camera / MQTT / inbox checks activate when you pass their env vars:
RTSP_URL='rtsp://user:pass@192.168.1.50:554/h264Preview_01_main' \
MQTT_HOST=127.0.0.1 MQTT_USER=mqtt MQTT_PASS=secret \
INBOX_DIR=/srv/juggle_inbox \
bash box-precheck.sh
```

It checks OS/arch, CPU cores + **AVX2** (OpenVINO speed), RAM, disk, current CPU
headroom (HA/Matter already running), Python 3.9+/venv, ffmpeg, OpenCV runtime
libs, `/dev/video0`, systemd user-manager + lingering + cgroup v2, the Docker
daemon + a Home Assistant container, live **RTSP** decode from the camera, **MQTT**
broker reachability/auth, and shared-inbox writability. Exit code is non-zero if
any hard check fails. This is the fastest way to de-risk a box you have limited
access to.

For **tuning** (not just pass/fail), also run `deploy/box-telemetry.sh` — a
read-only dump of CPU governor/frequency, thermals/throttling, VA-API hardware
decode availability, RAM, disk write speed, current HA/Matter load, and a probe
of the camera's main/sub streams (resolution/fps/codec). Paste its output back to
tune `infer_long_edge` / `person_stride` / ROI / which stream to analyze / the
CPU governor for this exact box:

```bash
RTSP_MAIN='rtsp://user:pass@CAM_IP:554/h264Preview_01_main' \
RTSP_SUB='rtsp://user:pass@CAM_IP:554/h264Preview_01_sub' \
DDTEST=1 bash box-telemetry.sh
```

## Option A — native + systemd (recommended, already wired)

Simplest and lowest-overhead on this CPU-bound, GPU-less box: a native process
gets full, unmediated CPU, and webcam enrollment (`/dev/video0`) just works.

```bash
./setup.sh --openvino          # venv + deps + models + Intel acceleration
cp config.example.yaml config.yaml && $EDITOR config.yaml
deploy/install-systemd.sh      # user service, starts now + at boot
```

Manage it:
```bash
systemctl --user status juggle-tracker.service
journalctl --user -u juggle-tracker.service -f
deploy/install-systemd.sh uninstall
```

The unit runs at `Nice=10`, `IOSchedulingClass=idle`, and `CPUQuota=150%` so it
can never starve HA/Matter. For a boot-without-login system service:
`deploy/install-systemd.sh --system` (uses sudo, runs as your user).

## Option B — Docker (good parity with HA/Matter)

On **Linux**, Docker uses the host kernel directly — **no VM overhead** (unlike
Docker Desktop on macOS/Windows). So containerizing costs you almost nothing here
and gives one consistent `compose` lifecycle with your HA/Matter stack, plus
pinned Python/ffmpeg. There's no GPU either way, so nothing is lost on
acceleration. Trade-off: webcam enrollment needs a `--device /dev/video0`
passthrough (works on Linux), or just enroll with `--images`.

A minimal setup (not yet in the repo — ask and I'll add it):

```dockerfile
# Dockerfile
FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN pip install -e .
CMD ["python", "-m", "juggle_tracker.cli", "watch"]
```

```yaml
# docker-compose.yml (joins HA's MQTT + shares the inbox)
services:
  juggle-tracker:
    build: .
    restart: unless-stopped
    cpus: "1.5"                      # be nice to HA/Matter
    volumes:
      - ./config.yaml:/app/config.yaml:ro
      - ./data:/app/data
      - /srv/juggle_inbox:/app/inbox # SAME host dir HA records into
      - ./models:/app/models
    # If your MQTT broker is another container, put them on one network.
```

Point `home_assistant.mqtt_host` at the broker's reachable address (the host IP
or the broker's compose service name if on the same network).

## The shared `inbox/` — the one integration detail

HA runs in Docker and can only write to paths bind-mounted into its container.
So pick **one host directory** both sides see:

1. Choose a host path, e.g. `/srv/juggle_inbox`.
2. **HA container**: bind-mount it (e.g. add `- /srv/juggle_inbox:/media/juggle_inbox`
   to HA's compose/volumes) and set the `camera.record` automation's `filename`
   to `/media/juggle_inbox/clip_....mp4` (the path *inside* the HA container).
3. **Tracker**:
   - Native (Option A): set `capture.inbox_dir: /srv/juggle_inbox` in `config.yaml`.
   - Docker (Option B): bind-mount `- /srv/juggle_inbox:/app/inbox`.

Now HA writes a clip → it appears in the tracker's inbox → the worker processes
it and moves it to `processed/`.

## Intel-CPU acceleration (OpenVINO) — the "optimal" bit

This is an Intel CPU, and Intel's **OpenVINO** runtime accelerates inference
markedly over stock torch-CPU (commonly ~2-3x for these nano models) — the
highest-leverage way to approach real time without new hardware.

```bash
python tools/export_openvino.py            # FP32 (safe)
# or, faster but validate accuracy:  python tools/export_openvino.py --int8
```

Then point `config.yaml` at the exported dirs:

```yaml
models:
  detector: "models/yolo11n_openvino_model"
  pose:     "models/yolo11n-pose_openvino_model"
```

Re-run `python tools/eval.py ground_truth.csv` to confirm accuracy held (INT8 in
particular can nick small/blurry-ball recall — check before keeping it).

## Low-power profile — Intel i7-3740QM (Ivy Bridge, 2012, no AVX2)

The confirmed target is a **mid-2012 MacBook Pro** (i7-3740QM, 4c/8t, **no
AVX2/FMA**, 15 GB RAM, bare metal). It works well for this batch pipeline, with
these hardware-specific realities:

- **RAM is not a constraint** (15 GB; models + torch need ~1–2 GB).
- **CPU is the limit.** Expect ~1–3 analysis FPS, so a 20–30 s clip takes roughly
  **2–5 minutes** to process. Fine for delayed scoring; not real-time.
- **OpenVINO gives little here** — its speedup depends on AVX2. Keep the `.pt`
  models; only switch to exported OpenVINO dirs if a benchmark on this box shows a
  real win (see `tools/eval.py`), or after moving to AVX2+ hardware.
- **Thermals:** sustained load will spin the fans and may throttle a 13-year-old
  laptop. The systemd unit runs the worker at `Nice=10` + idle IO + `CPUQuota` so
  HA/Matter stay responsive; leave the machine ventilated.

Recommended `config.yaml` (already the defaults in `config.example.yaml`):

```yaml
capture:    { clip_seconds: 20 }
processing: { infer_long_edge: 640, person_stride: 4, torch_threads: 4 }
roi:        [ ... tight crop around the play area ... ]   # big win on slow CPU
models:     # keep the .pt weights (NOT OpenVINO) on this no-AVX2 CPU
  detector: "models/yolo11n.pt"
  pose:     "models/yolo11n-pose.pt"
```

If you outgrow the ~minutes-per-clip latency, the clean upgrade is a small
dedicated box — an **Apple Silicon Mac Mini** (MPS) or **NVIDIA Jetson Orin**
— where the realtime pipeline and a containerized, GPU-accelerated build become
attractive; keep this 2012 box as the HA/Matter hub.

## Recommendation

Run **native + systemd** on the MacBook now — least overhead on weak hardware,
and it's ready to go. Use the **low-power profile** above (it's the default).
Switch to **Docker** if you later want unified compose management, and revisit
**OpenVINO / GPU** only on AVX2+ or dedicated hardware.
