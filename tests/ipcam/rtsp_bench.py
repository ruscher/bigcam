#!/usr/bin/env python3
"""RTSP test bench that mimics the stream layout observed on a Yoosee camera.

Serves ``/onvif1`` (1280x720 H.264, main) and ``/onvif2`` (640x360 H.264, sub),
both with G.711 A-law mono 8 kHz audio, protected by RTSP Digest auth.

This is test tooling only (requires gst-rtsp-server); BigCam never needs it at
runtime.  Credentials come from the environment so they never appear in the
process list:

    BENCH_USER=admin BENCH_PASS=$(openssl rand -hex 8) python3 rtsp_bench.py --port 8554
"""

from __future__ import annotations

import argparse
import os
import signal
import sys

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstRtsp", "1.0")
gi.require_version("GstRtspServer", "1.0")
from gi.repository import GLib, Gst, GstRtsp, GstRtspServer  # noqa: E402

LAUNCH = (
    "( videotestsrc is-live=true pattern={pattern} ! "
    "video/x-raw,width={w},height={h},framerate=30/1 ! "
    "clockoverlay halignment=right valignment=top time-format=\"%H:%M:%S\" ! "
    "x264enc tune=zerolatency speed-preset=ultrafast bitrate={br} key-int-max=30 ! "
    "h264parse ! rtph264pay name=pay0 pt=96 config-interval=1 "
    "audiotestsrc is-live=true wave=sine freq=440 volume=0.05 ! "
    "audio/x-raw,rate=8000,channels=1 ! alawenc ! rtppcmapay name=pay1 pt=8 )"
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8554)
    args = parser.parse_args()
    user = os.environ.get("BENCH_USER", "admin")
    password = os.environ.get("BENCH_PASS")
    if not password:
        print("Set BENCH_PASS in the environment", file=sys.stderr)
        return 2
    Gst.init(None)
    server = GstRtspServer.RTSPServer()
    server.set_service(str(args.port))
    auth = GstRtspServer.RTSPAuth()
    auth.set_supported_methods(GstRtsp.RTSPAuthMethod.DIGEST)
    auth.set_realm("HIipCamera")
    token = GstRtspServer.RTSPToken()
    token.set_string(GstRtspServer.RTSP_TOKEN_MEDIA_FACTORY_ROLE, "user")
    auth.add_digest(user, password, token)
    server.set_auth(auth)
    mounts = server.get_mount_points()
    for path, (w, h, br, pattern) in {"/onvif1": (1280, 720, 1200, "smpte"),
                                      "/onvif2": (640, 360, 400, "ball")}.items():
        factory = GstRtspServer.RTSPMediaFactory()
        factory.set_launch(LAUNCH.format(w=w, h=h, br=br, pattern=pattern))
        factory.set_shared(True)
        perms = GstRtspServer.RTSPPermissions()
        perms.add_permission_for_role("user", GstRtspServer.RTSP_PERM_MEDIA_FACTORY_ACCESS, True)
        perms.add_permission_for_role("user", GstRtspServer.RTSP_PERM_MEDIA_FACTORY_CONSTRUCT, True)
        factory.set_permissions(perms)
        mounts.add_factory(path, factory)
    server.attach(None)
    loop = GLib.MainLoop()
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, loop.quit)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, loop.quit)
    print(f"RTSP bench ready on port {args.port}: /onvif1 /onvif2", flush=True)
    loop.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
