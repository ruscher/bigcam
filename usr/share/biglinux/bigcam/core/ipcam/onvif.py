"""Minimal, dependency-free ONVIF SOAP client.

Only the Python standard library is used so the client works on every
distribution BigCam targets.  The module never imports GTK and every network
call is synchronous with an explicit timeout; callers run it in a worker
thread (see ``utils.async_worker``).

Credentials are kept in memory only and are never included in exception
messages or log records.
"""

from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import http.client
import logging
import os
import socket
import threading
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit
from xml.sax.saxutils import escape

log = logging.getLogger(__name__)

NS = {
    "s": "http://www.w3.org/2003/05/soap-envelope",
    "tds": "http://www.onvif.org/ver10/device/wsdl",
    "trt": "http://www.onvif.org/ver10/media/wsdl",
    "tptz": "http://www.onvif.org/ver20/ptz/wsdl",
    "timg": "http://www.onvif.org/ver20/imaging/wsdl",
    "tev": "http://www.onvif.org/ver10/events/wsdl",
    "tmd": "http://www.onvif.org/ver10/deviceIO/wsdl",
    "tse": "http://www.onvif.org/ver10/search/wsdl",
    "trc": "http://www.onvif.org/ver10/recording/wsdl",
    "trp": "http://www.onvif.org/ver10/replay/wsdl",
    "tt": "http://www.onvif.org/ver10/schema",
    "wsnt": "http://docs.oasis-open.org/wsn/b-2",
    "wsa": "http://www.w3.org/2005/08/addressing",
}

_WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
_WSU = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
_DIGEST = ("http://docs.oasis-open.org/wss/2004/01/"
           "oasis-200401-wss-username-token-profile-1.0#PasswordDigest")

MAX_RESPONSE = 4 * 1024 * 1024


class OnvifError(Exception):
    """Network, protocol or parsing failure (message never contains secrets)."""


class OnvifAuthError(OnvifError):
    """The device rejected the supplied credentials."""


class OnvifDenied(OnvifAuthError):
    """The device closed the connection without answering.

    Observed on Yoosee/gSOAP firmware for protected operations (PTZ, imaging,
    events) when the request lacks valid WS-Security credentials.
    """


class OnvifFault(OnvifError):
    """The device answered with a SOAP fault (e.g. unsupported action)."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(f"{code}: {reason}" if reason else code)
        self.code = code
        self.reason = reason


def _q(prefix: str, tag: str) -> str:
    return f"{{{NS[prefix]}}}{tag}"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def find_local(node: ET.Element | None, *names: str) -> ET.Element | None:
    """Find the first descendant whose local name path matches ``names``."""
    if node is None:
        return None
    current = [node]
    for name in names:
        nxt = []
        for element in current:
            for child in element.iter():
                if child is not element and _local(child.tag) == name:
                    nxt.append(child)
                    break
        if not nxt:
            return None
        current = nxt
    return current[0]


def child_local(node: ET.Element | None, name: str) -> ET.Element | None:
    """Direct child by local name (``find_local`` searches the whole subtree)."""
    if node is None:
        return None
    return next((child for child in node if _local(child.tag) == name), None)


def findall_local(node: ET.Element | None, name: str) -> list[ET.Element]:
    if node is None:
        return []
    return [child for child in node.iter() if _local(child.tag) == name]


def text_local(node: ET.Element | None, *names: str, default: str = "") -> str:
    found = find_local(node, *names)
    if found is None or found.text is None:
        return default
    return found.text.strip()


@dataclass
class OnvifProfile:
    token: str
    name: str
    encoding: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    bitrate: int = 0
    audio_encoding: str = ""
    ptz_config_token: str = ""
    video_source_token: str = ""


@dataclass
class OnvifServices:
    device: str = ""
    media: str = ""
    ptz: str = ""
    imaging: str = ""
    events: str = ""
    deviceio: str = ""
    recording: str = ""
    search: str = ""
    replay: str = ""
    relay_outputs: int = -1      # -1 = not reported
    audio_outputs: int = -1
    audio_sources: int = -1
    extra: dict[str, str] = field(default_factory=dict)


class OnvifClient:
    """ONVIF client speaking SOAP 1.2 over HTTP with WS-UsernameToken auth."""

    def __init__(self, host: str, port: int = 80, username: str = "", password: str = "",
                 timeout: float = 6.0, path: str = "/onvif/device_service") -> None:
        if not host or any(c in host for c in "/\\@ \t\r\n"):
            raise ValueError("Invalid ONVIF host")
        if not 0 < int(port) < 65536:
            raise ValueError("Invalid ONVIF port")
        self.host = host
        self.port = int(port)
        self.username = username or ""
        self._password = password or ""
        self.timeout = float(timeout)
        self.device_url = f"http://{self._netloc()}{path}"
        self.services = OnvifServices(device=self.device_url)
        self._time_offset = _dt.timedelta(0)
        self._lock = threading.Lock()
        self._use_wsse: bool | None = None

    # -- helpers ------------------------------------------------------------

    def _netloc(self, port: int | None = None) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{host}:{port or self.port}"

    def fix_xaddr(self, url: str) -> str:
        """Devices often advertise their internal/LAN IP; keep scheme, port and path only."""
        if not url:
            return ""
        parts = urlsplit(url.strip())
        if parts.scheme not in {"http", "https"}:
            return ""
        # The configured port wins too: behind NAT/tunnels the advertised one is wrong.
        return urlunsplit((parts.scheme, self._netloc(), parts.path or "/", parts.query, ""))

    def _security_header(self) -> str:
        if not self.username:
            return ""
        nonce = os.urandom(16)
        created = (_dt.datetime.now(_dt.timezone.utc) + self._time_offset).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z")
        digest = base64.b64encode(hashlib.sha1(
            nonce + created.encode() + self._password.encode("utf-8")).digest()).decode()
        return (
            f'<s:Header><wsse:Security xmlns:wsse="{_WSSE}" xmlns:wsu="{_WSU}" '
            f's:mustUnderstand="1"><wsse:UsernameToken>'
            f"<wsse:Username>{escape(self.username)}</wsse:Username>"
            f'<wsse:Password Type="{_DIGEST}">{digest}</wsse:Password>'
            f'<wsse:Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/'
            f'oasis-200401-wss-soap-message-security-1.0#Base64Binary">'
            f"{base64.b64encode(nonce).decode()}</wsse:Nonce>"
            f"<wsu:Created>{created}</wsu:Created>"
            f"</wsse:UsernameToken></wsse:Security></s:Header>"
        )

    def _envelope(self, body: str, auth: bool) -> bytes:
        ns = " ".join(f'xmlns:{k}="{v}"' for k, v in NS.items() if k != "s")
        header = self._security_header() if auth else ""
        return (f'<?xml version="1.0" encoding="UTF-8"?>'
                f'<s:Envelope xmlns:s="{NS["s"]}" {ns}>{header}<s:Body>{body}</s:Body>'
                f"</s:Envelope>").encode("utf-8")

    def _post(self, url: str, payload: bytes, extra_headers: dict | None = None
              ) -> tuple[int, dict, bytes]:
        parts = urlsplit(url)
        conn_cls = (http.client.HTTPSConnection if parts.scheme == "https"
                    else http.client.HTTPConnection)
        conn = conn_cls(parts.hostname, parts.port, timeout=self.timeout)
        headers = {"Content-Type": "application/soap+xml; charset=utf-8",
                   "Connection": "close", "User-Agent": "BigCam-ONVIF"}
        if extra_headers:
            headers.update(extra_headers)
        try:
            conn.request("POST", parts.path + (f"?{parts.query}" if parts.query else ""),
                         body=payload, headers=headers)
            response = conn.getresponse()
            data = response.read(MAX_RESPONSE + 1)
            if len(data) > MAX_RESPONSE:
                raise OnvifError("ONVIF response too large")
            return response.status, {k.lower(): v for k, v in response.getheaders()}, data
        except http.client.RemoteDisconnected:
            raise OnvifDenied("Device closed the connection without a reply") from None
        except (OSError, socket.timeout, http.client.HTTPException) as exc:
            raise OnvifError(f"ONVIF connection failed: {type(exc).__name__}") from None
        finally:
            conn.close()

    def _digest_header(self, url: str, challenge: str, payload: bytes) -> str:
        """HTTP Digest (RFC 7616, MD5) for devices that require it instead of WSSE."""
        params = {}
        for item in _split_auth(challenge.split(" ", 1)[1] if " " in challenge else ""):
            if "=" in item:
                key, value = item.split("=", 1)
                params[key.strip().lower()] = value.strip().strip('"')
        realm, nonce = params.get("realm", ""), params.get("nonce", "")
        qop = "auth" if "auth" in params.get("qop", "").split(",") else ""
        path = urlsplit(url).path or "/"
        ha1 = hashlib.md5(f"{self.username}:{realm}:{self._password}".encode()).hexdigest()
        ha2 = hashlib.md5(f"POST:{path}".encode()).hexdigest()
        cnonce = os.urandom(8).hex()
        if qop:
            response = hashlib.md5(f"{ha1}:{nonce}:00000001:{cnonce}:{qop}:{ha2}".encode()).hexdigest()
        else:
            response = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
        value = (f'Digest username="{self.username}", realm="{realm}", nonce="{nonce}", '
                 f'uri="{path}", response="{response}"')
        if qop:
            value += f', qop={qop}, nc=00000001, cnonce="{cnonce}"'
        if "opaque" in params:
            value += f', opaque="{params["opaque"]}"'
        return value

    # -- core call ----------------------------------------------------------

    def call(self, url: str, body: str, auth: bool = True) -> ET.Element:
        """POST a SOAP body and return the parsed ``<Body>`` element."""
        if not url:
            raise OnvifFault("NotSupported", "Service not available")
        use_auth = auth and bool(self.username)
        status, headers, data = self._post(url, self._envelope(body, use_auth))
        if status == 401 and self.username and "digest" in headers.get("www-authenticate", "").lower():
            payload = self._envelope(body, use_auth)
            status, headers, data = self._post(
                url, payload, {"Authorization": self._digest_header(
                    url, headers["www-authenticate"], payload)})
        if status == 401:
            raise OnvifAuthError("Authentication required or credentials rejected")
        try:
            root = ET.fromstring(data)
        except ET.ParseError:
            raise OnvifError(f"Invalid ONVIF response (HTTP {status})") from None
        body_el = find_local(root, "Body")
        if body_el is None:
            raise OnvifError("SOAP response without body")
        fault = find_local(body_el, "Fault")
        if fault is not None:
            codes = [(_t.text or "").strip() for _t in findall_local(fault, "Value")]
            code = codes[-1] if codes else "Fault"
            reason = text_local(fault, "Text")
            low = (code + " " + reason).lower()
            if any(k in low for k in ("notauthorized", "not authorized", "authority",
                                      "authentication", "password", "sender not authorized")):
                raise OnvifAuthError("Credentials rejected by device")
            raise OnvifFault(code, reason[:200])
        if status >= 400:
            raise OnvifError(f"HTTP {status}")
        return body_el

    # -- device -------------------------------------------------------------

    def sync_time(self) -> None:
        """Compensate clock skew so WS-UsernameToken timestamps are accepted."""
        try:
            body = self.call(self.device_url, "<tds:GetSystemDateAndTime/>", auth=False)
        except OnvifError:
            return
        utc = find_local(body, "UTCDateTime")
        if utc is None:
            return
        try:
            device = _dt.datetime(
                int(text_local(utc, "Year")), int(text_local(utc, "Month")),
                int(text_local(utc, "Day")), int(text_local(utc, "Hour")),
                int(text_local(utc, "Minute")), int(text_local(utc, "Second")),
                tzinfo=_dt.timezone.utc)
        except ValueError:
            return
        self._time_offset = device - _dt.datetime.now(_dt.timezone.utc)

    def get_device_information(self) -> dict[str, str]:
        body = self._call_auth_fallback(self.device_url, "<tds:GetDeviceInformation/>")
        return {key: text_local(body, key) for key in
                ("Manufacturer", "Model", "FirmwareVersion", "SerialNumber", "HardwareId")}

    def _call_auth_fallback(self, url: str, body: str) -> ET.Element:
        """Try authenticated first, then anonymous (many devices allow both).

        Some firmwares reject a WSSE header with wrong credentials even on
        operations that are otherwise public, so the result of the first
        successful mode is remembered.
        """
        modes = [True, False] if self._use_wsse is not False else [False, True]
        if not self.username:
            modes = [False]
        last: OnvifError | None = None
        for mode in modes:
            try:
                result = self.call(url, body, auth=mode)
                if self._use_wsse is None and self.username:
                    self._use_wsse = mode
                return result
            except OnvifAuthError as exc:
                last = exc
            except OnvifFault as exc:
                # Some devices answer bad credentials with a generic fault.
                last = exc
                if not mode:
                    break
        assert last is not None
        raise last

    def get_capabilities(self) -> OnvifServices:
        body = self._call_auth_fallback(
            self.device_url, "<tds:GetCapabilities><tds:Category>All</tds:Category>"
                             "</tds:GetCapabilities>")
        caps = find_local(body, "Capabilities")
        svc = OnvifServices(device=self.device_url)
        for name, attr in (("Media", "media"), ("PTZ", "ptz"), ("Imaging", "imaging"),
                           ("Events", "events")):
            node = child_local(caps, name)
            if node is not None:
                setattr(svc, attr, self.fix_xaddr(text_local(node, "XAddr")))
        # Direct child only: Device/Network also contains an <Extension>.
        ext = child_local(caps, "Extension")
        for name, attr in (("DeviceIO", "deviceio"), ("Recording", "recording"),
                           ("Search", "search"), ("Replay", "replay")):
            node = child_local(ext, name)
            if node is not None:
                setattr(svc, attr, self.fix_xaddr(text_local(node, "XAddr")))
                if name == "DeviceIO":
                    for key, field_name in (("RelayOutputs", "relay_outputs"),
                                            ("AudioOutputs", "audio_outputs"),
                                            ("AudioSources", "audio_sources")):
                        value = text_local(node, key)
                        if value.isdigit():
                            setattr(svc, field_name, int(value))
        # GetServices is optional; use it to fill gaps when present.
        try:
            sbody = self._call_auth_fallback(
                self.device_url,
                "<tds:GetServices><tds:IncludeCapability>false</tds:IncludeCapability>"
                "</tds:GetServices>")
            for service in findall_local(sbody, "Service"):
                namespace = text_local(service, "Namespace")
                xaddr = self.fix_xaddr(text_local(service, "XAddr"))
                for key, attr in (("ver10/media", "media"), ("ptz", "ptz"),
                                  ("imaging", "imaging"), ("events", "events"),
                                  ("deviceIO", "deviceio"), ("recording", "recording"),
                                  ("search", "search"), ("replay", "replay")):
                    if key in namespace and not getattr(svc, attr):
                        setattr(svc, attr, xaddr)
                if namespace:
                    svc.extra[namespace] = xaddr
        except OnvifError:
            pass
        self.services = svc
        return svc

    def get_scopes(self) -> list[str]:
        body = self._call_auth_fallback(self.device_url, "<tds:GetScopes/>")
        return [text_local(s, "ScopeItem") for s in findall_local(body, "Scopes")]

    def get_relay_outputs(self) -> list[str]:
        body = self.call(self.services.deviceio or self.device_url, "<tds:GetRelayOutputs/>"
                         if not self.services.deviceio else "<tmd:GetRelayOutputs/>")
        return [r.get("token", "") for r in findall_local(body, "RelayOutputs")]

    def set_relay_output_state(self, token: str, active: bool) -> None:
        state = "active" if active else "inactive"
        self.call(self.device_url,
                  f"<tds:SetRelayOutputState><tds:RelayOutputToken>{escape(token)}"
                  f"</tds:RelayOutputToken><tds:LogicalState>{state}</tds:LogicalState>"
                  f"</tds:SetRelayOutputState>")

    # -- media --------------------------------------------------------------

    def get_profiles(self) -> list[OnvifProfile]:
        body = self._call_auth_fallback(self.services.media or self.device_url,
                                        "<trt:GetProfiles/>")
        profiles = []
        for node in findall_local(body, "Profiles"):
            venc = find_local(node, "VideoEncoderConfiguration")
            res = find_local(venc, "Resolution")
            rate = find_local(venc, "RateControl")
            aenc = find_local(node, "AudioEncoderConfiguration")
            ptz = find_local(node, "PTZConfiguration")
            vsrc = find_local(node, "VideoSourceConfiguration")
            profiles.append(OnvifProfile(
                token=node.get("token", ""),
                name=text_local(node, "Name") or node.get("token", ""),
                encoding=text_local(venc, "Encoding"),
                width=_int(text_local(res, "Width")),
                height=_int(text_local(res, "Height")),
                fps=float(_int(text_local(rate, "FrameRateLimit"))),
                bitrate=_int(text_local(rate, "BitrateLimit")),
                audio_encoding=text_local(aenc, "Encoding") if aenc is not None else "",
                ptz_config_token=ptz.get("token", "") if ptz is not None else "",
                video_source_token=text_local(vsrc, "SourceToken") if vsrc is not None else "",
            ))
        return profiles

    def fix_media_uri(self, uri: str) -> str:
        """Replace the advertised host (often a stale LAN IP) by the configured one."""
        parts = urlsplit((uri or "").strip())
        if parts.scheme not in {"rtsp", "rtsps", "http", "https"} or not parts.hostname:
            return ""
        host = f"[{self.host}]" if ":" in self.host else self.host
        netloc = f"{host}:{parts.port}" if parts.port else host
        return urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, ""))

    def get_stream_uri(self, profile_token: str, transport: str = "RTSP") -> str:
        body = self._call_auth_fallback(
            self.services.media or self.device_url,
            "<trt:GetStreamUri><trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream>"
            f"<tt:Transport><tt:Protocol>{escape(transport)}</tt:Protocol></tt:Transport>"
            f"</trt:StreamSetup><trt:ProfileToken>{escape(profile_token)}</trt:ProfileToken>"
            "</trt:GetStreamUri>")
        return self.fix_media_uri(text_local(body, "Uri"))

    def get_snapshot_uri(self, profile_token: str) -> str:
        body = self._call_auth_fallback(
            self.services.media or self.device_url,
            f"<trt:GetSnapshotUri><trt:ProfileToken>{escape(profile_token)}"
            "</trt:ProfileToken></trt:GetSnapshotUri>")
        return self.fix_media_uri(text_local(body, "Uri"))

    # -- PTZ ----------------------------------------------------------------

    def ptz_get_nodes(self) -> ET.Element:
        return self._call_auth_fallback(self.services.ptz, "<tptz:GetNodes/>")

    def ptz_get_configurations(self) -> ET.Element:
        return self._call_auth_fallback(self.services.ptz, "<tptz:GetConfigurations/>")

    def ptz_get_status(self, profile: str) -> ET.Element:
        return self._call_auth_fallback(
            self.services.ptz,
            f"<tptz:GetStatus><tptz:ProfileToken>{escape(profile)}</tptz:ProfileToken>"
            "</tptz:GetStatus>")

    def ptz_continuous_move(self, profile: str, pan: float, tilt: float, zoom: float = 0.0,
                            timeout_s: float | None = None) -> None:
        pan, tilt, zoom = (_clamp(v) for v in (pan, tilt, zoom))
        velocity = ""
        if pan or tilt or not zoom:
            # All-zero velocity is sent explicitly: it is the stop fallback.
            velocity += f'<tt:PanTilt x="{pan:.3f}" y="{tilt:.3f}"/>'
        if zoom:
            velocity += f'<tt:Zoom x="{zoom:.3f}"/>'
        tout = f"<tptz:Timeout>PT{timeout_s:.1f}S</tptz:Timeout>" if timeout_s else ""
        self._call_auth_fallback(
            self.services.ptz,
            f"<tptz:ContinuousMove><tptz:ProfileToken>{escape(profile)}</tptz:ProfileToken>"
            f"<tptz:Velocity>{velocity}</tptz:Velocity>{tout}</tptz:ContinuousMove>")

    def ptz_relative_move(self, profile: str, pan: float, tilt: float, zoom: float = 0.0) -> None:
        trans = f'<tt:PanTilt x="{_clamp(pan):.3f}" y="{_clamp(tilt):.3f}"/>'
        if zoom:
            trans += f'<tt:Zoom x="{_clamp(zoom):.3f}"/>'
        self._call_auth_fallback(
            self.services.ptz,
            f"<tptz:RelativeMove><tptz:ProfileToken>{escape(profile)}</tptz:ProfileToken>"
            f"<tptz:Translation>{trans}</tptz:Translation></tptz:RelativeMove>")

    def ptz_stop(self, profile: str) -> None:
        self._call_auth_fallback(
            self.services.ptz,
            f"<tptz:Stop><tptz:ProfileToken>{escape(profile)}</tptz:ProfileToken>"
            "<tptz:PanTilt>true</tptz:PanTilt><tptz:Zoom>true</tptz:Zoom></tptz:Stop>")

    def ptz_get_presets(self, profile: str) -> list[tuple[str, str]]:
        body = self._call_auth_fallback(
            self.services.ptz,
            f"<tptz:GetPresets><tptz:ProfileToken>{escape(profile)}</tptz:ProfileToken>"
            "</tptz:GetPresets>")
        return [(p.get("token", ""), text_local(p, "Name") or p.get("token", ""))
                for p in findall_local(body, "Preset")]

    def ptz_set_preset(self, profile: str, name: str, token: str = "") -> str:
        tok = f"<tptz:PresetToken>{escape(token)}</tptz:PresetToken>" if token else ""
        body = self._call_auth_fallback(
            self.services.ptz,
            f"<tptz:SetPreset><tptz:ProfileToken>{escape(profile)}</tptz:ProfileToken>"
            f"<tptz:PresetName>{escape(name[:64])}</tptz:PresetName>{tok}</tptz:SetPreset>")
        return text_local(body, "PresetToken")

    def ptz_goto_preset(self, profile: str, token: str) -> None:
        self._call_auth_fallback(
            self.services.ptz,
            f"<tptz:GotoPreset><tptz:ProfileToken>{escape(profile)}</tptz:ProfileToken>"
            f"<tptz:PresetToken>{escape(token)}</tptz:PresetToken></tptz:GotoPreset>")

    def ptz_remove_preset(self, profile: str, token: str) -> None:
        self._call_auth_fallback(
            self.services.ptz,
            f"<tptz:RemovePreset><tptz:ProfileToken>{escape(profile)}</tptz:ProfileToken>"
            f"<tptz:PresetToken>{escape(token)}</tptz:PresetToken></tptz:RemovePreset>")

    def ptz_goto_home(self, profile: str) -> None:
        self._call_auth_fallback(
            self.services.ptz,
            f"<tptz:GotoHomePosition><tptz:ProfileToken>{escape(profile)}</tptz:ProfileToken>"
            "</tptz:GotoHomePosition>")

    # -- imaging ------------------------------------------------------------

    def imaging_get_settings(self, source_token: str) -> ET.Element:
        return self._call_auth_fallback(
            self.services.imaging,
            f"<timg:GetImagingSettings><timg:VideoSourceToken>{escape(source_token)}"
            "</timg:VideoSourceToken></timg:GetImagingSettings>")

    def imaging_get_options(self, source_token: str) -> ET.Element:
        return self._call_auth_fallback(
            self.services.imaging,
            f"<timg:GetOptions><timg:VideoSourceToken>{escape(source_token)}"
            "</timg:VideoSourceToken></timg:GetOptions>")

    def imaging_set_settings(self, source_token: str, settings_xml: str) -> None:
        self._call_auth_fallback(
            self.services.imaging,
            f"<timg:SetImagingSettings><timg:VideoSourceToken>{escape(source_token)}"
            f"</timg:VideoSourceToken><timg:ImagingSettings>{settings_xml}"
            "</timg:ImagingSettings><timg:ForcePersistence>true</timg:ForcePersistence>"
            "</timg:SetImagingSettings>")

    # -- events -------------------------------------------------------------

    def events_get_properties(self) -> ET.Element:
        return self._call_auth_fallback(self.services.events, "<tev:GetEventProperties/>")

    def events_create_pullpoint(self, termination_s: int = 120) -> str:
        body = self._call_auth_fallback(
            self.services.events,
            "<tev:CreatePullPointSubscription><tev:InitialTerminationTime>"
            f"PT{int(termination_s)}S</tev:InitialTerminationTime>"
            "</tev:CreatePullPointSubscription>")
        address = text_local(body, "SubscriptionReference", "Address")
        return self.fix_xaddr(address) or self.services.events

    def events_pull(self, address: str, timeout_s: int = 5, limit: int = 20) -> list[dict]:
        body = self._call_auth_fallback(
            address,
            f"<tev:PullMessages><tev:Timeout>PT{int(timeout_s)}S</tev:Timeout>"
            f"<tev:MessageLimit>{int(limit)}</tev:MessageLimit></tev:PullMessages>")
        events = []
        for msg in findall_local(body, "NotificationMessage"):
            topic = text_local(msg, "Topic")
            data = {item.get("Name", ""): item.get("Value", "")
                    for item in findall_local(find_local(msg, "Data"), "SimpleItem")}
            source = {item.get("Name", ""): item.get("Value", "")
                      for item in findall_local(find_local(msg, "Source"), "SimpleItem")}
            inner = find_local(msg, "Message")
            utc = ""
            for el in ([inner] if inner is not None else []) + findall_local(inner, "Message"):
                utc = el.get("UtcTime", "") or utc
            events.append({"topic": topic, "data": data, "source": source, "time": utc})
        return events

    def events_unsubscribe(self, address: str) -> None:
        try:
            self._call_auth_fallback(address, "<wsnt:Unsubscribe/>")
        except OnvifError:
            pass

    # -- recording search ---------------------------------------------------

    def search_recording_summary(self) -> ET.Element:
        return self._call_auth_fallback(self.services.search, "<tse:GetRecordingSummary/>")


def _split_auth(value: str) -> list[str]:
    items, current, quoted = [], "", False
    for char in value:
        if char == '"':
            quoted = not quoted
        if char == "," and not quoted:
            items.append(current)
            current = ""
        else:
            current += char
    if current:
        items.append(current)
    return items


def _int(value: str) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _clamp(value: float) -> float:
    return max(-1.0, min(1.0, float(value)))
