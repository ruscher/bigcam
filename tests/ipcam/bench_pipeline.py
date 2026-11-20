#!/usr/bin/env python3
"""Headless measurement of the IP camera preview pipeline used by StreamEngine.

Measures time-to-first-frame, delivered FPS and process CPU while consuming a
real RTSP stream through the same appsink tail used by the preview.

    BIGCAM_TEST_PASS=... python3 tests/ipcam/bench_pipeline.py HOST PORT /onvif1 --seconds 15 [--legacy]

The password is read from the environment and never printed.
"""

from __future__ import annotations

import argparse
import os
import resource
import sys
import time

ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "usr", "share", "biglinux", "bigcam")
sys.path.insert(0, os.path.abspath(ROOT))

import gi  # noqa: E402

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

from core.backends.ip_backend import IPBackend  # noqa: E402
from core.ipcam.models import IPCameraConfig  # noqa: E402
from core.ipcam.security import CredentialStore, redact  # noqa: E402

TAIL = ("videoflip name=flip method=0 ! videocrop name=crop ! videoconvert ! video/x-raw,format=BGRA ! "
        "queue max-size-buffers=2 leaky=downstream ! "
        "appsink name=sink emit-signals=true drop=true max-buffers=2 sync=false")


def cpu_seconds() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime + usage.ru_stime


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("host")
    ap.add_argument("port", type=int)
    ap.add_argument("path")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--seconds", type=float, default=15)
    ap.add_argument("--legacy", action="store_true", help="old pipeline: credentials in URL")
    ap.add_argument("--audio", action="store_true")
    args = ap.parse_args()
    password = os.environ.get("BIGCAM_TEST_PASS", "")
    Gst.init(None)
    cfg = IPCameraConfig.new(name="bench", host=args.host, rtsp_port=args.port,
                             main_path=args.path, username=args.user, protocol="rtsp",
                             audio=args.audio, muted=True)
    CredentialStore.instance()._memory[cfg.id] = password  # session-only for the test
    backend = IPBackend()
    camera = backend.cameras_from_configs([cfg])[0]
    if args.legacy:
        from urllib.parse import quote
        url = f"rtsp://{args.user}:{quote(password, safe='')}@{args.host}:{args.port}{args.path}"
        source = f'rtspsrc location="{url}" latency=150 ! decodebin ! videoconvert'
    else:
        source = backend.get_gst_source(camera)
    pipeline = Gst.parse_launch(f"{source} ! {TAIL}")
    if not args.legacy:
        backend.prepare_pipeline(pipeline, camera)
    frames = 0
    first = [0.0]
    size = [0, 0]
    start = time.monotonic()

    def on_sample(sink):
        nonlocal frames
        sample = sink.emit("pull-sample")
        if sample is not None:
            frames += 1
            if not first[0]:
                first[0] = time.monotonic() - start
                st = sample.get_caps().get_structure(0)
                size[0], size[1] = st.get_value("width"), st.get_value("height")
        return Gst.FlowReturn.OK

    pipeline.get_by_name("sink").connect("new-sample", on_sample)
    loop = GLib.MainLoop()
    errors = []

    def on_msg(_bus, msg):
        if msg.type == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            errors.append(redact(f"{err.message} | {dbg}"))
            loop.quit()

    bus = pipeline.get_bus()
    bus.add_signal_watch()
    bus.connect("message", on_msg)
    cpu0 = cpu_seconds()
    pipeline.set_state(Gst.State.PLAYING)
    GLib.timeout_add(int(args.seconds * 1000), loop.quit)
    loop.run()
    elapsed = time.monotonic() - start
    cpu = cpu_seconds() - cpu0
    pipeline.set_state(Gst.State.NULL)
    steady = elapsed - first[0] if first[0] else 0
    print(f"mode={'legacy' if args.legacy else 'new'} audio={args.audio} frames={frames} "
          f"size={size[0]}x{size[1]} ttff_ms={first[0]*1000:.0f} "
          f"fps={frames/steady if steady else 0:.1f} cpu_pct={100*cpu/elapsed:.1f} "
          f"maxrss_mb={resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024:.0f}")
    for err in errors:
        print("ERROR:", err)
    return 1 if errors or not frames else 0


if __name__ == "__main__":
    sys.exit(main())
