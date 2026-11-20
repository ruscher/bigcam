"""Lightweight RTSP probe (OPTIONS/DESCRIBE with Basic/Digest) and SDP parser.

Used by the connection test and to translate failures into precise messages
(wrong password, unknown path, unreachable host) before GStreamer is started.
Never logs or returns credentials.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import socket
import ssl
import time
from dataclasses import dataclass, field

OK = "ok"
AUTH_REQUIRED = "auth-required"
AUTH_FAILED = "auth-failed"
NOT_FOUND = "not-found"
UNREACHABLE = "unreachable"
REFUSED = "refused"
TIMEOUT = "timeout"
PROTOCOL = "protocol-error"


@dataclass
class SdpMedia:
    kind: str           # video / audio / application
    codec: str          # H264, H265, PCMA, PCMU, MPEG4-GENERIC...
    clock_rate: int = 0
    channels: int = 1
    payload: int = -1
    control: str = ""


@dataclass
class RtspProbeResult:
    status: str
    code: int = 0
    realm: str = ""
    auth_scheme: str = ""
    server: str = ""
    methods: list[str] = field(default_factory=list)
    media: list[SdpMedia] = field(default_factory=list)
    elapsed_ms: int = 0
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == OK

    @property
    def video(self) -> SdpMedia | None:
        return next((m for m in self.media if m.kind == "video"), None)

    @property
    def audio(self) -> SdpMedia | None:
        return next((m for m in self.media if m.kind == "audio"), None)


def parse_sdp(text: str) -> list[SdpMedia]:
    media: list[SdpMedia] = []
    current: SdpMedia | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("m="):
            fields = line[2:].split()
            kind = fields[0] if fields else ""
            payload = int(fields[3]) if len(fields) > 3 and fields[3].isdigit() else -1
            static = {0: ("PCMU", 8000), 8: ("PCMA", 8000), 26: ("JPEG", 90000),
                      14: ("MPA", 90000)}.get(payload, ("", 0))
            current = SdpMedia(kind=kind, codec=static[0], clock_rate=static[1], payload=payload)
            media.append(current)
        elif current is not None and line.startswith("a=rtpmap:"):
            match = re.match(r"a=rtpmap:(\d+)\s+([^/\s]+)/(\d+)(?:/(\d+))?", line)
            if match and int(match.group(1)) == current.payload:
                current.codec = match.group(2).upper()
                current.clock_rate = int(match.group(3))
                current.channels = int(match.group(4) or 1)
        elif current is not None and line.startswith("a=control:"):
            current.control = line[len("a=control:"):]
    return media


def _auth_header(challenge: str, method: str, uri: str, user: str, password: str) -> str:
    scheme, _, rest = challenge.partition(" ")
    if scheme.lower() == "basic":
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        return f"Basic {token}"
    params = dict(re.findall(r'(\w+)="?([^",]*)"?', rest))
    realm, nonce = params.get("realm", ""), params.get("nonce", "")
    algorithm = params.get("algorithm", "MD5").upper()
    hasher = hashlib.sha256 if algorithm.startswith("SHA-256") else hashlib.md5
    ha1 = hasher(f"{user}:{realm}:{password}".encode()).hexdigest()
    ha2 = hasher(f"{method}:{uri}".encode()).hexdigest()
    qop = "auth" if "auth" in params.get("qop", "").split(",") else ""
    header = f'Digest username="{user}", realm="{realm}", nonce="{nonce}", uri="{uri}"'
    if qop:
        cnonce = os.urandom(8).hex()
        response = hasher(f"{ha1}:{nonce}:00000001:{cnonce}:{qop}:{ha2}".encode()).hexdigest()
        header += f', qop={qop}, nc=00000001, cnonce="{cnonce}"'
    else:
        response = hasher(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
    header += f', response="{response}"'
    if "opaque" in params:
        header += f', opaque="{params["opaque"]}"'
    if algorithm != "MD5":
        header += f", algorithm={algorithm}"
    return header


def _read_response(sock: socket.socket, deadline: float) -> tuple[int, dict[str, str], str]:
    data = b""
    while b"\r\n\r\n" not in data:
        sock.settimeout(max(0.1, deadline - time.monotonic()))
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
        if len(data) > 65536:
            raise ValueError("RTSP header too large")
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode("latin-1", "replace").split("\r\n")
    match = re.match(r"RTSP/1\.\d\s+(\d{3})", lines[0] if lines else "")
    if not match:
        raise ValueError("Not an RTSP response")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        key, _, value = line.partition(":")
        key = key.strip().lower()
        # Keep the first Digest challenge if several are offered.
        if key == "www-authenticate" and key in headers and headers[key].lower().startswith("digest"):
            continue
        headers[key] = value.strip()
    length = int(headers.get("content-length", "0") or 0)
    while len(body) < min(length, 65536):
        sock.settimeout(max(0.1, deadline - time.monotonic()))
        chunk = sock.recv(4096)
        if not chunk:
            break
        body += chunk
    return int(match.group(1)), headers, body.decode("utf-8", "replace")


def probe(host: str, port: int, path: str, username: str = "", password: str = "",
          timeout: float = 5.0, tls: bool = False) -> RtspProbeResult:
    """Run OPTIONS + DESCRIBE (authenticating when challenged)."""
    start = time.monotonic()
    deadline = start + timeout
    netloc = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    uri = f"rtsp{'s' if tls else ''}://{netloc}{path or '/'}"
    result = RtspProbeResult(status=PROTOCOL)
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except socket.timeout:
        return RtspProbeResult(TIMEOUT, detail="connect timeout")
    except ConnectionRefusedError:
        return RtspProbeResult(REFUSED, detail="connection refused")
    except OSError as exc:
        return RtspProbeResult(UNREACHABLE, detail=type(exc).__name__)
    try:
        if tls:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE  # cameras use self-signed certificates
            sock = ctx.wrap_socket(sock, server_hostname=host)
        cseq = 1

        def send(method: str, extra: str = "") -> tuple[int, dict[str, str], str]:
            nonlocal cseq
            request = (f"{method} {uri} RTSP/1.0\r\nCSeq: {cseq}\r\n"
                       f"User-Agent: BigCam\r\n{extra}\r\n")
            cseq += 1
            sock.sendall(request.encode("utf-8"))
            return _read_response(sock, deadline)

        code, headers, _ = send("OPTIONS")
        result.server = headers.get("server", "")[:80]
        result.methods = [m.strip() for m in headers.get("public", "").split(",") if m.strip()]
        accept = "Accept: application/sdp\r\n"
        code, headers, body = send("DESCRIBE", accept)
        challenge = headers.get("www-authenticate", "")
        if code == 401 and challenge:
            result.auth_scheme = challenge.split(" ", 1)[0]
            match = re.search(r'realm="([^"]*)"', challenge)
            result.realm = match.group(1)[:80] if match else ""
            if not username:
                result.status, result.code = AUTH_REQUIRED, code
                return result
            auth = _auth_header(challenge, "DESCRIBE", uri, username, password)
            code, headers, body = send("DESCRIBE", accept + f"Authorization: {auth}\r\n")
            if code in (401, 403):
                result.status, result.code = AUTH_FAILED, code
                return result
            if code == 400:
                # Observed on Yoosee/HiSilicon firmware: a well-formed Digest
                # answer with the wrong password is answered with 400.
                result.status, result.code = AUTH_FAILED, code
                result.detail = "credentials rejected (HTTP 400 after Digest)"
                return result
        result.code = code
        if code == 200:
            result.status = OK
            result.media = parse_sdp(body)
        elif code == 404:
            result.status = NOT_FOUND
        elif code in (401, 403):
            result.status = AUTH_REQUIRED
        else:
            result.status = PROTOCOL
            result.detail = f"RTSP {code}"
        return result
    except socket.timeout:
        result.status, result.detail = TIMEOUT, "response timeout"
        return result
    except (OSError, ValueError, ssl.SSLError) as exc:
        result.status, result.detail = PROTOCOL, type(exc).__name__
        return result
    finally:
        result.elapsed_ms = int((time.monotonic() - start) * 1000)
        try:
            sock.close()
        except OSError:
            pass
