"""Discover what an IP camera really supports.

Every capability carries a state:

* ``confirmed``   – the device answered the corresponding request;
* ``unsupported`` – the device/protocol states it is absent (e.g. RelayOutputs=0);
* ``needs-auth``  – the device refused because credentials are missing/wrong;
* ``unknown``     – not probed or the probe failed for another reason.

The UI enables a control only for ``confirmed`` capabilities.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from core.ipcam import rtsp, yoosee
from utils.i18n import _
from core.ipcam.models import IPCameraConfig
from core.ipcam.onvif import (
    OnvifAuthError, OnvifClient, OnvifError, OnvifFault, OnvifProfile, find_local,
    findall_local, text_local,
)

log = logging.getLogger(__name__)

CONFIRMED = "confirmed"
UNSUPPORTED = "unsupported"
NEEDS_AUTH = "needs-auth"
UNKNOWN = "unknown"


@dataclass
class Capability:
    state: str = UNKNOWN
    detail: str = ""

    @property
    def available(self) -> bool:
        return self.state == CONFIRMED


@dataclass
class PTZInfo:
    pan_tilt: bool = False
    zoom: bool = False
    continuous: bool = False
    relative: bool = False
    presets: bool = False
    home: bool = False
    max_presets: int = 0


@dataclass
class IPCameraCapabilities:
    video: Capability = field(default_factory=Capability)
    audio: Capability = field(default_factory=Capability)
    ptz: Capability = field(default_factory=Capability)
    ptz_info: PTZInfo = field(default_factory=PTZInfo)
    imaging: Capability = field(default_factory=Capability)
    ircut: Capability = field(default_factory=Capability)
    events: Capability = field(default_factory=Capability)
    relays: Capability = field(default_factory=Capability)
    relay_tokens: list[str] = field(default_factory=list)
    light: Capability = field(default_factory=Capability)
    siren: Capability = field(default_factory=Capability)
    talkback: Capability = field(default_factory=Capability)
    sd_playback: Capability = field(default_factory=Capability)
    snapshot_uri: str = ""
    profiles: list[OnvifProfile] = field(default_factory=list)
    device_info: dict[str, str] = field(default_factory=dict)
    video_codec: str = ""
    audio_codec: str = ""
    rtsp_status: str = ""
    vendor: str = "generic"
    video_source_token: str = ""
    ptz_profile: str = ""
    ptz_service: str = ""
    imaging_service: str = ""
    events_service: str = ""

    def summary(self) -> dict[str, str]:
        return {name: getattr(self, name).state for name in
                ("video", "audio", "ptz", "imaging", "ircut", "events", "relays", "light",
                 "siren", "talkback", "sd_playback")}


def _state_for(exc: Exception) -> Capability:
    if isinstance(exc, OnvifAuthError):
        return Capability(NEEDS_AUTH, "credentials required or rejected")
    if isinstance(exc, OnvifFault):
        return Capability(UNSUPPORTED, f"device fault: {exc.code}")
    return Capability(UNKNOWN, str(exc)[:120])


def make_client(config: IPCameraConfig, password: str | None = None, timeout: float = 5.0) -> OnvifClient | None:
    if not config.onvif_port:
        return None
    return OnvifClient(config.host, config.onvif_port, config.username,
                       config.password() if password is None else password, timeout=timeout)


def probe_capabilities(config: IPCameraConfig, password: str | None = None,
                       timeout: float = 5.0) -> IPCameraCapabilities:
    """Probe RTSP and ONVIF. Blocking; call from a worker thread."""
    caps = IPCameraCapabilities()
    pw = config.password() if password is None else password

    # -- RTSP: the real source of truth for live video ----------------------
    if config.is_rtsp:
        result = rtsp.probe(config.host, config.rtsp_port, config.active_path, config.username, pw,
                            timeout=timeout, tls=config.scheme == "rtsps")
        caps.rtsp_status = result.status
        if result.ok:
            caps.video = Capability(CONFIRMED if result.video else UNSUPPORTED)
            caps.video_codec = result.video.codec if result.video else ""
            caps.audio = Capability(CONFIRMED if result.audio else UNSUPPORTED)
            caps.audio_codec = result.audio.codec if result.audio else ""
        elif result.status in (rtsp.AUTH_REQUIRED, rtsp.AUTH_FAILED):
            caps.video = Capability(NEEDS_AUTH, result.status)
            caps.audio = Capability(NEEDS_AUTH, result.status)  # SDP is only sent after auth
        else:
            caps.video = Capability(UNKNOWN, result.status)
        if yoosee.looks_like_yoosee(realm=result.realm, rtsp_methods=result.methods,
                                    onvif_port=config.onvif_port, stream_uri=config.main_path):
            caps.vendor = "yoosee"
    else:
        caps.video = Capability(UNKNOWN, _("HTTP stream: verified when the preview starts"))

    client = make_client(config, pw, timeout)
    if client is None:
        for name in ("ptz", "imaging", "ircut", "events", "relays"):
            setattr(caps, name, Capability(UNSUPPORTED, _("ONVIF not configured")))
        _apply_vendor_limits(caps)
        return caps

    # -- ONVIF device / media ----------------------------------------------
    try:
        client.sync_time()
        caps.device_info = client.get_device_information()
        services = client.get_capabilities()
        caps.profiles = client.get_profiles()
    except OnvifError as exc:
        log.info("ONVIF probe failed for %s: %s", config.host, type(exc).__name__)
        for name in ("ptz", "imaging", "ircut", "events", "relays"):
            setattr(caps, name, _state_for(exc))
        _apply_vendor_limits(caps)
        return caps

    if yoosee.looks_like_yoosee(onvif_port=config.onvif_port,
                                manufacturer=caps.device_info.get("Manufacturer", ""),
                                model=caps.device_info.get("Model", ""),
                                realm=yoosee.YOOSEE_RTSP_REALM if caps.vendor == "yoosee" else "",
                                stream_uri=config.main_path):
        caps.vendor = "yoosee"
        # Yoosee firmware advertises shuffled XAddrs (Media→ptz_service, PTZ→
        # deviceio_service, DeviceIO with a stale LAN IP). gSOAP dispatches on
        # the SOAP action, so the device service endpoint serves everything.
        services.media = services.ptz = services.events = client.device_url
        services.imaging = services.imaging or client.device_url
        services.deviceio = client.device_url

    caps.ptz_service, caps.imaging_service = services.ptz, services.imaging
    caps.events_service = services.events
    main = caps.profiles[0] if caps.profiles else None
    caps.ptz_profile = next((p.token for p in caps.profiles if p.ptz_config_token), "")
    caps.video_source_token = main.video_source_token if main else ""
    if main and not caps.audio_codec and main.audio_encoding:
        caps.audio_codec = main.audio_encoding
    if main:
        try:
            caps.snapshot_uri = client.get_snapshot_uri(main.token)
        except OnvifError:
            caps.snapshot_uri = ""

    # -- PTZ ----------------------------------------------------------------
    if not caps.ptz_profile:
        caps.ptz = Capability(UNSUPPORTED, _("the camera has no PTZ configuration"))
    elif not services.ptz:
        caps.ptz = Capability(UNSUPPORTED, _("the camera has no PTZ service"))
    else:
        try:
            nodes = client.ptz_get_nodes()
            spaces = find_local(nodes, "SupportedPTZSpaces")
            info = caps.ptz_info
            info.continuous = find_local(spaces, "ContinuousPanTiltVelocitySpace") is not None
            info.relative = find_local(spaces, "RelativePanTiltTranslationSpace") is not None
            info.pan_tilt = info.continuous or info.relative or \
                find_local(spaces, "AbsolutePanTiltPositionSpace") is not None
            info.zoom = find_local(spaces, "ContinuousZoomVelocitySpace") is not None or \
                find_local(spaces, "RelativeZoomTranslationSpace") is not None
            info.max_presets = int(text_local(nodes, "MaximumNumberOfPresets", default="0") or 0)
            info.home = text_local(nodes, "HomeSupported").lower() == "true"
            if nodes is None or not findall_local(nodes, "PTZNode"):
                # Some firmwares answer GetNodes empty but still move with ContinuousMove.
                info.pan_tilt = info.continuous = True
            caps.ptz = Capability(CONFIRMED if (info.pan_tilt or info.zoom) else UNSUPPORTED)
            try:
                client.ptz_get_presets(caps.ptz_profile)
                info.presets = True
            except OnvifError:
                info.presets = info.max_presets > 0
        except OnvifError as exc:
            caps.ptz = _state_for(exc)

    # -- Imaging / IR cut ----------------------------------------------------
    if services.imaging and caps.video_source_token:
        try:
            settings = client.imaging_get_settings(caps.video_source_token)
            caps.imaging = Capability(CONFIRMED)
            ircut = text_local(settings, "IrCutFilter")
            caps.ircut = Capability(CONFIRMED, ircut) if ircut else Capability(
                UNSUPPORTED, _("infrared filter not reported"))
        except OnvifError as exc:
            caps.imaging = caps.ircut = _state_for(exc)
    else:
        caps.imaging = caps.ircut = Capability(UNSUPPORTED, _("the camera has no imaging service"))

    # -- Events ---------------------------------------------------------------
    if services.events:
        try:
            client.events_get_properties()
            caps.events = Capability(CONFIRMED)
        except OnvifError as exc:
            caps.events = _state_for(exc)
    else:
        caps.events = Capability(UNSUPPORTED, _("the camera has no event service"))

    # -- Relay outputs (generic light/siren wiring on many cameras) -----------
    if services.audio_outputs == 0:
        caps.talkback = Capability(UNSUPPORTED, _("the camera reports no audio output"))
    if services.relay_outputs == 0:
        caps.relays = Capability(UNSUPPORTED, _("the camera reports no alarm outputs (RelayOutputs=0)"))
        _apply_vendor_limits(caps)
        return caps
    try:
        caps.relay_tokens = [t for t in client.get_relay_outputs() if t]
        caps.relays = Capability(CONFIRMED if caps.relay_tokens else UNSUPPORTED,
                                 "" if caps.relay_tokens else "RelayOutputs=0")
    except OnvifError as exc:
        caps.relays = _state_for(exc)

    _apply_vendor_limits(caps)
    return caps


def _apply_vendor_limits(caps: IPCameraCapabilities) -> None:
    if caps.vendor == "yoosee":
        for limit in yoosee.YOOSEE_LIMITATIONS:
            current = getattr(caps, limit.feature)
            if not current.available:
                setattr(caps, limit.feature, Capability(UNSUPPORTED, limit.reason))
    else:
        for name in ("light", "siren", "talkback", "sd_playback"):
            if getattr(caps, name).state == UNKNOWN:
                setattr(caps, name, Capability(UNSUPPORTED, _("no standard API detected")))
