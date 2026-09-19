#!/usr/bin/env bash
# Install (or uninstall) the juggle-tracker batch worker as a systemd service on
# Ubuntu. Defaults to a per-user service (no root needed) with lingering enabled
# so it also runs at boot.
#
#   deploy/install-systemd.sh              # install + start (user service)
#   deploy/install-systemd.sh uninstall    # stop + remove
#   deploy/install-systemd.sh --system     # install as a system service (sudo)
#
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
UNIT="juggle-tracker.service"
TEMPLATE="$REPO/deploy/$UNIT"
MODE="user"
[[ "${1:-}" == "--system" ]] && MODE="system"

render() { sed "s|__REPO__|$REPO|g" "$TEMPLATE"; }

if [[ "${1:-}" == "uninstall" ]]; then
  echo "==> Uninstalling ($UNIT)"
  systemctl --user disable --now "$UNIT" 2>/dev/null || true
  rm -f "$HOME/.config/systemd/user/$UNIT"
  systemctl --user daemon-reload 2>/dev/null || true
  if [[ -f "/etc/systemd/system/$UNIT" ]]; then
    sudo systemctl disable --now "$UNIT" 2>/dev/null || true
    sudo rm -f "/etc/systemd/system/$UNIT"
    sudo systemctl daemon-reload
  fi
  echo "Removed."
  exit 0
fi

if [[ ! -x "$REPO/.venv/bin/python" ]]; then
  echo "ERROR: $REPO/.venv not found. Run ./setup.sh first." >&2
  exit 1
fi

if [[ "$MODE" == "system" ]]; then
  echo "==> Installing SYSTEM service (needs sudo), running as user: $USER"
  # A system service must know which user to run as.
  render | sudo tee "/etc/systemd/system/$UNIT" >/dev/null
  sudo sed -i "/^\[Service\]/a User=$USER\nGroup=$USER" "/etc/systemd/system/$UNIT"
  sudo sed -i "s|^WantedBy=default.target|WantedBy=multi-user.target|" "/etc/systemd/system/$UNIT"
  sudo systemctl daemon-reload
  sudo systemctl enable --now "$UNIT"
  echo ""
  echo "Installed. Commands:"
  echo "  systemctl status $UNIT"
  echo "  journalctl -u $UNIT -f"
else
  echo "==> Installing USER service for $USER"
  mkdir -p "$HOME/.config/systemd/user"
  render > "$HOME/.config/systemd/user/$UNIT"
  systemctl --user daemon-reload
  systemctl --user enable --now "$UNIT"
  # Start at boot without an active login session.
  loginctl enable-linger "$USER" >/dev/null 2>&1 || \
    echo "  (note: could not enable-linger; service starts on your next login)"
  echo ""
  echo "Installed. Commands:"
  echo "  systemctl --user status $UNIT"
  echo "  journalctl --user -u $UNIT -f      # live worker log"
  echo "  tail -f $REPO/worker.log"
  echo "  deploy/install-systemd.sh uninstall"
fi
