"""Surveillance recording for IP cameras without re-encoding.

The RTSP payload is depacketised and parsed, then muxed into segmented
Matroska files (``splitmuxsink``).  H.264/H.265 video and G.711/AAC audio are
copied as-is, so a recording costs almost no CPU.  Files are organised as
``<root>/<camera>/<YYYY-MM-DD>/<HH-MM-SS>.mkv``.

Modes (``IPCameraConfig.recording['mode']``): ``off``, ``continuous``,
``motion`` (started by events, stopped after ``post_roll_s``) and
``schedule`` (continuous inside ``HH:MM-HH:MM`` windows).
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Callable

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst

from core.ipcam.models import IPCameraConfig
from core.ipcam.security import redact
from utils.urls import gst_quote

log = logging.getLogger(__name__)

MIN_FREE_BYTES = 1024 ** 3          # stop recording below 1 GiB free
_SAFE_NAME = re.compile(r"[^\w.\- ]+", re.UNICODE)


def default_root() -> Path:
    videos = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_VIDEOS) or os.path.expanduser("~/Videos")
    return Path(videos) / "BigCam" / "IP"


def camera_dir(cfg: IPCameraConfig) -> Path:
    root = Path(cfg.recording.get("directory") or default_root())
    name = _SAFE_NAME.sub("_", cfg.name).strip(" ._") or cfg.id.replace(":", "_")
    return root / name


def parse_schedule(text: str) -> list[tuple[int, int]]:
    """'08:00-18:00, 22:00-06:00' → [(480, 1080), (1320, 360)] minutes."""
    windows = []
    for part in re.split(r"[,;]", text or ""):
        match = re.match(r"\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$", part)
        if not match:
            continue
        h1, m1, h2, m2 = (int(g) for g in match.groups())
        if h1 < 24 and h2 < 24 and m1 < 60 and m2 < 60:
            windows.append((h1 * 60 + m1, h2 * 60 + m2))
    return windows


def in_schedule(windows: list[tuple[int, int]], now: _dt.datetime | None = None) -> bool:
    now = now or _dt.datetime.now()
    minute = now.hour * 60 + now.minute
    for start, end in windows:
        if start <= end and start <= minute < end:
            return True
        if start > end and (minute >= start or minute < end):
            return True
    return False


def free_bytes(path: Path) -> int:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return 0


def apply_retention(cfg: IPCameraConfig, now: float | None = None) -> int:
    """Delete this camera's segments older than ``retention_days``. Returns count."""
    days = int(cfg.recording.get("retention_days", 0) or 0)
    if days <= 0:
        return 0
    limit = (now or time.time()) - days * 86400
    base = camera_dir(cfg)
    removed = 0
    if not base.is_dir():
        return 0
    for day_dir in base.iterdir():
        if not day_dir.is_dir() or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day_dir.name):
            continue
        for item in day_dir.glob("*.mkv"):
            try:
                if item.stat().st_mtime < limit:
                    item.unlink()
                    removed += 1
            except OSError:
                pass
        try:
            day_dir.rmdir()  # only succeeds when empty
        except OSError:
            pass
    return removed


class IPCameraRecorder:
    """One passthrough recording pipeline for one camera (GLib main loop thread)."""

    def __init__(self, cfg: IPCameraConfig, source_fragment: str, password: str,
                 video_codec: str = "", has_audio: bool = True,
                 on_state: Callable[[str, str], None] | None = None) -> None:
        self.cfg = cfg
        self._fragment = source_fragment
        self._password = password
        self._video_codec = (video_codec or "H264").upper()
        self._has_audio = has_audio
        self._on_state = on_state or (lambda _s, _m: None)
        self._pipeline: Gst.Pipeline | None = None
        self._bus_id = 0
        self._segments: list[str] = []
        self._lock = threading.Lock()
        self.started_at = 0.0
        self.reason = ""

    @property
    def active(self) -> bool:
        return self._pipeline is not None

    @property
    def segments(self) -> list[str]:
        with self._lock:
            return list(self._segments)

    def _pipeline_description(self) -> str:
        if self._video_codec in ("H265", "HEVC"):
            video = "rtph265depay ! h265parse config-interval=-1"
        elif self._video_codec in ("JPEG", "MJPEG"):
            video = "rtpjpegdepay ! jpegparse"
        else:
            video = "rtph264depay ! h264parse config-interval=-1"
        seg_ns = int(max(0.1, float(self.cfg.recording.get("segment_min", 10))) * 60 * Gst.SECOND)
        desc = (f"{self._fragment} "
                f"splitmuxsink name=mux muxer-factory=matroskamux async-finalize=true "
                f"max-size-time={seg_ns} "
                f"ipsrc. ! application/x-rtp,media=video ! queue ! {video} ! queue ! mux.video ")
        if self._has_audio:
            desc += ("ipsrc. ! application/x-rtp,media=audio ! queue ! decodebin3 caps=\"audio/x-alaw;"
                     "audio/x-mulaw;audio/mpeg\" ! queue ! mux.audio_%u ")
        return desc

    def start(self, reason: str = "manual") -> bool:
        if self._pipeline is not None:
            return True
        directory = camera_dir(self.cfg)
        if free_bytes(directory) < MIN_FREE_BYTES:
            self._on_state("error", "low-disk")
            return False
        try:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            pipeline = Gst.parse_launch(self._pipeline_description())
        except (OSError, GLib.Error) as exc:
            log.warning("IP recorder setup failed: %s", redact(exc))
            self._on_state("error", "setup")
            return False
        src = pipeline.get_by_name("ipsrc")
        if src is not None and (self.cfg.username or self._password):
            src.set_property("user-id", self.cfg.username)
            src.set_property("user-pw", self._password)
        mux = pipeline.get_by_name("mux")
        mux.connect("format-location", self._format_location)
        bus = pipeline.get_bus()
        bus.add_signal_watch()
        self._bus_id = bus.connect("message", self._on_message)
        self._pipeline = pipeline
        self.started_at = time.time()
        self.reason = reason
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self._teardown()
            self._on_state("error", "start")
            return False
        self._on_state("recording", reason)
        return True

    def _format_location(self, _mux, _index) -> str:
        now = _dt.datetime.now()
        day = camera_dir(self.cfg) / now.strftime("%Y-%m-%d")
        day.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = day / f"{now.strftime('%H-%M-%S')}.mkv"
        n = 1
        while path.exists():
            path = day / f"{now.strftime('%H-%M-%S')}_{n}.mkv"
            n += 1
        with self._lock:
            self._segments.append(str(path))
        return str(path)

    def _on_message(self, _bus, msg) -> None:
        if msg.type == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            log.warning("IP recording error: %s (%s)", redact(err.message), redact(dbg))
            self._teardown(finalize=False)
            self._on_state("error", "stream")
        elif msg.type == Gst.MessageType.EOS:
            self._teardown(finalize=False)
            self._on_state("stopped", self.reason)

    def stop(self, timeout_s: float = 5.0) -> None:
        """Finalize the current segment (EOS) and release the stream."""
        pipeline = self._pipeline
        if pipeline is None:
            return
        pipeline.send_event(Gst.Event.new_eos())
        msg = pipeline.get_bus().timed_pop_filtered(
            int(timeout_s * Gst.SECOND), Gst.MessageType.EOS | Gst.MessageType.ERROR)
        if msg is None:
            log.warning("IP recording did not finalize within %.0fs", timeout_s)
        self._teardown(finalize=False)
        self._on_state("stopped", self.reason)

    def _teardown(self, finalize: bool = False) -> None:
        pipeline, self._pipeline = self._pipeline, None
        if pipeline is None:
            return
        bus = pipeline.get_bus()
        if self._bus_id:
            bus.disconnect(self._bus_id)
            self._bus_id = 0
        bus.remove_signal_watch()
        pipeline.set_state(Gst.State.NULL)


def build_recorder(backend, camera, cfg: IPCameraConfig, caps=None,
                   on_state: Callable[[str, str], None] | None = None) -> IPCameraRecorder | None:
    fragment = backend.get_passthrough_source(camera, stream="main")
    if not fragment:
        return None
    return IPCameraRecorder(cfg, fragment, cfg.password(),
                            video_codec=getattr(caps, "video_codec", "") or "H264",
                            has_audio=bool(cfg.audio and (caps is None or caps.audio.state != "unsupported")),
                            on_state=on_state)


__all__ = ["IPCameraRecorder", "build_recorder", "apply_retention", "camera_dir", "parse_schedule",
           "in_schedule", "free_bytes", "default_root", "gst_quote"]
