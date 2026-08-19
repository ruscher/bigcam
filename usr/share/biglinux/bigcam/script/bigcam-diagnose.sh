#!/bin/sh
# Simple diagnostic script to check loopback devices, frame delivery and FPS.
set -e

echo "BigCam diagnostic"

echo "1) v4l2loopback kernel module and devices"
lsmod | grep v4l2loopback || true
echo "Devices:"
ls /dev/video* 2>/dev/null || true

echo "\n2) List loopback devices and labels"
v4l2-ctl --list-devices || true

echo "\n3) Basic capture test: attempt to capture 5 frames from each BigCam device"
for dev in /dev/video{20..24}; do
  if [ -e "$dev" ]; then
    echo "Capturing from $dev"
    ffmpeg -hide_banner -loglevel error -f v4l2 -video_size 1920x1080 -i "$dev" -frames:v 5 -y /tmp/bigcam_test_${dev##*/}.jpg 2>/dev/null || echo "capture failed for $dev"
  fi
done

echo "\n4) Check for dropped frames via v4l2-ctl (if available)"
for dev in /dev/video{20..24}; do
  if [ -e "$dev" ]; then
    echo "Stats for $dev:" 
    v4l2-ctl -d "$dev" --get-fmt-video || true
  fi
done

echo "Diagnostics complete. Inspect /tmp/bigcam_test_*.jpg for captured frames."
