"""Credential storage, input validation and log redaction for IP cameras.

Passwords are stored in the desktop Secret Service (GNOME Keyring, KWallet
via ksecretd, KeePassXC…) through libsecret.  When no Secret Service is
reachable the password is kept only in memory for the running session and the
UI says so; it is never written to the settings file.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import threading

log = logging.getLogger(__name__)

_SCHEMA_NAME = "br.com.biglinux.bigcam.IPCamera"
_HOST_RE = re.compile(r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*\.?$")
_URL_CRED_RE = re.compile(r"((?:rtsps?|https?)://)([^/@\s]+)@", re.IGNORECASE)
_PARAM_SECRET_RE = re.compile(r"((?:user-pw|password|passwd|pwd|user_pw)\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|\S+)",
                              re.IGNORECASE)


def redact(text: object) -> str:
    """Remove credentials from URLs and key=value pairs before logging/displaying."""
    value = str(text)
    value = _URL_CRED_RE.sub(r"\1***@", value)
    return _PARAM_SECRET_RE.sub(r"\1***", value)


def validate_host(host: str) -> str:
    """Accept an IPv4/IPv6 literal or an RFC 1123 hostname; reject everything else."""
    host = (host or "").strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    if not _HOST_RE.match(host):
        raise ValueError("Invalid host name or IP address")
    return host.rstrip(".").lower()


def validate_port(port: object) -> int:
    value = int(port)
    if not 0 < value < 65536:
        raise ValueError("Invalid port")
    return value


def validate_path(path: str) -> str:
    path = (path or "").strip() or "/"
    if not path.startswith("/"):
        path = "/" + path
    if any(ord(c) < 32 or ord(c) == 127 or c in " \"'\\" for c in path) or len(path) > 512:
        raise ValueError("Invalid stream path")
    return path


class CredentialStore:
    """Thread-safe password store for camera ids (libsecret, memory fallback)."""

    _instance: "CredentialStore | None" = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._memory: dict[str, str] = {}
        self._secret = None
        self._schema = None
        self._checked = False

    @classmethod
    def instance(cls) -> "CredentialStore":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def _backend(self):
        if self._checked:
            return self._secret
        self._checked = True
        if os.environ.get("BIGCAM_DISABLE_KEYRING") == "1":
            self._secret = None  # tests and sandboxed runs: memory only
            return None
        try:
            import gi
            gi.require_version("Secret", "1")
            from gi.repository import Secret
            self._schema = Secret.Schema.new(
                _SCHEMA_NAME, Secret.SchemaFlags.NONE,
                {"camera-id": Secret.SchemaAttributeType.STRING})
            self._secret = Secret
        except (ImportError, ValueError):
            log.warning("libsecret is unavailable; IP camera passwords are kept for this session only")
            self._secret = None
        return self._secret

    @property
    def persistent(self) -> bool:
        return self._backend() is not None

    def store(self, camera_id: str, password: str, label: str = "") -> bool:
        """Store a password. Returns True when persisted in the Secret Service."""
        with self._lock:
            self._memory[camera_id] = password
        secret = self._backend()
        if secret is None:
            return False
        try:
            return bool(secret.password_store_sync(
                self._schema, {"camera-id": camera_id}, secret.COLLECTION_DEFAULT,
                f"BigCam: {label or camera_id}", password, None))
        except Exception as exc:  # GLib.Error: locked/absent keyring
            log.warning("Could not save camera password in the keyring (%s)", type(exc).__name__)
            return False

    def lookup(self, camera_id: str) -> str | None:
        with self._lock:
            if camera_id in self._memory:
                return self._memory[camera_id]
        secret = self._backend()
        if secret is None:
            return None
        try:
            value = secret.password_lookup_sync(self._schema, {"camera-id": camera_id}, None)
        except Exception as exc:
            log.warning("Could not read camera password from the keyring (%s)", type(exc).__name__)
            return None
        if value is not None:
            with self._lock:
                self._memory[camera_id] = value
        return value

    def delete(self, camera_id: str) -> None:
        with self._lock:
            self._memory.pop(camera_id, None)
        secret = self._backend()
        if secret is None:
            return
        try:
            secret.password_clear_sync(self._schema, {"camera-id": camera_id}, None)
        except Exception as exc:
            log.warning("Could not remove camera password from the keyring (%s)", type(exc).__name__)
