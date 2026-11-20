"""Unit tests for the IP camera subsystem (no real devices, no keyring)."""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from core.ipcam import discovery, rtsp
from core.ipcam.capabilities import CONFIRMED, NEEDS_AUTH, UNSUPPORTED, probe_capabilities
from core.ipcam.models import IPCameraConfig
from core.ipcam.onvif import OnvifAuthError, OnvifClient, OnvifDenied
from core.ipcam.ptz import PTZController
from core.ipcam.recorder import apply_retention, camera_dir, in_schedule, parse_schedule
from core.ipcam.security import CredentialStore, redact, validate_host, validate_path

PASSWORD = "s3cr3t-test"

# ---------------------------------------------------------------- fake ONVIF

PUBLIC = {"GetDeviceInformation", "GetCapabilities", "GetServices", "GetProfiles", "GetStreamUri",
          "GetSystemDateAndTime", "GetSnapshotUri"}

RESPONSES = {
    "GetDeviceInformation": "<tds:GetDeviceInformationResponse><tds:Manufacturer>Technology</tds:Manufacturer>"
                            "<tds:Model>IPC</tds:Model><tds:FirmwareVersion>33.00.25</tds:FirmwareVersion>"
                            "</tds:GetDeviceInformationResponse>",
    # Shuffled XAddrs, exactly as reported by the Yoosee firmware.
    "GetCapabilities": "<tds:GetCapabilitiesResponse><tds:Capabilities>"
                       "<tt:Device><tt:XAddr>http://H/onvif/device_service</tt:XAddr><tt:Network><tt:Extension>"
                       "<tt:Dot11Configuration>false</tt:Dot11Configuration></tt:Extension></tt:Network></tt:Device>"
                       "<tt:Events><tt:XAddr>http://H/onvif/media_service</tt:XAddr></tt:Events>"
                       "<tt:Media><tt:XAddr>http://H/onvif/ptz_service</tt:XAddr></tt:Media>"
                       "<tt:PTZ><tt:XAddr>http://H/onvif/deviceio_service</tt:XAddr></tt:PTZ>"
                       "<tt:Extension><tt:DeviceIO><tt:XAddr>http://192.168.1.33:5000/onvif/device_service"
                       "</tt:XAddr><tt:AudioOutputs>0</tt:AudioOutputs><tt:RelayOutputs>0</tt:RelayOutputs>"
                       "</tt:DeviceIO></tt:Extension></tds:Capabilities></tds:GetCapabilitiesResponse>",
    "GetServices": "<tds:GetServicesResponse/>",
    "GetProfiles": "<trt:GetProfilesResponse><trt:Profiles token=\"IPCProfilesToken0\"><tt:Name>MainStream</tt:Name>"
                   "<tt:VideoSourceConfiguration><tt:SourceToken>VS0</tt:SourceToken></tt:VideoSourceConfiguration>"
                   "<tt:VideoEncoderConfiguration><tt:Encoding>H264</tt:Encoding><tt:Resolution><tt:Width>1280"
                   "</tt:Width><tt:Height>720</tt:Height></tt:Resolution></tt:VideoEncoderConfiguration>"
                   "<tt:AudioEncoderConfiguration><tt:Encoding>G711</tt:Encoding></tt:AudioEncoderConfiguration>"
                   "<tt:PTZConfiguration token=\"PTZConfigToken\"/></trt:Profiles></trt:GetProfilesResponse>",
    "GetStreamUri": "<trt:GetStreamUriResponse><trt:MediaUri><tt:Uri>rtsp://192.168.1.33:554/onvif1</tt:Uri>"
                    "</trt:MediaUri></trt:GetStreamUriResponse>",
    "GetSnapshotUri": "<trt:GetSnapshotUriResponse/>",
    "GetSystemDateAndTime": "<tds:GetSystemDateAndTimeResponse/>",
    "GetNodes": "<tptz:GetNodesResponse><tptz:PTZNode token=\"N\"><tt:SupportedPTZSpaces>"
                "<tt:ContinuousPanTiltVelocitySpace/></tt:SupportedPTZSpaces></tptz:PTZNode></tptz:GetNodesResponse>",
    "GetPresets": "<tptz:GetPresetsResponse><tptz:Preset token=\"1\"><tt:Name>Portao</tt:Name></tptz:Preset>"
                  "</tptz:GetPresetsResponse>",
    "ContinuousMove": "<tptz:ContinuousMoveResponse/>",
    "Stop": "<tptz:StopResponse/>",
    "GetImagingSettings": "<timg:GetImagingSettingsResponse><timg:ImagingSettings><tt:Brightness>50</tt:Brightness>"
                          "<tt:IrCutFilter>AUTO</tt:IrCutFilter></timg:ImagingSettings></timg:GetImagingSettingsResponse>",
    "GetEventProperties": "<tev:GetEventPropertiesResponse/>",
}


def _wsse_valid(body: str) -> bool:
    user = re.search(r"<wsse:Username>([^<]*)<", body)
    digest = re.search(r"PasswordDigest\">([^<]*)<", body)
    nonce = re.search(r"Base64Binary\">([^<]*)<", body)
    created = re.search(r"<wsu:Created>([^<]*)<", body)
    if not (user and digest and nonce and created) or user.group(1) != "admin":
        return False
    expected = base64.b64encode(hashlib.sha1(
        base64.b64decode(nonce.group(1)) + created.group(1).encode() + PASSWORD.encode()).digest()).decode()
    return expected == digest.group(1)


class FakeOnvif(BaseHTTPRequestHandler):
    calls: list[tuple[str, bool]] = []
    stop_supported = True

    def log_message(self, *_args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
        action = re.search(r"<s:Body><\w+:(\w+)", body).group(1)
        authed = _wsse_valid(body)
        FakeOnvif.calls.append((action, authed))
        if action not in PUBLIC and not authed:
            # Yoosee behaviour: close the connection without any reply.
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
            return
        if action == "Stop" and not FakeOnvif.stop_supported:
            payload = ('<s:Fault><s:Code><s:Value>s:Sender</s:Value></s:Code><s:Reason><s:Text>'
                       'not implemented</s:Text></s:Reason></s:Fault>')
            status = 500
        else:
            payload, status = RESPONSES.get(action, f"<x:{action}Response xmlns:x=\"urn:x\"/>"), 200
        xml = ('<?xml version="1.0"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
               'xmlns:tds="http://www.onvif.org/ver10/device/wsdl" xmlns:trt="http://www.onvif.org/ver10/media/wsdl" '
               'xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema" '
               'xmlns:timg="http://www.onvif.org/ver20/imaging/wsdl" xmlns:tev="http://www.onvif.org/ver10/events/wsdl">'
               f"<s:Body>{payload}</s:Body></s:Envelope>").encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/soap+xml")
        self.send_header("Content-Length", str(len(xml)))
        self.end_headers()
        self.wfile.write(xml)


@pytest.fixture
def onvif_server():
    FakeOnvif.calls = []
    FakeOnvif.stop_supported = True
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeOnvif)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()


def test_onvif_public_and_protected(onvif_server):
    anon = OnvifClient("127.0.0.1", onvif_server)
    assert anon.get_device_information()["Model"] == "IPC"
    with pytest.raises(OnvifDenied):
        anon.call(anon.device_url, "<tptz:GetNodes/>", auth=False)
    bad = OnvifClient("127.0.0.1", onvif_server, "admin", "wrong")
    with pytest.raises(OnvifAuthError):
        bad.call(bad.device_url, "<tptz:GetNodes/>", auth=True)
    good = OnvifClient("127.0.0.1", onvif_server, "admin", PASSWORD)
    good.call(good.device_url, "<tptz:GetNodes/>", auth=True)


def test_onvif_rewrites_advertised_hosts(onvif_server):
    client = OnvifClient("127.0.0.1", onvif_server)
    services = client.get_capabilities()
    assert services.deviceio.startswith(f"http://127.0.0.1:{onvif_server}/")  # stale 192.168.1.33 replaced
    assert services.relay_outputs == 0 and services.audio_outputs == 0
    uri = client.get_stream_uri(client.get_profiles()[0].token)
    assert uri == "rtsp://127.0.0.1:554/onvif1"


def test_capabilities_with_and_without_password(onvif_server):
    cfg = IPCameraConfig.new(name="Garagem", host="127.0.0.1", onvif_port=onvif_server, protocol="onvif",
                             scheme="http", main_path="/onvif1", username="admin")
    without = probe_capabilities(cfg, password="")
    assert without.ptz.state == NEEDS_AUTH
    assert without.relays.state == UNSUPPORTED and "RelayOutputs=0" in without.relays.detail
    assert without.talkback.state == UNSUPPORTED
    caps = probe_capabilities(cfg, password=PASSWORD)
    assert caps.ptz.state == CONFIRMED and caps.ptz_info.continuous and caps.ptz_info.presets
    assert caps.ircut.state == UNSUPPORTED  # no Imaging XAddr advertised → not assumed
    assert caps.light.state == UNSUPPORTED and caps.siren.state == UNSUPPORTED


def test_yoosee_identification_needs_several_signals():
    from core.ipcam.yoosee import looks_like_yoosee
    assert looks_like_yoosee(realm="HIipCamera", rtsp_methods=["USER_CMD_SET"])
    assert looks_like_yoosee(onvif_port=5000, manufacturer="Technology", model="IPC", stream_uri="/onvif1")
    assert not looks_like_yoosee(stream_uri="/onvif1")
    assert not looks_like_yoosee(manufacturer="Technology", model="IPC")


# ---------------------------------------------------------------- PTZ safety

def _ptz(onvif_server):
    client = OnvifClient("127.0.0.1", onvif_server, "admin", PASSWORD, timeout=2)
    client.services.ptz = client.device_url
    return PTZController(client, "IPCProfilesToken0")


def _actions():
    return [a for a, _ in FakeOnvif.calls]


def test_ptz_watchdog_stops_without_keepalive(onvif_server):
    ptz = _ptz(onvif_server)
    ptz.start_move(0.5, 0.0)
    time.sleep(3.0)  # no keepalive: watchdog must stop the motor
    assert "ContinuousMove" in _actions() and "Stop" in _actions()
    assert not ptz.moving
    ptz.close()


def test_ptz_keepalive_refreshes_then_stop(onvif_server):
    ptz = _ptz(onvif_server)
    ptz.start_move(0.0, 0.5)
    for _ in range(6):
        time.sleep(0.4)
        ptz.keepalive()
    assert _actions().count("ContinuousMove") >= 2   # refreshed before device Timeout
    assert "Stop" not in _actions()
    ptz.stop()
    time.sleep(0.5)
    assert _actions()[-1] == "Stop"
    ptz.close()


def test_ptz_stop_fallback_and_close(onvif_server):
    FakeOnvif.stop_supported = False
    ptz = _ptz(onvif_server)
    ptz.start_move(-0.5, 0.0)
    time.sleep(0.3)
    ptz.close()
    actions = _actions()
    # Stop rejected → zero-velocity ContinuousMove sent as the last command.
    assert actions[-2:] == ["Stop", "ContinuousMove"]


def test_ptz_presets_callback(onvif_server):
    ptz = _ptz(onvif_server)
    got = threading.Event()
    result = {}
    ptz.list_presets(lambda r, e: (result.update(r=r, e=e), got.set()))
    assert got.wait(3)
    assert result["r"] == [("1", "Portao")] and not result["e"]
    ptz.close()


# ---------------------------------------------------------------- RTSP probe

def _fake_rtsp(port_holder, password, ready):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port_holder.append(srv.getsockname()[1])
    ready.set()
    conn, _ = srv.accept()
    nonce = "abc123"
    buf = b""
    while True:
        data = conn.recv(4096)
        if not data:
            break
        buf += data
        while b"\r\n\r\n" in buf:
            req, buf = buf.split(b"\r\n\r\n", 1)
            text = req.decode()
            cseq = re.search(r"CSeq: (\d+)", text).group(1)
            if text.startswith("OPTIONS"):
                conn.sendall(f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\nPublic: OPTIONS, DESCRIBE, USER_CMD_SET\r\n\r\n".encode())
            elif "Authorization" not in text:
                conn.sendall(f'RTSP/1.0 401 Unauthorized\r\nCSeq: {cseq}\r\nWWW-Authenticate: Digest '
                             f'realm="HIipCamera",nonce="{nonce}"\r\n\r\n'.encode())
            else:
                uri = re.search(r'uri="([^"]+)"', text).group(1)
                resp = re.search(r'response="([^"]+)"', text).group(1)
                ha1 = hashlib.md5(f"admin:HIipCamera:{password}".encode()).hexdigest()
                ha2 = hashlib.md5(f"DESCRIBE:{uri}".encode()).hexdigest()
                if resp == hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest():
                    sdp = ("v=0\r\nm=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\na=control:track1\r\n"
                           "m=audio 0 RTP/AVP 8\r\na=control:track2\r\n")
                    conn.sendall(f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\nContent-Length: {len(sdp)}\r\n\r\n{sdp}".encode())
                else:
                    conn.sendall(f"RTSP/1.0 400 Bad Request\r\nCSeq: {cseq}\r\n\r\n".encode())
    conn.close()
    srv.close()


@pytest.mark.parametrize("password,status", [(PASSWORD, rtsp.OK), ("wrong", rtsp.AUTH_FAILED), ("", rtsp.AUTH_FAILED)])
def test_rtsp_probe_digest(password, status):
    port, ready = [], threading.Event()
    threading.Thread(target=_fake_rtsp, args=(port, PASSWORD, ready), daemon=True).start()
    ready.wait(2)
    result = rtsp.probe("127.0.0.1", port[0], "/onvif1", "admin", password, timeout=3)
    assert result.status == status
    assert result.realm == "HIipCamera" and "USER_CMD_SET" in result.methods
    if status == rtsp.OK:
        assert (result.video.codec, result.audio.codec, result.audio.clock_rate) == ("H264", "PCMA", 8000)


# ---------------------------------------------------------------- models/security

def test_legacy_url_migration_moves_password_out_of_settings():
    cfg = IPCameraConfig.from_dict({"name": "rtsp://admin:pw@10.0.0.9/onvif1",
                                    "url": "rtsp://admin:p%40ss@10.0.0.9:554/onvif1"})
    data = cfg.to_dict()
    assert "p@ss" not in repr(data) and "pw" not in data["name"]
    assert data["host"] == "10.0.0.9" and data["username"] == "admin" and data["main_path"] == "/onvif1"
    assert CredentialStore.instance().lookup(cfg.id) == "p@ss"
    assert not CredentialStore.instance().persistent  # keyring disabled in tests
    assert cfg.stream_url() == "rtsp://10.0.0.9:554/onvif1"


@pytest.mark.parametrize("host", ["a b", "x;rm -rf", "-bad", "", "http://x", "a" * 300])
def test_invalid_hosts(host):
    with pytest.raises(ValueError):
        validate_host(host)


def test_paths_and_redaction():
    assert validate_path("onvif1") == "/onvif1"
    with pytest.raises(ValueError):
        validate_path('/a" ! filesink location=/tmp/x')
    text = 'rtspsrc location="rtsp://u:p4ss@h/x" user-pw=p4ss password: p4ss'
    assert "p4ss" not in redact(text)


def test_targets_are_bounded():
    assert len(discovery.parse_targets("10.0.0.0/24")) == 254
    assert discovery.parse_targets("192.168.15.15") == ["192.168.15.15"]
    with pytest.raises(ValueError):
        discovery.parse_targets("10.0.0.0/16")


# ---------------------------------------------------------------- recording rules

def test_schedule_windows():
    windows = parse_schedule("08:00-18:00, 22:00-06:00, bogus")
    assert windows == [(480, 1080), (1320, 360)]
    assert in_schedule(windows, dt.datetime(2026, 1, 1, 9, 0))
    assert in_schedule(windows, dt.datetime(2026, 1, 1, 23, 30))
    assert in_schedule(windows, dt.datetime(2026, 1, 1, 5, 59))
    assert not in_schedule(windows, dt.datetime(2026, 1, 1, 19, 0))


def test_retention_only_touches_own_segments(tmp_path):
    cfg = IPCameraConfig.new(name="Garagem", host="10.0.0.9")
    cfg.recording.update(directory=str(tmp_path), retention_days=7)
    day = camera_dir(cfg) / "2026-01-01"
    day.mkdir(parents=True)
    old, new, other = day / "10-00-00.mkv", day / "11-00-00.mkv", day / "notes.txt"
    for f in (old, new, other):
        f.write_text("x")
    import os
    os.utime(old, (time.time() - 8 * 86400,) * 2)
    assert apply_retention(cfg) == 1
    assert not old.exists() and new.exists() and other.exists()
