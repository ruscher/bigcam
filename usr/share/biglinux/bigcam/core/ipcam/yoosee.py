"""Yoosee/Gwell specific knowledge, applied only when the device is identified as such.

Everything here was observed on a real device (firmware 33.00.25, ONVIF on TCP
5000, RTSP realm ``HIipCamera``) or is documented by the vendor; see
``docs/IP_CAMERA_02_DIAGNOSTICO_YOOSEE.md``.  Features without a documented
local API (white light, siren, SD-card playback, talk-back) are reported as
unavailable instead of being simulated.
"""

from __future__ import annotations

from dataclasses import dataclass

from utils.i18n import _

YOOSEE_RTSP_REALM = "HIipCamera"
YOOSEE_ONVIF_PORT = 5000
YOOSEE_MAIN_PATH = "/onvif1"
YOOSEE_SUB_PATH = "/onvif2"
YOOSEE_MAIN_PROFILE = "IPCProfilesToken0"
YOOSEE_SUB_PROFILE = "IPCProfilesToken1"
# Gwell/Yoosee documentation names the NVR user "admin" (RTSP) and the NVR
# guide uses "administrator"; the password is the one set in the app under
# Settings → NVR connection.  No default password is assumed.
YOOSEE_USERNAMES = ("admin", "administrator")


@dataclass(frozen=True)
class VendorLimitation:
    feature: str
    reason: str


# Reasons are kept short; the UI translates the feature names.
YOOSEE_LIMITATIONS = (
    VendorLimitation("light", _("no documented local API; the Yoosee app uses the vendor cloud/P2P protocol")),
    VendorLimitation("siren", _("no documented local API; the Yoosee app uses the vendor cloud/P2P protocol")),
    VendorLimitation("talkback", _("the camera reports no audio output; talk-back uses the proprietary P2P protocol")),
    VendorLimitation("sd_playback", _("no ONVIF recording service; memory-card playback is only in the Yoosee app")),
)


def looks_like_yoosee(realm: str = "", rtsp_methods: list[str] | None = None, onvif_port: int = 0,
                      manufacturer: str = "", model: str = "", stream_uri: str = "") -> bool:
    """Heuristic identification from facts gathered during probing (no single signal is enough)."""
    score = 0
    if realm == YOOSEE_RTSP_REALM:
        score += 2
    if rtsp_methods and "USER_CMD_SET" in rtsp_methods:
        score += 2
    if onvif_port == YOOSEE_ONVIF_PORT:
        score += 1
    if manufacturer.strip().lower() == "technology" and model.strip().upper() == "IPC":
        score += 1
    if stream_uri.rstrip("/").endswith(YOOSEE_MAIN_PATH):
        score += 1
    return score >= 3
