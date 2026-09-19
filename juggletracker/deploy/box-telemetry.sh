#!/usr/bin/env bash
# box-telemetry.sh — gather read-only hardware/runtime telemetry to dial in the
# Soccer Juggle Tracker config for THIS box. Self-contained (stock tools; extras
# used only if present). Copy this one file to the box and run it; paste the
# output back for tuning. Writes a copy to telemetry-<host>-<date>.txt.
#
# Optional env vars enable the deeper probes:
#   RTSP_MAIN=... RTSP_SUB=...   # probe the camera streams (needs ffprobe)
#   DDTEST=1                     # run a small disk write test in $PWD
#
# It changes nothing except an optional temp file it deletes.

OUT="telemetry-$(hostname -s 2>/dev/null || echo box)-$(date +%Y%m%d_%H%M%S).txt"
exec > >(tee "$OUT") 2>&1
sec(){ printf '\n===== %s =====\n' "$1"; }
have(){ command -v "$1" >/dev/null 2>&1; }

echo "Soccer Juggle Tracker — hardware telemetry"
echo "date: $(date)    host: $(hostname 2>/dev/null)"

sec "OS / kernel"
(. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME"); uname -a
echo "virt: $(have systemd-detect-virt && systemd-detect-virt || echo n/a)"

sec "CPU model + flags (AVX/AVX2/FMA matter for ML speed)"
have lscpu && lscpu | grep -E 'Model name|^Architecture|^CPU\(s\)|Thread|Core|Socket|MHz|BogoMIPS'
echo "avx flags: $(grep -o 'avx[0-9_]*\|fma\|sse4_2' /proc/cpuinfo 2>/dev/null | sort -u | paste -sd' ' -)"

sec "CPU frequency scaling + governor  (<-- powersave governor is a common, fixable bottleneck)"
for c in /sys/devices/system/cpu/cpu[0-9]*/cpufreq; do
  [ -d "$c" ] || continue
  g=$(cat "$c/scaling_governor" 2>/dev/null)
  cur=$(cat "$c/scaling_cur_freq" 2>/dev/null)
  mn=$(cat "$c/scaling_min_freq" 2>/dev/null)
  mx=$(cat "$c/scaling_max_freq" 2>/dev/null)
  printf '  %s governor=%s cur=%sMHz min=%sMHz max=%sMHz\n' \
    "$(basename "$(dirname "$c")")" "$g" "$((cur/1000))" "$((mn/1000))" "$((mx/1000))"
done 2>/dev/null | sort -u
[ -r /sys/devices/system/cpu/intel_pstate/no_turbo ] && \
  echo "  intel_pstate no_turbo=$(cat /sys/devices/system/cpu/intel_pstate/no_turbo) (0 = turbo enabled)"
have cpupower && cpupower frequency-info 2>/dev/null | grep -E 'driver|governor|boost|current policy' | head

sec "Thermals / throttling history"
if have sensors; then sensors 2>/dev/null | grep -iE 'core|package|temp|fan' | head -20
else
  for z in /sys/class/thermal/thermal_zone*; do
    printf '  %s: %s = %s C\n' "$(basename "$z")" "$(cat "$z/type" 2>/dev/null)" \
      "$(( $(cat "$z/temp" 2>/dev/null || echo 0) / 1000 ))"
  done 2>/dev/null
fi
echo "  recent thermal kernel msgs:"; (dmesg 2>/dev/null | grep -iE 'thermal|throttl|mce' | tail -5) || echo "  (need sudo for dmesg)"

sec "Memory"
have free && free -h
echo "swappiness: $(cat /proc/sys/vm/swappiness 2>/dev/null)"

sec "iGPU / hardware accel (VA-API can offload video decode from the CPU)"
have lspci && lspci 2>/dev/null | grep -iE 'vga|display|3d'
echo "render nodes: $(ls /dev/dri/ 2>/dev/null | paste -sd' ' - || echo none)"
if have vainfo; then echo "-- vainfo (H.264 decode entrypoints => ffmpeg -hwaccel vaapi possible):"; vainfo 2>/dev/null | grep -iE 'VAProfileH264|Driver version' | head
else echo "vainfo not installed (sudo apt-get install vainfo) — tells us if HW video decode is available"; fi

sec "Disk space + (optional) write speed"
have df && df -h "$PWD" "$HOME" 2>/dev/null | sort -u
if [ "${DDTEST:-0}" = 1 ]; then
  echo "-- write test (256MB, then deleted):"
  dd if=/dev/zero of="$PWD/.telemetry_ddtest" bs=1M count=256 conv=fdatasync 2>&1 | tail -1
  rm -f "$PWD/.telemetry_ddtest"
else
  echo "(set DDTEST=1 to measure clip write throughput)"
fi

sec "Current load + what HA/Matter already consume"
have uptime && uptime
if have docker && docker ps >/dev/null 2>&1; then
  echo "-- docker stats (baseline CPU/mem of running containers):"
  docker stats --no-stream --format '  {{.Name}}: cpu={{.CPUPerc}} mem={{.MemUsage}}' 2>/dev/null
else echo "(docker stats needs docker access; shows HA/Matter baseline load)"; fi
echo "-- top processes by CPU:"; have top && top -bn1 2>/dev/null | head -12 | tail -6

sec "Camera stream probe (set RTSP_MAIN / RTSP_SUB to enable)"
probe(){
  local url="$1" label="$2"
  [ -z "$url" ] && { echo "  $label: (not provided)"; return; }
  if have ffprobe; then
    echo "  $label:"
    timeout 20 ffprobe -v error -rtsp_transport tcp -select_streams v:0 \
      -show_entries stream=width,height,r_frame_rate,codec_name,bit_rate \
      -of default=noprint_wrappers=1 "$url" 2>&1 | sed 's/^/    /'
  else echo "  $label: install ffmpeg (ffprobe) to probe"; fi
}
probe "${RTSP_MAIN:-}" "main stream"
probe "${RTSP_SUB:-}"  "sub stream"

echo ""
echo "Done. Telemetry saved to: $OUT"
echo "Paste it back and I'll tune infer_long_edge / person_stride / ROI / stream choice / governor for this box."
