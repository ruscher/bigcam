#!/usr/bin/env python3
"""Graphical integration test for IP cameras inside a private session.

Runs the real BigCam window against a real RTSP stream (the bench in
tests/ipcam/rtsp_bench.py, reachable at BENCH_HOST:BENCH_PORT) and, for PTZ, a
local fake ONVIF service combined with an RTSP endpoint at TUNNEL_PORT on
127.0.0.1 (an SSH local forward of the bench, as used for remote sites).

    BIGCAM_TEST_RESULTS=/abs/dir BENCH_HOST=<bench-host> BENCH_PORT=8554 TUNNEL_PORT=18554 \\
    BENCH_PASS=... BENCH_CTL="ssh-command-to-stop/start-bench" \\
    xvfb-run -a -s '-screen 0 1440x900x24' dbus-run-session -- python3 tests/ipcam/run_graphical_ip.py

No physical camera is involved; results are written as JSON + screenshots.
"""

from __future__ import annotations

import json
import os
import resource
import subprocess
import sys
import threading
import time
import traceback
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "usr/share/biglinux/bigcam"))
sys.path.insert(0, str(ROOT / "tests/ipcam"))
RESULTS = Path(os.environ["BIGCAM_TEST_RESULTS"])
RESULTS.mkdir(parents=True, exist_ok=True)
os.environ["BIGCAM_DISABLE_KEYRING"] = "1"

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
from gi.repository import Adw, GLib, Gst, Gtk  # noqa: E402

Gst.init(None)
from constants import APP_ID, APP_NAME  # noqa: E402

GLib.set_prgname(APP_ID)
GLib.set_application_name(APP_NAME)
from utils.settings_manager import SettingsManager  # noqa: E402

HOST, PORT = os.environ["BENCH_HOST"], int(os.environ["BENCH_PORT"])
TUNNEL = int(os.environ.get("TUNNEL_PORT", "0"))
PASSWORD = os.environ["BENCH_PASS"]
CTL = os.environ.get("BENCH_CTL", "")

# Legacy entry on purpose: exercises the migration that strips the password.
SettingsManager().update({
    "show-welcome": False, "virtual-camera-enabled": False, "hotplug_enabled": False,
    "resource-monitor-enabled": False, "auto-hide-controls": False, "theme": "dark",
    "ip_cameras": [{"name": "Garagem (bench)", "url": f"rtsp://admin:{PASSWORD}@{HOST}:{PORT}/onvif1"}],
})

context = GLib.MainContext.default()
summary: dict = {"physical_camera": False, "checks": {}, "metrics": {}}


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


def shot(_widget, name):
    """Grab the private X screen exactly as displayed."""
    pump(0.3)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "x11grab", "-video_size", "1440x900",
                    "-i", os.environ["DISPLAY"], "-frames:v", "1", str(RESULTS / name)],
                   check=False, timeout=15)


def cpu() -> float:
    u = resource.getrusage(resource.RUSAGE_SELF)
    return u.ru_utime + u.ru_stime


def check(name, value=True):
    summary["checks"][name] = value
    print("CHECK", name, value, flush=True)


if os.environ.get("BIGCAM_LEGACY_BGR") == "1":
    # Measure the pre-optimisation conversion for the before/after report.
    import numpy as _np
    import core.stream_engine as _se

    def _legacy(data, width, height, stride, offset=0):
        return _np.ndarray((height, width, 4), dtype=_np.uint8, buffer=data, offset=offset,
                           strides=(stride, 4, 1))[:, :, :3].copy()
    _se.bgr_from_bgra = _legacy
    summary["legacy_conversion"] = True

app = win = None
fake = None
try:
    from main import BigDigicamApp
    from constants import BackendType

    app = BigDigicamApp()
    assert app.register(None)
    app.activate()
    pump_until(lambda: bool(app.get_windows()), label="window")
    win = app.get_windows()[0]
    win.set_default_size(1400, 860)
    pump_until(lambda: win.get_mapped(), label="mapped")
    pump_until(lambda: any(c.backend == BackendType.IP for c in win._camera_manager.cameras), 15, "IP camera")
    settings_text = (Path(os.environ["XDG_CONFIG_HOME"]) / "bigcam").rglob("*.json")
    leaked = [str(p) for p in settings_text if PASSWORD in p.read_text(errors="ignore")]
    check("legacy_password_removed_from_settings", not leaked)

    cam = next(c for c in win._camera_manager.cameras if c.backend == BackendType.IP)
    t0 = time.monotonic()
    win._ipcam.select(cam.id)
    pump_until(lambda: win._stream_engine.is_playing(), 30, "IP preview playing")
    summary["metrics"]["gui_time_to_first_frame_ms"] = int((time.monotonic() - t0) * 1000)
    frame = win._stream_engine.last_frame_bgr
    check("live_frame_1280x720", frame is not None and frame.shape[:2] == (720, 1280))
    win._split_view.set_show_sidebar(True)
    win._switch_sidebar_tab(0)
    pump_until(lambda: win._controls_page._ip_panel is not None, 20, "IP panel")
    pump(2.5)
    shot(win, "01-ip-live-dark.png")
    check("ip_panel_shown", True)

    # Steady-state cost of the preview (decode + display in this process).
    textures = [0]
    tid = win._stream_engine.connect("new-texture", lambda *_: textures.__setitem__(0, textures[0] + 1))
    c0, w0 = cpu(), time.monotonic()
    pump(10)
    elapsed = time.monotonic() - w0
    win._stream_engine.disconnect(tid)
    summary["metrics"]["preview_cpu_pct_1280x720"] = round(100 * (cpu() - c0) / elapsed, 1)
    summary["metrics"]["preview_displayed_fps"] = round(textures[0] / elapsed, 1)
    summary["metrics"]["maxrss_mb"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024

    # Photo
    from utils import xdg
    before = set(Path(xdg.photos_dir()).glob("*"))
    win._on_capture_action()
    pump_until(lambda: not win._capture_pending and set(Path(xdg.photos_dir()).glob("*")) - before, 20, "photo")
    photo = sorted(set(Path(xdg.photos_dir()).glob("*")) - before)[-1]
    from PIL import Image
    with Image.open(photo) as im:
        check("photo_saved", im.size == (1280, 720))

    # Local recording through the existing recorder
    win._on_record_toggle()
    pump_until(lambda: win._video_recorder.state == "recording", 20, "recording")
    pump(4)
    win._on_record_toggle()
    pump_until(lambda: win._video_recorder._done.is_set(), 25, "finalize")
    out = win._video_recorder.output_path
    probe = json.loads(subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", out], text=True))
    subprocess.run(["ffmpeg", "-v", "error", "-i", out, "-f", "null", "-"], check=True, timeout=60)
    check("local_recording_decodes", float(probe["format"]["duration"]) > 2)

    # Stream quality switch (main → sub)
    cfg = win._ipcam.backend.config_for(cam)
    cfg.sub_path = "/onvif2"
    cfg.stream = "sub"
    win._ipcam.config_changed(cfg, True)
    pump_until(lambda: win._stream_engine.is_playing() and win._stream_engine.last_frame_bgr is not None
               and win._stream_engine.last_frame_bgr.shape[:2] == (360, 640), 30, "substream")
    check("substream_switch_640x360", True)

    # Reconnection: stop the bench, expect "reconnecting", start it, expect playback again.
    if CTL:
        states = []
        hid = win._stream_engine.connect("connection-status", lambda _e, s, _m: states.append(s))
        subprocess.run(CTL.split() + ["stop"], check=True, timeout=30)
        pump_until(lambda: "reconnecting" in states, 40, "reconnecting state")
        shot(win, "02-reconnecting.png")
        check("connection_loss_detected", True)
        subprocess.run(CTL.split() + ["start"], check=True, timeout=30)
        t1 = time.monotonic()
        pump_until(lambda: states[-1:] == ["connected"], 90, "reconnected")
        summary["metrics"]["recovery_after_restore_s"] = round(time.monotonic() - t1, 1)
        summary["metrics"]["reconnect_attempts_seen"] = states.count("reconnecting")
        check("auto_reconnected", win._stream_engine.is_playing())
        win._stream_engine.disconnect(hid)

    # PTZ: fake ONVIF + tunnelled RTSP on localhost
    if TUNNEL:
        import test_ipcam as fakes
        fakes.PASSWORD = PASSWORD  # one password for ONVIF (fake) and RTSP (bench)
        fakes.FakeOnvif.calls = []
        fake = ThreadingHTTPServer(("127.0.0.1", 0), fakes.FakeOnvif)
        threading.Thread(target=fake.serve_forever, daemon=True).start()
        from core.ipcam.models import IPCameraConfig
        from core.ipcam.security import CredentialStore
        ptz_cfg = IPCameraConfig.new(name="PTZ (tunnel)", host="127.0.0.1", rtsp_port=TUNNEL,
                                     onvif_port=fake.server_address[1], main_path="/onvif1",
                                     sub_path="/onvif2", username="admin", stream="sub")
        CredentialStore.instance().store(ptz_cfg.id, PASSWORD)
        win._ipcam._saved(ptz_cfg, new=True)
        pump_until(lambda: win._active_camera is not None and win._active_camera.id == ptz_cfg.id, 15, "select")
        pump_until(lambda: win._controls_page._ip_panel is not None and win._controls_page._ip_panel._pad, 25,
                   "PTZ pad")
        pad = win._controls_page._ip_panel._pad
        pad.hold(1, 0)
        pump(1.6)
        pad.release()
        pump(0.6)
        acts = [a for a, ok in fakes.FakeOnvif.calls if a in ("ContinuousMove", "Stop")]
        check("ptz_move_then_stop", "ContinuousMove" in acts and acts[-1] == "Stop")
        pad.hold(0, 1)
        pump(0.3)
        win._controls_page._ip_panel.dispose_panel()  # e.g. camera switched away
        pump(0.6)
        acts = [a for a, ok in fakes.FakeOnvif.calls if a in ("ContinuousMove", "Stop")]
        check("ptz_stop_on_panel_dispose", acts[-1] == "Stop")
        win._ipcam.select(ptz_cfg.id, restart=True)
        pump_until(lambda: win._controls_page._ip_panel is not None and win._controls_page._ip_panel._pad, 25, "pad")
        pump(2)
        shot(win, "03-ptz-panel.png")

    # Camera wall
    win._ipcam.show_wall()
    wall = win._ipcam._wall
    pump_until(lambda: wall.get_mapped(), 10, "wall")
    pump_until(lambda: all(t._state.get_label() == "Live" or "Ao vivo" in t._state.get_label()
                           for t in wall._tiles), 30, "wall tiles live")
    pump(2)
    shot(wall, "04-wall.png")
    check("wall_tiles_live", len(wall._tiles))
    wall.close()
    pump(0.5)

    # Assistant: manual path with a wrong password, then the right one.
    from ui.ip_camera_dialog import IPCameraDialog
    dialog = IPCameraDialog()
    dialog.present(win)
    dialog._nav.push(dialog._form_page)
    dialog._name_row.set_text("Garagem")
    dialog._host_row.set_text(HOST)
    dialog._proto_row.set_selected(1)
    dialog._rtsp_port.set_value(PORT)
    dialog._user_row.set_text("admin")
    dialog._pass_row.set_text("wrong-password")
    dialog._main_path.set_text("/onvif1")
    dialog._sub_path.set_text("/onvif2")
    dialog._run_test()
    pump_until(lambda: dialog._test_btn.get_sensitive(), 30, "test (wrong)")
    pump(0.5)
    shot(win, "05-dialog-wrong-password.png")
    check("dialog_reports_wrong_password", dialog._result_icon.get_icon_name() == "dialog-password-symbolic")
    dialog._pass_row.set_text(PASSWORD)
    dialog._run_test()
    pump_until(lambda: dialog._test_btn.get_sensitive(), 40, "test (right)")
    pump(0.5)
    shot(win, "06-dialog-ok.png")
    check("dialog_preview_ok", dialog._tested_ok and dialog._preview.get_visible())
    dialog.force_close()

    # Light theme
    Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
    win.set_default_size(900, 700)
    pump(2)
    shot(win, "07-light-narrow.png")
    check("light_theme_rendered", True)
except Exception:
    summary["error"] = traceback.format_exc()
    print(summary["error"], file=sys.stderr)
finally:
    (RESULTS / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    if fake is not None:
        fake.shutdown()
    if win is not None:
        win._cleanup_and_close()
        try:
            pump(3)
        except AssertionError:
            pass
    sys.exit(1 if "error" in summary else 0)
