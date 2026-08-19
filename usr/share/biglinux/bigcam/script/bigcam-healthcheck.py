#!/usr/bin/env python3
"""Quick integrity and capture health check for virtual cameras."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path


def run(cmd: list[str]) -> tuple[int, str, str]:
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def main() -> int:
    devices = sorted(Path("/dev").glob("video*"))
    print("BigCam Health Check")
    print(f"Devices detected: {len(devices)}")

    if not devices:
        print("No /dev/video* devices found.")
        return 1

    for dev in devices:
        print(f"\n== {dev} ==")
        rc, out, err = run(["v4l2-ctl", "--device", str(dev), "--all"])
        if rc == 0:
            lines = [
                line.strip()
                for line in out.splitlines()
                if "Width" in line
                or "Height" in line
                or "Pixel Format" in line
                or "Frame Rate" in line
                or "Frames per second" in line
            ]
            print("\n".join(lines[:12]) if lines else "No format metadata found.")
        else:
            print(f"v4l2-ctl failed: {err or out}")

        sample = Path("/tmp") / ("bigcam_health_" + dev.name + ".jpg")
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "v4l2",
            "-video_size",
            "1920x1080",
            "-i",
            str(dev),
            "-frames:v",
            "3",
            "-y",
            str(sample),
        ]
        rc2, out2, err2 = run(cmd)
        if rc2 == 0 and sample.exists():
            print("Capture test: PASS")
            print(f"Frame sample: {sample}")
        else:
            print("Capture test: FAIL")
            print(err2 or out2 or "No diagnostics output")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
