#!/usr/bin/env bash
# servicer -- manage soccer-juggle-tracker services
#
# Usage:
#   ./servicer.sh --start     <service>
#   ./servicer.sh --shut-down <service>
#   ./servicer.sh --restart   <service>
#   ./servicer.sh --check     <service>
#
# Services: mosquitto | frigate | homeassistant | juggletracker | all

set -euo pipefail

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
usage() {
    echo "Usage: $0 --start|--shut-down|--restart|--check mosquitto|frigate|homeassistant|juggletracker|machinetelemetry|all" >&2
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
# Per-service checks
# ---------------------------------------------------------------------------
check_mosquitto()     { check_docker_service ~/mosquitto mosquitto; }
check_frigate()       { check_docker_service ~/frigate frigate; check_docker_service ~/frigate frigate-inbox-bridge;  }
check_homeassistant() { check_docker_service ~/homeassistant homeassistant; }

check_juggletracker() {
    log "Checking juggletracker..."
    systemctl --user status juggle-tracker.service
    journalctl --user -u juggle-tracker.service -n 5 --no-pager
}

check_machinetelemetry() {
    log "Checking machinetelemetry..."
    systemctl --user status machinetelemetry.service
    journalctl --user -u machinetelemetry.service -n 5 --no-pager
}

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
[[ $# -eq 2 ]] || usage

COMMAND="$1"
SERVICE="$2"

case "$COMMAND" in
    --start|--shut-down|--restart|--check) ;;
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
esac

log "Done."
