#!/usr/bin/env bash
# One-time setup: venv + deps + model weights.
# Target OS: Ubuntu (Linux x86_64). Safe to re-run.
#   ./setup.sh              # venv + deps + download .pt weights
#   ./setup.sh --openvino   # also export models to OpenVINO (Intel CPU speedup)
set -euo pipefail
cd "$(dirname "$0")"

# System prerequisites (Ubuntu): python venv + ffmpeg.
if command -v apt-get >/dev/null 2>&1; then
  if ! command -v ffmpeg >/dev/null 2>&1 || ! python3 -m venv --help >/dev/null 2>&1; then
    echo "==> Installing system prerequisites (sudo apt-get: ffmpeg, python3-venv)"
    sudo apt-get update -qq
    sudo apt-get install -y -qq ffmpeg python3-venv python3-pip libgl1 libglib2.0-0
  fi
fi

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

if [[ "${1:-}" == "--openvino" ]]; then
  echo "==> Exporting models to OpenVINO (Intel CPU acceleration)"
  python tools/export_openvino.py || echo "  (OpenVINO export failed; continuing with .pt models)"
fi

echo ""
echo "Setup complete. Next:"
echo "  cp config.example.yaml config.yaml   # then edit RTSP + MQTT"
echo "  source .venv/bin/activate"
echo "  python -m juggle_tracker.cli enroll --name \"Kid1\""
