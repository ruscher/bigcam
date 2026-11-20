"""Find IP cameras on the local network.

Two complementary strategies:

1. ONVIF WS-Discovery (UDP multicast 239.255.255.250:3702).
2. A bounded unicast probe of the machine's own private subnets (at most one
   /24 per interface) on well-known camera ports.  This finds devices whose
   firmware ignores WS-Discovery – the Yoosee camera used to develop this
   module is one of them.

The scan only touches RFC 1918 / CGNAT / link-local networks the computer is
directly attached to, uses short timeouts and never tries credentials.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import socket
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlsplit

from core.ipcam import rtsp, yoosee
from core.ipcam.onvif import OnvifClient, OnvifError

log = logging.getLogger(__name__)

ONVIF_PORTS = (80, 5000, 8000, 8080, 8899, 2020)
RTSP_PORTS = (554, 8554)
HTTP_PORTS = (80, 8080)
MAX_HOSTS = 1024

_PROBE = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" '
    'xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
    'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
    'xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
    "<e:Header><w:MessageID>uuid:{mid}</w:MessageID>"
    '<w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>'
    '<w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe'
    "</w:Action></e:Header><e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types>"
    "</d:Probe></e:Body></e:Envelope>"
)


@dataclass
class DiscoveredCamera:
    host: str
    name: str = ""
    manufacturer: str = ""
    model: str = ""
    firmware: str = ""
    onvif_port: int = 0
    rtsp_port: int = 0
    http_port: int = 0
    rtsp_realm: str = ""
    auth_required: bool = False
    main_path: str = ""
    sub_path: str = ""
    vendor: str = "generic"
    sources: set[str] = field(default_factory=set)

    @property
    def title(self) -> str:
        if self.vendor == "yoosee":
            return "Yoosee"
        label = " ".join(x for x in (self.manufacturer, self.model) if x)
        return label or self.name or self.host


def local_networks(max_prefix: int = 24) -> list[ipaddress.IPv4Network]:
    """Private IPv4 networks of this machine, each narrowed to at most /``max_prefix``."""
    nets: list[ipaddress.IPv4Network] = []
    try:
        out = subprocess.run(["ip", "-j", "-4", "addr", "show", "up"], capture_output=True,
                             text=True, timeout=3, check=False).stdout
        for iface in json.loads(out or "[]"):
            if iface.get("ifname") == "lo" or "LOOPBACK" in iface.get("flags", []):
                continue
            for addr in iface.get("addr_info", []):
                if addr.get("family") != "inet":
                    continue
                prefix = max(int(addr.get("prefixlen", 32)), max_prefix)
                if prefix >= 31:
                    continue
                net = ipaddress.ip_network(f"{addr['local']}/{prefix}", strict=False)
                if net.is_private and net not in nets:
                    nets.append(net)
    except (OSError, ValueError, subprocess.SubprocessError, KeyError):
        log.debug("Could not enumerate interfaces", exc_info=True)
    return nets


def parse_targets(text: str) -> list[str]:
    """Parse user input such as '192.168.15.15', '192.168.15.0/24' or 'cam.lan'."""
    hosts: list[str] = []
    for token in re.split(r"[\s,;]+", text.strip()):
        if not token:
            continue
        try:
            net = ipaddress.ip_network(token, strict=False)
            if net.num_addresses > MAX_HOSTS:
                raise ValueError("Network too large (maximum /22)")
            hosts.extend(str(h) for h in (net.hosts() if net.num_addresses > 1 else [net.network_address]))
        except ValueError as exc:
            if "too large" in str(exc):
                raise
            from core.ipcam.security import validate_host
            hosts.append(validate_host(token))
    return hosts[:MAX_HOSTS]


def _port_open(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def ws_discovery(timeout: float = 2.5) -> dict[str, DiscoveredCamera]:
    found: dict[str, DiscoveredCamera] = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.settimeout(0.3)
        sock.sendto(_PROBE.format(mid=uuid.uuid4()).encode(), ("239.255.255.250", 3702))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            text = data.decode("utf-8", "replace")
            for xaddr in re.findall(r"XAddrs>([^<]+)<", text):
                for url in xaddr.split():
                    parts = urlsplit(url)
                    if parts.hostname:
                        cam = found.setdefault(addr[0], DiscoveredCamera(host=addr[0]))
                        cam.onvif_port = parts.port or 80
                        cam.sources.add("ws-discovery")
            scopes = " ".join(re.findall(r"Scopes>([^<]+)<", text))
            name = re.search(r"onvif://www\.onvif\.org/name/(\S+)", scopes)
            if name and addr[0] in found:
                found[addr[0]].name = name.group(1).replace("%20", " ")[:60]
    except OSError:
        log.debug("WS-Discovery failed", exc_info=True)
    finally:
        sock.close()
    return found


def _identify(cam: DiscoveredCamera, timeout: float) -> DiscoveredCamera:
    if cam.onvif_port:
        try:
            client = OnvifClient(cam.host, cam.onvif_port, timeout=timeout)
            info = client.get_device_information()
            cam.manufacturer, cam.model = info.get("Manufacturer", ""), info.get("Model", "")
            cam.firmware = info.get("FirmwareVersion", "")
            try:
                profiles = client.get_profiles()
                if profiles:
                    cam.main_path = urlsplit(client.get_stream_uri(profiles[0].token)).path
                if len(profiles) > 1:
                    cam.sub_path = urlsplit(client.get_stream_uri(profiles[1].token)).path
            except OnvifError:
                pass
            cam.sources.add("onvif")
        except OnvifError:
            # Port open but not ONVIF (a web UI on port 80, for instance).
            if cam.onvif_port in HTTP_PORTS:
                cam.http_port = cam.onvif_port
            cam.onvif_port = 0
    if cam.rtsp_port:
        result = rtsp.probe(cam.host, cam.rtsp_port, cam.main_path or "/", timeout=timeout)
        if result.status in (rtsp.AUTH_REQUIRED, rtsp.OK, rtsp.NOT_FOUND):
            cam.rtsp_realm = result.realm
            cam.auth_required = result.status == rtsp.AUTH_REQUIRED
            cam.sources.add("rtsp")
            if yoosee.looks_like_yoosee(realm=result.realm, rtsp_methods=result.methods,
                                        onvif_port=cam.onvif_port, manufacturer=cam.manufacturer,
                                        model=cam.model, stream_uri=cam.main_path):
                cam.vendor = "yoosee"
                cam.main_path = cam.main_path or yoosee.YOOSEE_MAIN_PATH
                cam.sub_path = cam.sub_path or yoosee.YOOSEE_SUB_PATH
                cam.auth_required = True
        elif result.status == rtsp.PROTOCOL and not result.methods:
            cam.rtsp_port = 0
    return cam


def discover(targets: list[str] | None = None, progress: Callable[[float, str], None] | None = None,
             cancel: threading.Event | None = None, port_timeout: float = 0.5,
             workers: int = 64) -> list[DiscoveredCamera]:
    """Run WS-Discovery plus a bounded port probe. Blocking; call from a worker."""
    cancel = cancel or threading.Event()
    notify = progress or (lambda _f, _m: None)
    notify(0.02, "ws-discovery")
    found = ws_discovery()
    if targets is None:
        hosts = [str(h) for net in local_networks() for h in net.hosts()][:MAX_HOSTS]
    else:
        hosts = targets[:MAX_HOSTS]
    ports = sorted(set(ONVIF_PORTS + RTSP_PORTS))
    jobs = [(h, p) for h in hosts for p in ports]
    done = 0
    lock = threading.Lock()

    def check(job: tuple[str, int]) -> None:
        nonlocal done
        host, port = job
        if cancel.is_set():
            return
        if _port_open(host, port, port_timeout):
            with lock:
                cam = found.setdefault(host, DiscoveredCamera(host=host))
                if port in RTSP_PORTS and not cam.rtsp_port:
                    cam.rtsp_port = port
                elif port in ONVIF_PORTS and not cam.onvif_port:
                    cam.onvif_port = port
        with lock:
            done += 1
            if done % 64 == 0 or done == len(jobs):
                notify(0.05 + 0.75 * done / max(1, len(jobs)), "scan")

    if jobs:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="bigcam-scan") as pool:
            list(pool.map(check, jobs))
    if cancel.is_set():
        return []
    candidates = [c for c in found.values() if c.rtsp_port or c.onvif_port]
    notify(0.82, "identify")
    with ThreadPoolExecutor(max_workers=8, thread_name_prefix="bigcam-ident") as pool:
        identified = list(pool.map(lambda c: _identify(c, 3.0), candidates))
    notify(1.0, "done")
    result = [c for c in identified if c.rtsp_port or c.onvif_port]
    result.sort(key=lambda c: tuple(int(x) if x.isdigit() else 0 for x in c.host.split(".")))
    return result
