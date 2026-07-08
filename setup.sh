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
    echo "==> Installing system prerequisites (sudo apt-get: ffmpeg, python venv)"
    sudo apt-get update -qq
    sudo apt-get install -y -qq ffmpeg python3-venv python3-pip libgl1 libglib2.0-0
  fi
fi

# The ML stack (torch/ultralytics/onnxruntime/insightface/openvino) only has
# wheels for Python 3.10-3.12. A too-new system Python (e.g. 3.13/3.14) will make
# pip fail to resolve. Pick a compatible interpreter for the venv.
pick_python() {
  for p in python3.12 python3.11 python3.10; do
    command -v "$p" >/dev/null 2>&1 && { echo "$p"; return; }
  done
  # Fall back to python3 only if it's within the supported range.
  local v
  v=$(python3 -c 'import sys;print("%d%02d"%sys.version_info[:2])' 2>/dev/null || echo 0)
  if [ "$v" -ge 310 ] && [ "$v" -le 312 ]; then echo python3; fi
}
PYBIN="$(pick_python)"
if [ -z "$PYBIN" ]; then
  echo "ERROR: No compatible Python (3.10-3.12) found; system python3 is $(python3 -V 2>&1)."
  echo "       The ML deps have no wheels for 3.13+/very-new versions. Install one, e.g.:"
  echo "         sudo add-apt-repository -y ppa:deadsnakes/ppa && sudo apt-get update"
  echo "         sudo apt-get install -y python3.12 python3.12-venv"
  echo "       Or use uv/pyenv to provide python3.12, then re-run ./setup.sh."
  exit 1
fi
echo "==> Using interpreter: $PYBIN ($($PYBIN -V 2>&1))"

echo "==> Creating virtualenv (.venv)"
"$PYBIN" -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate

echo "==> Upgrading pip"
pip install --quiet --upgrade pip

echo "==> Installing requirements (CPU-only torch on Linux x86_64)"
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
