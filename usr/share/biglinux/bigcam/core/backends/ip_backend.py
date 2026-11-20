"""IP camera backend – RTSP / HTTP streams with optional ONVIF control.

Credentials never appear in pipeline descriptions, CameraInfo objects or the
settings file: the public URL is used in the pipeline string and the
user/password are injected directly into the source element after parsing
(:meth:`IPBackend.prepare_pipeline`).
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

import gi

gi.require_version("Gst", "1.0")

from constants import BackendType, ControlCategory, ControlType
from gi.repository import GLib, Gst
from utils.i18n import _
from utils.urls import gst_quote

from core.camera_backend import CameraBackend, CameraControl, CameraInfo, VideoFormat
from core.ipcam.capabilities import IPCameraCapabilities, make_client, probe_capabilities
from core.ipcam.models import IPCameraConfig
from core.ipcam.onvif import OnvifError, find_local, text_local
from core.ipcam.security import redact

log = logging.getLogger(__name__)

SOURCE_NAME = "ipsrc"
VOLUME_NAME = "ipvolume"
AUDIO_SINK_NAME = "ipaudiosink"

_IMAGING_CONTROLS = (
    ("Brightness", "brightness", _("Brightness")),
    ("Contrast", "contrast", _("Contrast")),
    ("ColorSaturation", "saturation", _("Saturation")),
    ("Sharpness", "sharpness", _("Sharpness")),
)
_IRCUT_MODES = ("AUTO", "OFF", "ON")


class IPBackend(CameraBackend):
    """Backend for network cameras configured by the user or found by discovery."""

    def __init__(self) -> None:
        self._configs: dict[str, IPCameraConfig] = {}
        self._caps: dict[str, IPCameraCapabilities] = {}
        self._lock = threading.Lock()

    def get_backend_type(self) -> BackendType:
        return BackendType.IP

    def is_available(self) -> bool:
        return True

    # -- registry ------------------------------------------------------------

    def detect_cameras(self) -> list[CameraInfo]:
        # IP cameras come from the persistent configuration; discovery is an
        # explicit user action (see ui/ip_camera_dialog.py).
        return []

    def cameras_from_configs(self, configs: list[IPCameraConfig]) -> list[CameraInfo]:
        cameras = []
        with self._lock:
            self._configs = {cfg.id: cfg for cfg in configs}
            self._caps = {k: v for k, v in self._caps.items() if k in self._configs}
        for cfg in configs:
            cameras.append(self.camera_info(cfg))
        return cameras

    def cameras_from_urls(self, entries: list[dict[str, Any]]) -> list[CameraInfo]:
        """Backward compatible entry point (legacy ``{name, url}`` or new dicts)."""
        configs = []
        for entry in entries:
            try:
                configs.append(IPCameraConfig.from_dict(dict(entry)))
            except (KeyError, TypeError, ValueError):
                log.warning("Ignoring an invalid IP camera configuration")
        return self.cameras_from_configs(configs)

    @staticmethod
    def camera_info(cfg: IPCameraConfig) -> CameraInfo:
        url = cfg.stream_url()
        return CameraInfo(
            id=cfg.id, name=cfg.name, backend=BackendType.IP, device_path=url,
            capabilities=["video"], extra={"url": url, "ipcam_id": cfg.id})

    def config_for(self, camera: CameraInfo | str) -> IPCameraConfig | None:
        cam_id = camera if isinstance(camera, str) else camera.extra.get("ipcam_id", camera.id)
        with self._lock:
            return self._configs.get(cam_id)

    def capabilities_for(self, camera: CameraInfo | str) -> IPCameraCapabilities | None:
        cam_id = camera if isinstance(camera, str) else camera.extra.get("ipcam_id", camera.id)
        with self._lock:
            return self._caps.get(cam_id)

    def refresh_capabilities(self, camera: CameraInfo | str) -> IPCameraCapabilities | None:
        """Probe the device (blocking, worker thread) and cache the result."""
        cfg = self.config_for(camera)
        if cfg is None:
            return None
        caps = probe_capabilities(cfg)
        with self._lock:
            self._caps[cfg.id] = caps
        log.info("IP camera %s capabilities: %s", cfg.id, caps.summary())
        return caps

    # -- controls (ONVIF Imaging) -------------------------------------------

    def get_controls(self, camera: CameraInfo) -> list[CameraControl]:
        cfg = self.config_for(camera)
        if cfg is None or not cfg.onvif_port:
            return []
        caps = self.capabilities_for(camera) or self.refresh_capabilities(camera)
        if caps is None or not caps.imaging.available or not caps.video_source_token:
            return []
        client = make_client(cfg)
        client.services.imaging = caps.imaging_service or client.device_url
        try:
            settings = client.imaging_get_settings(caps.video_source_token)
            try:
                options = client.imaging_get_options(caps.video_source_token)
            except OnvifError:
                options = None
        except OnvifError as exc:
            log.info("ONVIF imaging unavailable: %s", type(exc).__name__)
            return []
        controls: list[CameraControl] = []
        for tag, cid, label in _IMAGING_CONTROLS:
            raw = text_local(settings, tag)
            if not raw:
                continue
            rng = find_local(options, tag) if options is not None else None
            low = int(float(text_local(rng, "Min", default="0"))) if rng is not None else 0
            high = int(float(text_local(rng, "Max", default="100"))) if rng is not None else 100
            value = int(float(raw))
            controls.append(CameraControl(
                id=f"onvif_{cid}", name=label, category=ControlCategory.IMAGE,
                control_type=ControlType.INTEGER, value=value, default=(low + high) // 2,
                minimum=low, maximum=high))
        ircut = text_local(settings, "IrCutFilter")
        if ircut:
            choices = [_("Automatic"), _("Night (infrared)"), _("Day (color)")]
            controls.append(CameraControl(
                id="onvif_ircut", name=_("Night vision"), category=ControlCategory.EXPOSURE,
                control_type=ControlType.MENU,
                value=_IRCUT_MODES.index(ircut) if ircut in _IRCUT_MODES else 0,
                default=0, choices=choices, choice_values=[0, 1, 2]))
        return controls

    def set_control(self, camera: CameraInfo, control_id: str, value: Any) -> bool:
        cfg = self.config_for(camera)
        caps = self.capabilities_for(camera)
        if cfg is None or caps is None or not caps.imaging.available:
            return False
        if control_id == "onvif_ircut":
            mode = _IRCUT_MODES[int(value)] if 0 <= int(value) < 3 else "AUTO"
            xml = f"<tt:IrCutFilter>{mode}</tt:IrCutFilter>"
        else:
            tag = next((t for t, cid, _l in _IMAGING_CONTROLS if f"onvif_{cid}" == control_id), "")
            if not tag:
                return False
            xml = f"<tt:{tag}>{float(value):.1f}</tt:{tag}>"
        client = make_client(cfg)
        client.services.imaging = caps.imaging_service or client.device_url
        try:
            client.imaging_set_settings(caps.video_source_token, xml)
            return True
        except OnvifError as exc:
            log.warning("ONVIF imaging change failed: %s", type(exc).__name__)
            return False

    # -- gstreamer -----------------------------------------------------------

    def get_gst_source(self, camera: CameraInfo, fmt: VideoFormat | None = None,
                       audio: bool | None = None, stream: str | None = None) -> str:
        """Pipeline fragment ending in raw video. Credentials are added later."""
        cfg = self.config_for(camera)
        if cfg is None:
            cfg, _pw = IPCameraConfig.from_url(camera.extra.get("url", camera.device_path),
                                               camera.name, camera.id)
        with_audio = cfg.audio if audio is None else audio
        return self.source_for(cfg, with_audio, stream)

    @staticmethod
    def source_for(cfg: IPCameraConfig, with_audio: bool = False, stream: str | None = None) -> str:
        url = gst_quote(cfg.stream_url(stream))
        if cfg.is_rtsp:
            protocols = {"tcp": "tcp", "udp": "udp", "auto": "tcp+udp+udp-mcast"}[cfg.transport]
            src = (f"rtspsrc name={SOURCE_NAME} location={url} protocols={protocols} "
                   f"latency={cfg.latency_ms} drop-on-latency=true do-rtsp-keep-alive=true "
                   "tcp-timeout=10000000 timeout=5000000 retry=5 ")
            audio_branch = ""
            if with_audio:
                audio_branch = (
                    f"{SOURCE_NAME}. ! application/x-rtp,media=audio ! queue max-size-time=500000000 "
                    "leaky=downstream ! decodebin ! audioconvert ! audioresample ! "
                    f"volume name={VOLUME_NAME} mute={'true' if cfg.muted else 'false'} ! "
                    f"autoaudiosink name={AUDIO_SINK_NAME} sync=false ")
            return (src + audio_branch +
                    f"{SOURCE_NAME}. ! application/x-rtp,media=video ! "
                    "queue max-size-buffers=0 max-size-time=0 max-size-bytes=0 ! "
                    "decodebin ! videoconvert")
        return (f"souphttpsrc name={SOURCE_NAME} location={url} is-live=true timeout=10 "
                "! decodebin ! videoconvert")

    def get_passthrough_source(self, camera: CameraInfo, stream: str | None = None) -> str:
        """Fragment for recording without re-encoding: ``rtspsrc`` with named pads."""
        cfg = self.config_for(camera)
        if cfg is None or not cfg.is_rtsp:
            return ""
        protocols = {"tcp": "tcp", "udp": "udp", "auto": "tcp+udp"}[cfg.transport]
        return (f"rtspsrc name={SOURCE_NAME} location={gst_quote(cfg.stream_url(stream))} "
                f"protocols={protocols} latency={cfg.latency_ms} do-rtsp-keep-alive=true "
                "tcp-timeout=10000000 timeout=5000000 retry=5 ")

    def prepare_pipeline(self, pipeline: Gst.Pipeline, camera: CameraInfo | None = None) -> None:
        def element_added(_pipeline, _subbin, element):
            factory = element.get_factory()
            if factory and factory.get_name() == "multipartdemux":
                # MJPEG cameras keep a single image stream open indefinitely.
                element.set_property("single-stream", True)
        pipeline.connect("deep-element-added", element_added)
        if camera is None:
            return
        cfg = self.config_for(camera)
        src = pipeline.get_by_name(SOURCE_NAME)
        if cfg is None or src is None:
            return
        password = cfg.password()
        if cfg.username or password:
            src.set_property("user-id", cfg.username)
            src.set_property("user-pw", password)

    @classmethod
    def snapshot_bytes(cls, cfg: IPCameraConfig, password: str, stream: str | None = None,
                       timeout_s: float = 12.0) -> tuple[bytes, int, int, int]:
        """Grab one decoded frame (RGBA bytes, width, height, stride) – blocking.

        Used by the "Test connection" preview before the camera is saved, so
        the credentials are passed explicitly instead of read from the keyring.
        """
        pipeline = Gst.parse_launch(
            f"{cls.source_for(cfg, False, stream)} ! videoscale ! "
            "video/x-raw,width=[1,640],pixel-aspect-ratio=1/1 ! videoconvert ! "
            "video/x-raw,format=RGBA ! appsink name=grab max-buffers=1 drop=true sync=false")
        src = pipeline.get_by_name(SOURCE_NAME)
        if src is not None and (cfg.username or password):
            src.set_property("user-id", cfg.username)
            src.set_property("user-pw", password)
        try:
            if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                raise RuntimeError("pipeline")
            sink = pipeline.get_by_name("grab")
            deadline = GLib.get_monotonic_time() + int(timeout_s * 1_000_000)
            bus = pipeline.get_bus()
            while GLib.get_monotonic_time() < deadline:
                sample = sink.emit("try-pull-sample", 200 * Gst.MSECOND)
                if sample is not None:
                    st = sample.get_caps().get_structure(0)
                    w, h = st.get_value("width"), st.get_value("height")
                    buf = sample.get_buffer()
                    data = buf.extract_dup(0, buf.get_size())
                    return data, w, h, len(data) // h
                msg = bus.pop_filtered(Gst.MessageType.ERROR)
                if msg is not None:
                    err, dbg = msg.parse_error()
                    raise RuntimeError(redact(f"{err.message} {dbg or ''}"))
            raise TimeoutError("no frame")
        finally:
            pipeline.set_state(Gst.State.NULL)

    def set_muted(self, pipeline: Gst.Pipeline | None, muted: bool) -> bool:
        vol = pipeline.get_by_name(VOLUME_NAME) if pipeline is not None else None
        if vol is None:
            return False
        vol.set_property("mute", bool(muted))
        return True

    # -- photo ---------------------------------------------------------------

    def can_capture_photo(self) -> bool:
        return True

    def capture_photo(self, camera: CameraInfo, output_path: str) -> bool:
        pipeline = None
        try:
            pipeline = Gst.parse_launch(
                f"{self.get_gst_source(camera, audio=False)} ! jpegenc snapshot=true ! "
                f"filesink location={gst_quote(output_path)}")
            self.prepare_pipeline(pipeline, camera)
            if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                return False
            message = pipeline.get_bus().timed_pop_filtered(
                15 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
            return bool(message and message.type == Gst.MessageType.EOS
                        and os.path.isfile(output_path) and os.path.getsize(output_path) > 0)
        except (OSError, ValueError, GLib.Error) as exc:
            log.warning("Network snapshot failed: %s", redact(exc))
            return False
        finally:
            if pipeline is not None:
                pipeline.set_state(Gst.State.NULL)
