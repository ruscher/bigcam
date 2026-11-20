"""Background surveillance service: events, motion detection and recording.

One :class:`CameraMonitor` per configured camera, driven from the GLib main
loop.  It is independent from the preview, so alerts and recordings keep
working while another camera (or none) is shown.  Resource use is bounded:
local motion detection decodes the *sub*-stream at 2 fps and 320×180; recording
copies the main stream without decoding it.

UI code receives notifications through callbacks invoked on the main loop.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst

from core.ipcam import recorder as rec
from core.ipcam.capabilities import make_client
from core.ipcam.events import EventLog, MotionDetector, OnvifEventListener
from core.ipcam.models import IPCameraConfig
from core.ipcam.security import redact
from utils.urls import gst_quote

log = logging.getLogger(__name__)

EventCallback = Callable[[IPCameraConfig, dict], None]
StateCallback = Callable[[IPCameraConfig, str, str], None]


class CameraMonitor:
    def __init__(self, backend, camera, cfg: IPCameraConfig, caps, log_: EventLog,
                 on_event: EventCallback, on_recording: StateCallback,
                 on_status: StateCallback) -> None:
        self.backend, self.camera, self.cfg, self.caps = backend, camera, cfg, caps
        self._log = log_
        self._on_event, self._on_recording, self._on_status = on_event, on_recording, on_status
        self._listener: OnvifEventListener | None = None
        self._motion_pipe: Gst.Pipeline | None = None
        self._motion_bus = 0
        self._detector = MotionDetector(cfg.events.get("sensitivity", 50))
        self._recorder: rec.IPCameraRecorder | None = None
        self._timer = 0
        self._last_alert = 0.0
        self._last_motion = 0.0
        self._motion_retry = 0
        self._last_retention = 0.0
        self._recording_error_at = 0.0
        self.snapshot = cfg.to_dict()

    # -- lifecycle -------------------------------------------------------------

    @property
    def wants_events(self) -> bool:
        ev = self.cfg.events
        return bool(ev.get("native") or ev.get("local_motion") or self.cfg.recording.get("mode") == "motion")

    def start(self) -> None:
        ev = self.cfg.events
        native_ok = self.caps is not None and self.caps.events.available
        if ev.get("native", True) and native_ok and self.cfg.onvif_port:
            client = make_client(self.cfg, timeout=10)
            client.services.events = self.caps.events_service or client.device_url
            self._listener = OnvifEventListener(
                client, lambda e: GLib.idle_add(self._handle_event, e),
                lambda s: GLib.idle_add(self._emit_status, "events", s))
            self._listener.start()
        needs_local = ev.get("local_motion") or (
            self.cfg.recording.get("mode") == "motion" and self._listener is None)
        if needs_local:
            self._start_motion_pipeline()
        self._timer = GLib.timeout_add_seconds(15, self._tick)
        self._tick()

    def stop(self) -> None:
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        if self._listener is not None:
            listener, self._listener = self._listener, None
            # Unsubscribing may block on the network: never on the UI thread.
            threading.Thread(target=listener.stop, name="bigcam-events-stop", daemon=True).start()
        self._stop_motion_pipeline()
        self.stop_recording()

    # -- local motion ------------------------------------------------------------

    def _start_motion_pipeline(self) -> None:
        stream = "sub" if self.cfg.sub_path else "main"
        source = self.backend.get_gst_source(self.camera, audio=False, stream=stream)
        try:
            pipe = Gst.parse_launch(
                f"{source} ! videorate drop-only=true ! videoscale ! "
                "video/x-raw,format=GRAY8,width=320,height=180,framerate=2/1 ! "
                "appsink name=motion emit-signals=true drop=true max-buffers=1 sync=false")
        except GLib.Error as exc:
            log.warning("Motion pipeline failed: %s", redact(exc))
            return
        self.backend.prepare_pipeline(pipe, self.camera)
        pipe.get_by_name("motion").connect("new-sample", self._on_motion_sample)
        bus = pipe.get_bus()
        bus.add_signal_watch()
        self._motion_bus = bus.connect("message", self._on_motion_bus)
        self._motion_pipe = pipe
        pipe.set_state(Gst.State.PLAYING)

    def _stop_motion_pipeline(self) -> None:
        pipe, self._motion_pipe = self._motion_pipe, None
        if pipe is None:
            return
        bus = pipe.get_bus()
        if self._motion_bus:
            bus.disconnect(self._motion_bus)
            self._motion_bus = 0
        bus.remove_signal_watch()
        pipe.set_state(Gst.State.NULL)

    def _on_motion_sample(self, sink) -> Gst.FlowReturn:
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        st = sample.get_caps().get_structure(0)
        w, h = st.get_value("width"), st.get_value("height")
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        try:
            import numpy as np
            stride = len(info.data) // h
            gray = np.frombuffer(info.data, dtype=np.uint8).reshape(h, stride)[:, :w]
            ratio = self._detector.feed(gray)
        finally:
            buf.unmap(info)
        self._motion_retry = 0
        if ratio:
            GLib.idle_add(self._handle_event, {"kind": "motion", "source": "local",
                                               "detail": f"{ratio * 100:.1f}%"})
        return Gst.FlowReturn.OK

    def _on_motion_bus(self, _bus, msg) -> None:
        if msg.type in (Gst.MessageType.ERROR, Gst.MessageType.EOS):
            if msg.type == Gst.MessageType.ERROR:
                err, _dbg = msg.parse_error()
                log.info("Motion stream error (%s): %s", self.cfg.id, redact(err.message))
            self._stop_motion_pipeline()
            self._motion_retry += 1
            delay = min(60, 2 ** min(self._motion_retry, 6))
            self._emit_status("motion", "reconnecting")
            GLib.timeout_add_seconds(delay, self._restart_motion)

    def _restart_motion(self) -> bool:
        if self._timer and self._motion_pipe is None:
            self._start_motion_pipeline()
        return GLib.SOURCE_REMOVE

    # -- events -------------------------------------------------------------------

    def _handle_event(self, event: dict) -> bool:
        now = time.time()
        event = {**event, "time": now, "camera_id": self.cfg.id, "camera_name": self.cfg.name}
        if event["kind"] == "motion":
            self._last_motion = now
            if self.cfg.recording.get("mode") == "motion":
                self.start_recording("motion")
        cooldown = int(self.cfg.events.get("cooldown_s", 60))
        if now - self._last_alert >= cooldown or event["kind"] != "motion":
            self._last_alert = now
            self._log.append(event)
            self._on_event(self.cfg, event)
        return GLib.SOURCE_REMOVE

    def _emit_status(self, what: str, state: str) -> bool:
        self._on_status(self.cfg, what, state)
        return GLib.SOURCE_REMOVE

    # -- recording -------------------------------------------------------------------

    @property
    def recording(self) -> bool:
        return self._recorder is not None and self._recorder.active

    def start_recording(self, reason: str) -> bool:
        if self.recording:
            return True
        if time.time() - self._recording_error_at < 30:
            return False
        self._recorder = rec.build_recorder(self.backend, self.camera, self.cfg, self.caps,
                                            on_state=self._recorder_state)
        if self._recorder is None:
            return False
        return self._recorder.start(reason)

    def stop_recording(self) -> None:
        recorder, self._recorder = self._recorder, None
        if recorder is not None:
            recorder.stop()

    def _recorder_state(self, state: str, detail: str) -> None:
        if state == "error":
            self._recording_error_at = time.time()
            self._recorder = None
            self._log.append({"time": time.time(), "camera_id": self.cfg.id, "camera_name": self.cfg.name,
                              "kind": "recording-error", "source": "system", "detail": detail})
        self._on_recording(self.cfg, state, detail)

    def _tick(self) -> bool:
        mode = self.cfg.recording.get("mode", "off")
        now = time.time()
        if mode == "continuous":
            self.start_recording("continuous")
        elif mode == "schedule":
            inside = rec.in_schedule(rec.parse_schedule(self.cfg.recording.get("schedule", "")))
            if inside:
                self.start_recording("schedule")
            elif self.recording:
                self.stop_recording()
        elif mode == "motion":
            post = int(self.cfg.recording.get("post_roll_s", 15))
            if self.recording and now - self._last_motion > post:
                self.stop_recording()
        elif self.recording and self._recorder.reason != "manual":
            self.stop_recording()
        if self.recording and rec.free_bytes(rec.camera_dir(self.cfg)) < rec.MIN_FREE_BYTES:
            self.stop_recording()
            self._recorder_state("error", "low-disk")
        if now - self._last_retention > 3600:
            self._last_retention = now
            removed = rec.apply_retention(self.cfg)
            if removed:
                log.info("Retention removed %d old segment(s) for %s", removed, self.cfg.id)
        return GLib.SOURCE_CONTINUE


class IPMonitorService:
    """Owns the per-camera monitors; reconfigured whenever settings change."""

    def __init__(self, backend, on_event: EventCallback, on_recording: StateCallback,
                 on_status: StateCallback) -> None:
        self.backend = backend
        self.log = EventLog()
        self._monitors: dict[str, CameraMonitor] = {}
        self._cb = (on_event, on_recording, on_status)

    def monitor(self, camera_id: str) -> CameraMonitor | None:
        return self._monitors.get(camera_id)

    def reconfigure(self, cameras: list) -> None:
        wanted = {}
        for camera in cameras:
            cfg = self.backend.config_for(camera)
            if cfg is None:
                continue
            active = (cfg.events.get("local_motion") or cfg.recording.get("mode", "off") != "off"
                      or (cfg.events.get("native") and (self.backend.capabilities_for(camera) or None)
                          and self.backend.capabilities_for(camera).events.available))
            if active:
                wanted[cfg.id] = (camera, cfg)
        for cam_id in list(self._monitors):
            if cam_id not in wanted:
                self._monitors.pop(cam_id).stop()
        for cam_id, (camera, cfg) in wanted.items():
            current = self._monitors.get(cam_id)
            if current is not None:
                if current.snapshot == cfg.to_dict():
                    continue
                current.stop()
            monitor = CameraMonitor(self.backend, camera, cfg, self.backend.capabilities_for(camera),
                                    self.log, *self._cb)
            self._monitors[cam_id] = monitor
            monitor.start()

    def shutdown(self) -> None:
        for monitor in self._monitors.values():
            monitor.stop()
        self._monitors.clear()


__all__ = ["IPMonitorService", "CameraMonitor", "gst_quote"]
