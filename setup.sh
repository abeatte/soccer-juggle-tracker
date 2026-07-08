#!/usr/bin/env bash
# One-time setup: venv + deps + model weights.
# Safe to re-run.
set -euo pipefail
cd "$(dirname "$0")"

echo "==> Creating virtualenv (.venv)"
python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate

echo "==> Upgrading pip"
pip install --quiet --upgrade pip

echo "==> Installing requirements (CPU-only torch on Intel macOS)"
pip install --quiet -r requirements.txt

echo "==> Downloading YOLO model weights into ./models"
mkdir -p models
python - <<'PY'
from ultralytics import YOLO
# Downloads to the ultralytics cache, then we copy the weights into ./models.
import shutil, os
for name in ("yolo11n.pt", "yolo11n-pose.pt"):
    m = YOLO(name)                      # triggers download
    src = m.ckpt_path if hasattr(m, "ckpt_path") else name
    dst = os.path.join("models", name)
    if os.path.abspath(src) != os.path.abspath(dst):
        try:
            shutil.copy(src, dst)
        except Exception:
            # Ultralytics already placed it in CWD; ensure it's under models/
            if os.path.exists(name):
                shutil.move(name, dst)
    print("  ok:", dst)
PY

echo "==> InsightFace face model (buffalo_s) will auto-download on first enroll."
mkdir -p inbox processed data enroll_images

echo ""
echo "Setup complete. Next:"
echo "  cp config.example.yaml config.yaml   # then edit RTSP + MQTT"
echo "  source .venv/bin/activate"
echo "  python -m juggle_tracker.cli enroll --name \"Kid1\""
