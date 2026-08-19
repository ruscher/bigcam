Virtual camera management (BigCam)
=================================

Overview
--------
This repository already includes a `VirtualCamera` implementation that uses
`v4l2loopback` and `v4l2loopback-ctl` for dynamic device creation. The new
modules in `core/` provide helpers and a lightweight stabilizer.

Quickstart (Arch / BigLinux)
---------------------------
Install dependencies:

```bash
sudo pacman -Syu --needed v4l-utils gst-plugins-base gst-plugins-good gst-plugins-bad gst-libav gstreamer python-gobject gst-plugins-ugly ffmpeg python-opencv python-numpy
```

Load loopback module (dynamic preferred):

```bash
sudo /usr/share/biglinux/bigcam/script/bigcam-setup.sh
```

Create a virtual camera for an app component (Python example):

```py
from core.vcam_manager import VirtualCameraManager
mgr = VirtualCameraManager()
mgr.load_module_with_defaults()
dev = mgr.allocate_for('my-camera-1')
print('device:', dev)
```

Diagnostics
-----------
Run the bundled diagnostic script:

```bash
/usr/share/biglinux/bigcam/script/bigcam-diagnose.sh
```

Notes on anti-flicker and synchronization
-----------------------------------------
- Use `exclusive_caps=1` to allow multiple browser/WebRTC clients to see
  all virtual devices without negotiation issues.
- Use `max_buffers=8` and queue elements with `max-size-buffers=2` in
  GStreamer to tolerate USB timing jitter.
- Prefer v4l2src direct capture on Linux to minimize PipeWire-induced
  format conversions and timing jitter when low latency is critical.
