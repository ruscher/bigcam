"""Persistent IP camera configuration (never contains passwords)."""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from urllib.parse import unquote, urlsplit, urlunsplit

from core.ipcam.security import CredentialStore, validate_host, validate_path, validate_port

PROTOCOLS = ("onvif", "rtsp", "http")
TRANSPORTS = ("tcp", "udp", "auto")
STREAMS = ("main", "sub")


@dataclass
class IPCameraConfig:
    id: str = ""
    name: str = ""
    host: str = ""
    protocol: str = "onvif"          # onvif | rtsp | http
    rtsp_port: int = 554
    onvif_port: int = 0               # 0 = no ONVIF
    http_port: int = 80
    username: str = ""
    main_path: str = "/"
    sub_path: str = ""
    scheme: str = "rtsp"              # rtsp | rtsps | http | https
    stream: str = "main"              # main | sub
    transport: str = "tcp"            # tcp | udp | auto
    latency_ms: int = 200
    audio: bool = True
    muted: bool = True
    vendor: str = ""                  # "yoosee", "generic", ...
    manufacturer: str = ""
    model: str = ""
    firmware: str = ""
    location: str = ""
    favorite: bool = False
    onvif_profile_main: str = ""
    onvif_profile_sub: str = ""
    events: dict = field(default_factory=lambda: {
        "notify": True, "native": True, "local_motion": False,
        "sensitivity": 50, "cooldown_s": 60, "snapshot": True})
    recording: dict = field(default_factory=lambda: {
        "mode": "off", "segment_min": 10, "retention_days": 7,
        "schedule": "", "post_roll_s": 15, "directory": ""})

    # -- construction -------------------------------------------------------

    @classmethod
    def new(cls, **kwargs) -> "IPCameraConfig":
        cfg = cls(**kwargs)
        cfg.id = cfg.id or f"ip:{uuid.uuid4().hex[:16]}"
        return cfg.validated()

    def validated(self) -> "IPCameraConfig":
        self.host = validate_host(self.host)
        self.rtsp_port = validate_port(self.rtsp_port)
        self.http_port = validate_port(self.http_port)
        self.onvif_port = int(self.onvif_port) and validate_port(self.onvif_port)
        self.main_path = validate_path(self.main_path)
        self.sub_path = validate_path(self.sub_path) if self.sub_path else ""
        if self.protocol not in PROTOCOLS:
            raise ValueError("Unknown protocol")
        if self.scheme not in ("rtsp", "rtsps", "http", "https"):
            raise ValueError("Unknown URL scheme")
        if self.stream not in STREAMS:
            self.stream = "main"
        if self.transport not in TRANSPORTS:
            self.transport = "tcp"
        if any(c in self.username for c in ":@/\r\n\t") or len(self.username) > 128:
            raise ValueError("Invalid user name")
        self.latency_ms = max(0, min(5000, int(self.latency_ms)))
        self.name = (self.name or self.host).strip()[:80]
        self.location = (self.location or "").strip()[:80]
        return self

    @classmethod
    def from_dict(cls, data: dict) -> "IPCameraConfig":
        if "url" in data and "host" not in data:
            return cls.from_legacy(data)
        known = {f for f in cls.__dataclass_fields__}
        cfg = cls(**{k: v for k, v in data.items() if k in known})
        defaults = cls()
        cfg.events = {**defaults.events, **(cfg.events or {})}
        cfg.recording = {**defaults.recording, **(cfg.recording or {})}
        if not cfg.id:
            cfg.id = f"ip:{uuid.uuid4().hex[:16]}"
        return cfg.validated()

    @classmethod
    def from_url(cls, url: str, name: str = "", camera_id: str = "") -> tuple["IPCameraConfig", str]:
        """Build a config from a URL. Returns (config, password-from-url)."""
        parts = urlsplit(url.strip())
        scheme = parts.scheme.lower()
        if scheme not in ("rtsp", "rtsps", "http", "https") or not parts.hostname:
            raise ValueError("Use an HTTP(S) or RTSP(S) URL with a host")
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        is_rtsp = scheme.startswith("rtsp")
        default_port = {"rtsp": 554, "rtsps": 322, "http": 80, "https": 443}[scheme]
        cfg = cls(
            id=camera_id or f"ip:{uuid.uuid4().hex[:16]}", name=name, host=parts.hostname,
            protocol="rtsp" if is_rtsp else "http", scheme=scheme,
            rtsp_port=(parts.port or default_port) if is_rtsp else 554,
            http_port=(parts.port or default_port) if not is_rtsp else 80,
            username=unquote(parts.username or ""), main_path=path,
        )
        return cfg.validated(), unquote(parts.password or "")

    @classmethod
    def from_legacy(cls, data: dict) -> "IPCameraConfig":
        """Migrate the old ``{"name", "url"}`` entry, moving the password to the keyring."""
        cfg, password = cls.from_url(str(data.get("url", "")), str(data.get("name", "")))
        if "://" in cfg.name:
            cfg.name = cfg.host
        if password:
            CredentialStore.instance().store(cfg.id, password, cfg.name)
        return cfg

    def to_dict(self) -> dict:
        return asdict(self)

    # -- derived ------------------------------------------------------------

    @property
    def active_path(self) -> str:
        if self.stream == "sub" and self.sub_path:
            return self.sub_path
        return self.main_path

    def stream_url(self, stream: str | None = None) -> str:
        """Public URL (no credentials) for the selected stream."""
        path = self.sub_path if (stream or self.stream) == "sub" and self.sub_path else self.main_path
        host = f"[{self.host}]" if ":" in self.host else self.host
        port = self.rtsp_port if self.scheme.startswith("rtsp") else self.http_port
        path_only, _, query = path.partition("?")
        return urlunsplit((self.scheme, f"{host}:{port}", path_only, query, ""))

    def password(self) -> str:
        return CredentialStore.instance().lookup(self.id) or ""

    @property
    def is_rtsp(self) -> bool:
        return self.scheme.startswith("rtsp")
