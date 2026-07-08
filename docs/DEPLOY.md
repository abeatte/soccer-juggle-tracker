# Deployment — how it runs (Ubuntu)

The tracker is one **long-running worker** (`juggle_tracker.cli watch`) that
watches `inbox/` for clips, processes them, writes scores to SQLite, and
publishes to Home Assistant over MQTT. Clips arrive because HA's `camera.record`
(or the Reolink integration) drops `.mp4` files into that folder. That data flow
is identical no matter how you run the worker.

You're on **Ubuntu on a 2016 Intel MacBook Pro**, alongside HA and Matter in
Docker. Two good ways to run the worker:

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

## Recommendation

Run **native + systemd + OpenVINO** on the MacBook now — least overhead on weak
hardware, and it's ready to go. Switch to **Docker** if you later want unified
compose management or move the workload to a dedicated Apple Silicon Mac Mini /
Jetson, where a containerized (and GPU-accelerated) build becomes attractive.
