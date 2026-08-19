#!/bin/sh
# BigCam setup script: load v4l2loopback with recommended parameters
set -e

MODPROBE=$(command -v modprobe || echo /usr/bin/modprobe)
V4L2LOOPBACK_CTL=$(command -v v4l2loopback-ctl || echo /usr/sbin/v4l2loopback-ctl)

echo "BigCam setup: loading v4l2loopback module"

if [ -x "$V4L2LOOPBACK_CTL" ]; then
  echo "v4l2loopback-ctl available — loading module with devices=0 (dynamic)"
  sudo -n $MODPROBE v4l2loopback devices=0 || sudo $MODPROBE v4l2loopback devices=0
else
  # Fallback: create fixed devices 20..24 with sane defaults
  echo "v4l2loopback-ctl not found — loading fixed devices 20..24"
  sudo -n $MODPROBE v4l2loopback devices=5 exclusive_caps=1,1,1,1,1 max_buffers=8 video_nr=20,21,22,23,24 card_label="BigCam Virtual 1,BigCam Virtual 2,BigCam Virtual 3,BigCam Virtual 4,BigCam Virtual 5" || \
    sudo $MODPROBE v4l2loopback devices=5 exclusive_caps=1,1,1,1,1 max_buffers=8 video_nr=20,21,22,23,24 card_label="BigCam Virtual 1,BigCam Virtual 2,BigCam Virtual 3,BigCam Virtual 4,BigCam Virtual 5"
fi

echo "Loaded. Use v4l2loopback-ctl add/delete for dynamic devices when available."
