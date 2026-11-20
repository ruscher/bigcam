#!/usr/bin/env python3
"""Real-device graphical check (run on a machine in the camera's LAN).

Discovers the camera through the assistant (scan limited to REAL_HOST), runs
the connection test, saves the camera and opens it in the real BigCam window.

    BIGCAM_TEST_RESULTS=/abs/dir REAL_HOST=192.168.15.15 REAL_USER=admin [REAL_PASS=...] \\
    xvfb-run -a -s '-screen 0 1440x900x24' dbus-run-session -- python3 tests/ipcam/run_graphical_real.py

Without REAL_PASS the expected outcome is a clear "password required/wrong
password" state; with it, live video (and PTZ when the device confirms it).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "usr/share/biglinux/bigcam"))
RESULTS = Path(os.environ["BIGCAM_TEST_RESULTS"])
RESULTS.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("BIGCAM_DISABLE_KEYRING", "1")

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

Gst.init(None)
from constants import APP_ID, APP_NAME  # noqa: E402

GLib.set_prgname(APP_ID)
GLib.set_application_name(APP_NAME)
from utils.settings_manager import SettingsManager  # noqa: E402

HOST = os.environ["REAL_HOST"]
USER = os.environ.get("REAL_USER", "admin")
PASS = os.environ.get("REAL_PASS", "")
SettingsManager().update({"show-welcome": False, "virtual-camera-enabled": False, "hotplug_enabled": False,
                          "resource-monitor-enabled": False, "auto-hide-controls": False, "ip_cameras": [],
                          "auto-start-preview": False})  # never open local USB cameras
context = GLib.MainContext.default()
summary: dict = {"physical_camera": True, "host": HOST, "password_given": bool(PASS), "checks": {}}


def pump_until(predicate, seconds=15, label="condition"):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for _ in range(200):
            if not context.pending():
                break
            context.iteration(False)
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Timed out waiting for " + label)


def pump(seconds):
    end = time.monotonic() + seconds
    pump_until(lambda: time.monotonic() >= end, seconds + 1, "interval")


def shot(name):
    pump(0.3)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "x11grab", "-video_size", "1440x900",
                    "-i", os.environ["DISPLAY"], "-frames:v", "1", str(RESULTS / name)], check=False, timeout=15)


def check(name, value):
    summary["checks"][name] = value
    print("CHECK", name, value, flush=True)


win = None
try:
    from main import BigDigicamApp
    app = BigDigicamApp()
    assert app.register(None)
    app.activate()
    pump_until(lambda: bool(app.get_windows()), label="window")
    win = app.get_windows()[0]
    win.set_default_size(1400, 860)
    pump_until(lambda: win.get_mapped(), label="mapped")

    from ui.ip_camera_dialog import IPCameraDialog
    dialog = IPCameraDialog()
    saved = []
    dialog.connect("camera-saved", lambda _d, cfg: (saved.append(cfg), win._ipcam._saved(cfg, new=True)))
    dialog.present(win)
    dialog._open_search()
    dialog._target_row.set_text(HOST)  # never scan beyond the authorised device
    if dialog._scan_cancel is not None:
        dialog._scan_cancel.set()
    pump_until(lambda: dialog._scan_cancel is None, 20, "first scan stop")
    dialog._start_scan()
    pump_until(lambda: dialog._scan_cancel is None and dialog._results_rows, 40, "scan results")
    pump(0.5)
    shot("01-discovery.png")
    check("discovered", len(dialog._results_rows))
    dialog._results_rows[0].activate()
    pump(0.5)
    dialog._name_row.set_text("Garagem")
    dialog._user_row.set_text(USER)
    dialog._pass_row.set_text(PASS)
    dialog._run_test()
    pump_until(lambda: dialog._test_btn.get_sensitive(), 60, "connection test")
    pump(0.8)
    shot("02-connection-test.png")
    caps = dialog._caps
    summary["capabilities"] = caps.summary() if caps else {}
    summary["vendor"] = caps.vendor if caps else ""
    summary["device_info"] = caps.device_info if caps else {}
    check("test_ok", dialog._tested_ok)
    dialog._persist(dialog._collect())
    pump_until(lambda: bool(saved), 10, "saved")
    states = []
    win._stream_engine.connect("connection-status", lambda _e, s, _m: states.append(s))
    win._split_view.set_show_sidebar(True)
    win._switch_sidebar_tab(0)
    pump_until(lambda: win._active_camera is not None, 10, "selected")
    expected = ("connected",) if PASS else ("auth-failed", "connected")
    pump_until(lambda: any(s in states for s in expected + ("auth-failed",)), 60, "connection outcome")
    pump(3)
    shot("03-bigcam-window.png")
    summary["connection_states"] = states
    check("live_video", win._stream_engine.is_playing())
    check("auth_state_reported", "auth-failed" in states or win._stream_engine.is_playing())
    panel = win._controls_page._ip_panel
    check("ptz_pad_available", bool(panel and panel._pad))
except Exception:
    summary["error"] = traceback.format_exc()
    print(summary["error"], file=sys.stderr)
finally:
    (RESULTS / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    if win is not None:
        win._cleanup_and_close()
        try:
            pump(3)
        except AssertionError:
            pass
