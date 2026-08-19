#!/bin/bash
set -u

INTERVAL="${1:-2}"
DEVICES=(/dev/video*)

if [ "${#DEVICES[@]}" -eq 0 ]; then
  echo "No /dev/video* devices were found."
  exit 1
fi

while true; do
  clear
  echo "BigCam virtual camera monitor"
  echo "Interval: ${INTERVAL}s"
  echo "---"
  for dev in "${DEVICES[@]}"; do
    if [ -e "$dev" ]; then
      echo "Device $dev"
      v4l2-ctl -d "$dev" --all 2>/dev/null | grep -E 'Format|Width|Height|Frames|Pixel|Field' | head -n 12 || echo "  v4l2-ctl unavailable or device not readable"
      echo
    fi
  done
  sleep "$INTERVAL"
done
