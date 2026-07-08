#!/usr/bin/env bash
# box-precheck.sh — verify an Ubuntu host can run the Soccer Juggle Tracker,
# WITHOUT installing the project. Self-contained: uses only stock tools (+ ffmpeg,
# which the project needs anyway). Safe, read-only. Copy just this one file to the
# box and run it.
#
# Usage:
#   bash box-precheck.sh
#   RTSP_URL='rtsp://user:pass@192.168.1.50:554/h264Preview_01_main' \
#   MQTT_HOST=127.0.0.1 MQTT_PORT=1883 MQTT_USER=mqtt MQTT_PASS=secret \
#   INBOX_DIR=/srv/juggle_inbox bash box-precheck.sh
#
# Optional env vars enable the camera / MQTT / inbox checks; omit to skip them.

pass(){ printf '  [\033[32m✓\033[0m] PASS  %-26s %s\n' "$1" "$2"; }
warn(){ printf '  [\033[33m!\033[0m] WARN  %-26s %s\n' "$1" "$2"; WARNED=1; }
fail(){ printf '  [\033[31m✗\033[0m] FAIL  %-26s %s\n' "$1" "$2"; FAILED=1; }
have(){ command -v "$1" >/dev/null 2>&1; }

echo "Soccer Juggle Tracker — on-box precheck"
echo "======================================================================"

# --- OS / arch ------------------------------------------------------------
ARCH=$(uname -m)
OS=$( (. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME") || uname -s)
if [ "$ARCH" = "x86_64" ]; then pass "os/arch" "$OS ($ARCH)"
else warn "os/arch" "$OS ($ARCH) — expected x86_64; OpenVINO wheels target x86_64"; fi

# --- CPU cores + AVX (OpenVINO speed) ------------------------------------
CORES=$(nproc 2>/dev/null || echo '?')
if grep -qm1 avx2 /proc/cpuinfo 2>/dev/null; then
  pass "cpu" "$CORES cores, AVX2 present (OpenVINO will accelerate well)"
elif grep -qm1 avx /proc/cpuinfo 2>/dev/null; then
  warn "cpu" "$CORES cores, AVX only (OpenVINO ok, less speedup)"
else
  warn "cpu" "$CORES cores, no AVX detected (OpenVINO will be slow)"
fi

# --- RAM ------------------------------------------------------------------
if have free; then
  MEM_MB=$(free -m | awk '/^Mem:/{print $2}')
  AVAIL_MB=$(free -m | awk '/^Mem:/{print $7}')
  if [ "${MEM_MB:-0}" -ge 4000 ]; then pass "memory" "${MEM_MB}MB total, ${AVAIL_MB}MB available"
  else warn "memory" "${MEM_MB}MB total — tight alongside HA/Matter (torch+models want ~1-2GB)"; fi
fi

# --- Disk (home) ----------------------------------------------------------
AVAIL_GB=$(df -Pk "$HOME" 2>/dev/null | awk 'NR==2{printf "%.1f", $4/1048576}')
if awk "BEGIN{exit !(${AVAIL_GB:-0} >= 6)}"; then pass "disk" "${AVAIL_GB}GB free in \$HOME (deps+models ~3-5GB)"
else warn "disk" "${AVAIL_GB}GB free in \$HOME — deps+models need ~3-5GB"; fi

# --- CPU headroom (HA/Matter already running) ----------------------------
if have uptime; then
  LOAD=$(uptime | sed 's/.*load average: //')
  IDLE=$( (have top && top -bn1 2>/dev/null | awk -F',' '/Cpu\(s\)/{for(i=1;i<=NF;i++) if($i ~ /id/){gsub(/[^0-9.]/,"",$i); print $i}}') )
  if [ -n "$IDLE" ]; then
    if awk "BEGIN{exit !(${IDLE:-0} >= 30)}"; then pass "cpu headroom" "${IDLE}% idle now (load: $LOAD)"
    else warn "cpu headroom" "only ${IDLE}% idle (load: $LOAD) — batch worker will be slow but is nice/idle-priority"; fi
  else warn "cpu headroom" "load average: $LOAD"; fi
fi

# --- Python + venv + pip --------------------------------------------------
if have python3; then
  PYV=$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null)
  if python3 -c 'import sys;raise SystemExit(0 if sys.version_info[:2]>=(3,9) else 1)' 2>/dev/null; then
    pass "python3" "$PYV"
  else fail "python3" "$PYV — need >= 3.9"; fi
  if python3 -m venv --help >/dev/null 2>&1; then pass "python3-venv" "available"
  else fail "python3-venv" "missing — sudo apt-get install python3-venv"; fi
else fail "python3" "not found — sudo apt-get install python3 python3-venv python3-pip"; fi
have pip3 && pass "pip3" "$(pip3 --version 2>/dev/null | awk '{print $2}')" || warn "pip3" "not found (venv bundles pip; ok)"

# --- ffmpeg ---------------------------------------------------------------
if have ffmpeg; then pass "ffmpeg" "$(ffmpeg -version 2>/dev/null | head -1 | awk '{print $3}')"
else fail "ffmpeg" "not found — sudo apt-get install ffmpeg"; fi

# --- OpenCV runtime libs --------------------------------------------------
MISS=""
for lib in libGL.so.1 libglib-2.0.so.0; do
  ldconfig -p 2>/dev/null | grep -q "$lib" || MISS="$MISS $lib"
done
[ -z "$MISS" ] && pass "opencv libs" "libGL + glib present" \
  || warn "opencv libs" "missing:$MISS — sudo apt-get install libgl1 libglib2.0-0"

# --- Webcam (only needed for live enrollment) -----------------------------
if ls /dev/video0 >/dev/null 2>&1; then pass "webcam" "/dev/video0 present"
else warn "webcam" "/dev/video0 absent — enroll with --images DIR instead"; fi

# --- systemd user services + lingering + cgroup v2 ------------------------
if have systemctl; then
  if systemctl --user show-environment >/dev/null 2>&1; then pass "systemd --user" "user manager available"
  else warn "systemd --user" "no user manager in this session (use --system install)"; fi
  LING=$(loginctl show-user "$USER" 2>/dev/null | awk -F= '/^Linger=/{print $2}')
  [ "$LING" = "yes" ] && pass "linger" "enabled (service runs at boot)" \
    || warn "linger" "off — install script enables it (loginctl enable-linger)"
  CG=$(stat -fc %T /sys/fs/cgroup 2>/dev/null)
  [ "$CG" = "cgroup2fs" ] && pass "cgroup" "v2 (CPUQuota in user unit works)" \
    || warn "cgroup" "$CG — CPUQuota may need the --system service"
fi

# --- Docker (for HA/Matter + the shared inbox) ----------------------------
if have docker; then
  if docker ps >/dev/null 2>&1; then
    NAMES=$(docker ps --format '{{.Names}}' 2>/dev/null | paste -sd, -)
    pass "docker" "running; containers: ${NAMES:-none}"
    docker ps --format '{{.Names}}' 2>/dev/null | grep -qiE 'home.?assistant|hass' \
      && pass "home assistant" "container detected" \
      || warn "home assistant" "no HA container seen (check name)"
  else warn "docker" "installed but daemon not accessible to this user (sudo/docker group?)"; fi
else warn "docker" "not found (only needed for the HA/Matter side)"; fi

# --- Camera RTSP (needs RTSP_URL) -----------------------------------------
if [ -n "${RTSP_URL:-}" ]; then
  if have ffmpeg; then
    if timeout 20 ffmpeg -rtsp_transport tcp -i "$RTSP_URL" -frames:v 1 -f null - >/dev/null 2>&1; then
      pass "camera RTSP" "connected + decoded a frame"
    else fail "camera RTSP" "could not pull a frame (check IP/creds/RTSP enabled/network)"; fi
  else warn "camera RTSP" "install ffmpeg to test the stream"; fi
else warn "camera RTSP" "set RTSP_URL=... to test the camera"; fi

# --- MQTT broker (needs MQTT_HOST) ----------------------------------------
if [ -n "${MQTT_HOST:-}" ]; then
  PORT=${MQTT_PORT:-1883}
  if have mosquitto_sub; then
    ARGS=(-h "$MQTT_HOST" -p "$PORT" -t '$SYS/#' -C 1 -W 5)
    [ -n "${MQTT_USER:-}" ] && ARGS+=(-u "$MQTT_USER")
    [ -n "${MQTT_PASS:-}" ] && ARGS+=(-P "$MQTT_PASS")
    if mosquitto_sub "${ARGS[@]}" >/dev/null 2>&1; then pass "MQTT" "broker reachable + auth ok ($MQTT_HOST:$PORT)"
    else fail "MQTT" "connect/auth failed ($MQTT_HOST:$PORT)"; fi
  elif timeout 5 bash -c ": >/dev/tcp/$MQTT_HOST/$PORT" 2>/dev/null; then
    warn "MQTT" "port $MQTT_HOST:$PORT open (install mosquitto-clients to verify auth)"
  else fail "MQTT" "cannot reach $MQTT_HOST:$PORT"; fi
else warn "MQTT" "set MQTT_HOST=... (and MQTT_USER/MQTT_PASS) to test the broker"; fi

# --- Shared inbox dir (needs INBOX_DIR) -----------------------------------
if [ -n "${INBOX_DIR:-}" ]; then
  if [ -d "$INBOX_DIR" ] && [ -w "$INBOX_DIR" ]; then pass "inbox dir" "$INBOX_DIR writable"
  elif [ -d "$INBOX_DIR" ]; then fail "inbox dir" "$INBOX_DIR exists but NOT writable (bind-mount UID/perms)"
  else warn "inbox dir" "$INBOX_DIR does not exist yet"; fi
fi

echo "======================================================================"
if [ "${FAILED:-0}" = 1 ]; then echo "Result: FAIL — fix ✗ items before deploying."; exit 1
elif [ "${WARNED:-0}" = 1 ]; then echo "Result: OK with warnings (review ! items)."; exit 0
else echo "Result: ALL GOOD."; exit 0; fi
