#!/usr/bin/env bash
# servicer -- manage soccer-juggle-tracker services
#
# Usage:
#   ./servicer.sh --start     <service>
#   ./servicer.sh --shut-down <service>
#   ./servicer.sh --restart   <service>
#   ./servicer.sh --check     <service>
#   ./servicer.sh --deploy    <service>
#
# Services: mosquitto | frigate | homeassistant | juggletracker | machinetelemetry | all
#
# --deploy copies source files from this repo into the deployed service directory.
# It only copies the specific files listed in each deploy_* function (allowlist).
# It never deletes files that only exist in the deployed directory (runtime data,
# DBs, secrets, logs, etc.).
# .env files are always skipped — those hold live secrets and are gitignored.

set -euo pipefail

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
usage() {
    echo "Usage: $0 --start|--shut-down|--restart|--check|--deploy mosquitto|frigate|homeassistant|juggletracker|machinetelemetry|all" >&2
    exit 1
}

log()  { echo "[servicer] $*"; }
err()  { echo "[servicer] ERROR: $*" >&2; }
die()  { err "$*"; exit 1; }

# Show status and last 5 log lines for a docker-compose service.
check_docker_service() {
    local dir="$1"
    local label="$2"
    log "Checking $label..."
    (cd "$dir" && docker compose ps)
    (cd "$dir" && docker compose logs --tail=5 "$label")
}

# Verify a docker-compose service is stopped (no running containers).
docker_verify_down() {
    local dir="$1"
    local label="$2"
    log "Verifying $label containers are stopped..."
    docker ps
    if (cd "$dir" && docker compose ps | grep -qE 'Up|running'); then
        err "$label containers still appear to be running after shut-down."
        return 1
    else
        log "$label is stopped."
    fi
}

# ---------------------------------------------------------------------------
# Per-service operations
# ---------------------------------------------------------------------------
start_mosquitto() {
    local dir=~/mosquitto
    log "Starting mosquitto from $dir..."
    cd "$dir"
    docker compose config
    docker compose up -d
}

stop_mosquitto() {
    local dir=~/mosquitto
    log "Stopping mosquitto..."
    cd "$dir"
    docker compose down
    docker_verify_down "$dir" mosquitto
}

start_frigate() {
    local dir=~/frigate
    log "Starting frigate from $dir..."
    cd "$dir"
    docker compose config
    docker compose up -d --build
}

stop_frigate() {
    local dir=~/frigate
    log "Stopping frigate..."
    cd "$dir"
    docker compose down
    docker_verify_down "$dir" frigate
}

start_homeassistant() {
    local dir=~/homeassistant
    log "Starting homeassistant from $dir..."
    cd "$dir"
    docker compose config
    docker compose up -d
}

stop_homeassistant() {
    local dir=~/homeassistant
    log "Stopping homeassistant..."
    cd "$dir"
    docker compose down
    docker_verify_down "$dir" homeassistant
}

start_juggletracker() {
    local dir=~/juggletracker
    log "Starting juggletracker from $dir..."
    cd "$dir"
    python3 -m py_compile src/juggletracker/config.py
    # shellcheck disable=SC1091
    source .venv/bin/activate
    python -m juggletracker.cli doctor
    deploy/install-systemd.sh
}

stop_juggletracker() {
    log "Stopping juggletracker..."
    systemctl --user disable --now juggle-tracker.service
    log "Verifying juggletracker is stopped..."
    if systemctl --user is-active --quiet juggle-tracker.service; then
        err "juggle-tracker.service is still active after disable --now."
        return 1
    else
        log "juggle-tracker.service is stopped."
    fi
}

start_machinetelemetry() {
    local dir=~/projects/soccer-juggle-tracker/machinetelemetry
    log "Starting machinetelemetry from $dir..."
    "$dir/install.sh"
    systemctl --user start machinetelemetry.service
}

stop_machinetelemetry() {
    log "Stopping machinetelemetry..."
    systemctl --user disable --now machinetelemetry.service
    log "Verifying machinetelemetry is stopped..."
    if systemctl --user is-active --quiet machinetelemetry.service; then
        err "machinetelemetry.service is still active after disable --now."
        return 1
    else
        log "machinetelemetry.service is stopped."
    fi
}

# ---------------------------------------------------------------------------
# Per-service deploy
# ---------------------------------------------------------------------------
# deploy_rsync SRC_DIR DEST_DIR [INCLUDE_PATTERNS...]
#   Copies files under SRC_DIR into DEST_DIR using rsync allowlist mode:
#     - Only files matching an INCLUDE_PATTERN are transferred.
#     - If no INCLUDE_PATTERNS are given, all files are synced (open allowlist).
#     - Never deletes files that only exist in DEST_DIR.
#     - Always skips *.env files (live secrets, gitignored), __pycache__, *.pyc,
#       and .git artefacts regardless of the include list.
deploy_rsync() {
    local src="$1"
    local dest="$2"
    shift 2
    local include_patterns=("$@")

    log "Deploying $src -> $dest"

    local rsync_args=(
        -av
        --no-group   # don't try to chgrp destination; deploy user may lack permission
        # Always-excluded files — live secrets and build artefacts.
        --exclude='*.env'
        --exclude='.env_*'
        --exclude='*.env_example'
        --exclude='__pycache__/'
        --exclude='*.pyc'
        --exclude='.git/'
        --exclude='.gitignore'
    )

    if [[ ${#include_patterns[@]} -gt 0 ]]; then
        # Allowlist mode: include the listed patterns, exclude everything else.
        # Directories must be included so rsync can descend into them; the
        # trailing --exclude='*' drops anything not explicitly included.
        rsync_args+=('--include=*/')
        for pat in "${include_patterns[@]}"; do
            rsync_args+=(--include="$pat")
        done
        rsync_args+=('--exclude=*')
    fi

    # Trailing slash on src so rsync copies the *contents* into dest.
    rsync "${rsync_args[@]}" "${src%/}/" "$dest/"
    log "Deploy of $(basename "$src") complete."
}

deploy_mosquitto() {
    local repo
    repo="$(cd "$(dirname "$0")" && pwd)"
    deploy_rsync "$repo/mosquitto" ~/mosquitto \
        'docker-compose.yml' \
        'config/mosquitto.conf'
    # password.txt is intentionally excluded — holds live broker passwords.
}

deploy_frigate() {
    local repo
    repo="$(cd "$(dirname "$0")" && pwd)"
    deploy_rsync "$repo/frigate" ~/frigate \
        'docker-compose.yml'       \
        'config/config.yaml'       \
        'inbox-bridge/bridge.py'   \
        'inbox-bridge/requirements.txt' \
        'inbox-bridge/Dockerfile'
}

deploy_homeassistant() {
    local repo
    repo="$(cd "$(dirname "$0")" && pwd)"
    # The live HA config lives at ~/homeassistant/config/ and is managed by HA
    # itself.  We only sync the files we own: docker-compose, the packages drop-
    # in, and the dashboards.
    deploy_rsync "$repo/homeassistant" ~/homeassistant \
        'docker-compose.yml'                          \
        'packages/juggle_tracker.yaml'                \
        'configs/configuration.yaml'
}

deploy_juggletracker() {
    local repo
    repo="$(cd "$(dirname "$0")" && pwd)"
    deploy_rsync "$repo/juggletracker" ~/juggletracker \
        'setup.sh'                          \
        'pyproject.toml'                    \
        'requirements.txt'                  \
        'src/juggletracker/*.py'            \
        'tests/*.py'                        \
        'tools/*.py'                        \
        'deploy/*.sh'                       \
        'cli.py'                            \
        'config.yaml'                       \
        'calibration_overrides.yaml'
}

deploy_machinetelemetry() {
    local repo
    repo="$(cd "$(dirname "$0")" && pwd)"
    deploy_rsync "$repo/machinetelemetry" ~/machinetelemetry \
        'telemetry_publisher.py'       \
        'install.sh'                   \
        'machinetelemetry.service'
    # machine-telemetry.env is already excluded by the *.env rule in deploy_rsync.
}

# ---------------------------------------------------------------------------
# Per-service checks
# ---------------------------------------------------------------------------
check_mosquitto()     { check_docker_service ~/mosquitto mosquitto; }
check_frigate()       { check_docker_service ~/frigate frigate; check_docker_service ~/frigate frigate-inbox-bridge;  }
check_homeassistant() { check_docker_service ~/homeassistant homeassistant; }

check_juggletracker() {
    log "Checking juggletracker..."
    systemctl --user status juggle-tracker.service --no-pager || true
    journalctl --user -u juggle-tracker.service -n 5 --no-pager
}

check_machinetelemetry() {
    log "Checking machinetelemetry..."
    systemctl --user status machinetelemetry.service --no-pager || true
    journalctl --user -u machinetelemetry.service -n 5 --no-pager
}

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
[[ $# -eq 2 ]] || usage

COMMAND="$1"
SERVICE="$2"

case "$COMMAND" in
    --start|--shut-down|--restart|--check|--deploy) ;;
    *) usage ;;
esac

case "$SERVICE" in
    mosquitto|frigate|homeassistant|juggletracker|machinetelemetry|all) ;;
    *) die "Unknown service '$SERVICE'. Must be one of: mosquitto frigate homeassistant juggletracker machinetelemetry all" ;;
esac

ALL_SERVICES_DOWN=(machinetelemetry juggletracker homeassistant frigate mosquitto)
ALL_SERVICES=(mosquitto frigate homeassistant juggletracker machinetelemetry)

case "$COMMAND" in
    --start)
        if [[ "$SERVICE" == "all" ]]; then
            for svc in "${ALL_SERVICES[@]}"; do "start_${svc}"; done
        else
            "start_${SERVICE}"
            "check_${SERVICE}"
        fi
        ;;
    --shut-down)
        if [[ "$SERVICE" == "all" ]]; then
            for svc in "${ALL_SERVICES_DOWN[@]}"; do "stop_${svc}"; done
        else
            "stop_${SERVICE}"
        fi
        ;;
    --restart)
        if [[ "$SERVICE" == "all" ]]; then
            for svc in "${ALL_SERVICES_DOWN[@]}"; do
                log "Stopping $svc..."
                "stop_${svc}"
            done
            for svc in "${ALL_SERVICES[@]}"; do
                log "Starting $svc..."
                "start_${svc}"
                "check_${svc}"
            done
        else
            log "Restarting $SERVICE..."
            "stop_${SERVICE}"
            log "--- $SERVICE stopped; starting ---"
            "start_${SERVICE}"
            "check_${SERVICE}"
        fi
        ;;
    --check)
        if [[ "$SERVICE" == "all" ]]; then
            for svc in "${ALL_SERVICES[@]}"; do "check_${svc}"; done
        else
            "check_${SERVICE}"
        fi
        ;;
    --deploy)
        if [[ "$SERVICE" == "all" ]]; then
            for svc in "${ALL_SERVICES[@]}"; do "deploy_${svc}"; done
        else
            "deploy_${SERVICE}"
        fi
        ;;
esac

log "Done."
