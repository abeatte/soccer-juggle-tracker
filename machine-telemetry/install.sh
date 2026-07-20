#!/usr/bin/env bash
# Install (or uninstall) the machine-telemetry publisher as a systemd --user
# service on Ubuntu. Reuses the repo's shared .venv (created by ./setup.sh);
# paho-mqtt is already a project dependency. Lingering is enabled so it also
# starts at boot without an active login.
#
#   machine-telemetry/install.sh            # install + start (user service)
#   machine-telemetry/install.sh uninstall  # stop + remove
#
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
DIR="$REPO/machine-telemetry"
UNIT="machine-telemetry.service"
TEMPLATE="$DIR/$UNIT"

render() { sed "s|__REPO__|$REPO|g" "$TEMPLATE"; }

if [[ "${1:-}" == "uninstall" ]]; then
  echo "==> Uninstalling ($UNIT)"
  systemctl --user disable --now "$UNIT" 2>/dev/null || true
  rm -f "$HOME/.config/systemd/user/$UNIT"
  systemctl --user daemon-reload 2>/dev/null || true
  echo "Removed."
  exit 0
fi

if [[ ! -x "$REPO/.venv/bin/python" ]]; then
  echo "ERROR: $REPO/.venv not found. Run ./setup.sh in the repo root first." >&2
  exit 1
fi

# paho-mqtt ships with the repo requirements; verify it's importable.
if ! "$REPO/.venv/bin/python" -c "import paho.mqtt.client" 2>/dev/null; then
  echo "==> paho-mqtt missing in .venv; installing it"
  "$REPO/.venv/bin/pip" install --quiet "paho-mqtt>=2.0"
fi

# Config file (holds the MQTT password) — created once, kept 0600, gitignored.
if [[ ! -f "$DIR/machine-telemetry.env" ]]; then
  cp "$DIR/config.example.env" "$DIR/machine-telemetry.env"
  chmod 600 "$DIR/machine-telemetry.env"
  echo ">> Created machine-telemetry/machine-telemetry.env — EDIT IT (set MQTT_PASS) before starting."
fi

mkdir -p "$HOME/.config/systemd/user"
render > "$HOME/.config/systemd/user/$UNIT"
systemctl --user daemon-reload
systemctl --user enable "$UNIT"
loginctl enable-linger "$USER" >/dev/null 2>&1 || \
  echo "  (note: could not enable-linger; service starts on your next login)"

echo
echo "Installed. Next:"
echo "  1. Edit $DIR/machine-telemetry.env   (set MQTT_PASS, confirm MQTT_USER)"
echo "  2. systemctl --user start $UNIT"
echo "  3. systemctl --user status $UNIT"
echo "  4. journalctl --user -u $UNIT -f"
echo "  Uninstall: machine-telemetry/install.sh uninstall"
