#!/usr/bin/env bash
# Install (or uninstall) the juggle-tracker batch worker as a launchd agent.
#
#   deploy/install-launchd.sh            # install + load
#   deploy/install-launchd.sh uninstall  # stop + remove
#
# Substitutes the absolute repo path into the plist template so the agent works
# regardless of where you cloned the repo.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.abeatte.juggletracker"
AGENTS_DIR="$HOME/Library/LaunchAgents"
PLIST="$AGENTS_DIR/$LABEL.plist"
TEMPLATE="$REPO/deploy/$LABEL.plist.template"

uninstall() {
  echo "==> Unloading $LABEL"
  launchctl unload "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Removed $PLIST"
}

if [[ "${1:-}" == "uninstall" ]]; then
  uninstall
  exit 0
fi

if [[ ! -x "$REPO/.venv/bin/python" ]]; then
  echo "ERROR: $REPO/.venv not found. Run ./setup.sh first." >&2
  exit 1
fi

mkdir -p "$AGENTS_DIR"
echo "==> Rendering plist for repo: $REPO"
sed "s|__REPO__|$REPO|g" "$TEMPLATE" > "$PLIST"

# Reload if already loaded.
launchctl unload "$PLIST" 2>/dev/null || true
echo "==> Loading $LABEL"
launchctl load "$PLIST"

echo ""
echo "Installed and started. Useful commands:"
echo "  launchctl list | grep $LABEL          # is it running?"
echo "  tail -f $REPO/worker.log              # live worker log"
echo "  deploy/install-launchd.sh uninstall   # stop + remove"
