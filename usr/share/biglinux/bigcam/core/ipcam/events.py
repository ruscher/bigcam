"""Camera events: native ONVIF events, local motion detection and the event log.

* ``OnvifEventListener`` – ONVIF PullPoint subscription (native camera events).
* ``MotionDetector`` – software detection on a low-rate grayscale substream,
  always labelled ``source="local"`` so it is never confused with camera or
  vendor-cloud alerts.
* ``EventLog`` – append-only JSON-lines history with size rotation.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable

import numpy as np

from core.ipcam.onvif import OnvifClient, OnvifError

log = logging.getLogger(__name__)

MOTION_TOPICS = ("motion", "cellmotion", "motionalarm", "tamper", "videoloss", "alarm")


def data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    path = Path(base) / "bigcam"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


class EventLog:
    MAX_LINES = 5000

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or data_dir() / "ipcam-events.jsonl"
        self._lock = threading.Lock()

    def append(self, event: dict) -> None:
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            try:
                if self.path.stat().st_size > 2 * 1024 * 1024:
                    self._rotate()
            except OSError:
                pass

    def _rotate(self) -> None:
        lines = self.path.read_text(encoding="utf-8").splitlines()[-self.MAX_LINES // 2:]
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def read(self, limit: int = 500, camera_id: str = "", kinds: set[str] | None = None) -> list[dict]:
        with self._lock:
            try:
                lines = self.path.read_text(encoding="utf-8").splitlines()
            except OSError:
                return []
        events = []
        for line in reversed(lines):
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if camera_id and ev.get("camera_id") != camera_id:
                continue
            if kinds and ev.get("kind") not in kinds:
                continue
            events.append(ev)
            if len(events) >= limit:
                break
        return events

    def clear(self) -> None:
        with self._lock:
            try:
                self.path.unlink()
            except OSError:
                pass


class MotionDetector:
    """Frame-difference detector for small grayscale frames (e.g. 320×180 @ 2 fps)."""

    def __init__(self, sensitivity: int = 50) -> None:
        self.set_sensitivity(sensitivity)
        self._background: np.ndarray | None = None
        self._active_frames = 0

    def set_sensitivity(self, sensitivity: int) -> None:
        s = max(1, min(100, int(sensitivity)))
        self.pixel_threshold = int(45 - s * 0.3)            # 15..45 gray levels
        self.area_ratio = 0.02 * (1.0 - s / 100.0) + 0.002   # 0.2%..2.2% of the image

    def feed(self, gray: np.ndarray) -> float:
        """Return the changed-area ratio when motion is detected, else 0."""
        frame = gray.astype(np.int16)
        if self._background is None or self._background.shape != frame.shape:
            self._background = frame
            return 0.0
        diff = np.abs(frame - self._background)
        # Slow adaptation to lighting changes.
        self._background = (self._background * 0.7 + frame * 0.3).astype(np.int16)
        mask = diff > self.pixel_threshold
        # Remove isolated noisy pixels (cheap 2x2 erosion).
        mask = mask[:-1, :-1] & mask[1:, :-1] & mask[:-1, 1:] & mask[1:, 1:]
        ratio = float(mask.mean())
        if ratio >= self.area_ratio:
            self._active_frames += 1
        else:
            self._active_frames = 0
        # Require two consecutive frames to filter compression glitches.
        return ratio if self._active_frames >= 2 else 0.0


class OnvifEventListener:
    """PullPoint loop on a worker thread; renews the subscription as needed."""

    def __init__(self, client: OnvifClient, on_event: Callable[[dict], None],
                 on_status: Callable[[str], None] | None = None) -> None:
        self._client = client
        self._on_event = on_event
        self._on_status = on_status or (lambda _s: None)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="bigcam-onvif-events", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=self._client.timeout + 6)

    def _run(self) -> None:
        backoff = 2.0
        while not self._stop.is_set():
            address = ""
            try:
                address = self._client.events_create_pullpoint(termination_s=120)
                self._on_status("subscribed")
                backoff = 2.0
                renew_at = time.monotonic() + 100
                while not self._stop.is_set() and time.monotonic() < renew_at:
                    for raw in self._client.events_pull(address, timeout_s=5, limit=20):
                        event = _classify(raw)
                        if event:
                            self._on_event(event)
            except OnvifError as exc:
                self._on_status(f"error:{type(exc).__name__}")
                self._stop.wait(backoff)
                backoff = min(60.0, backoff * 2)
            finally:
                if address:
                    self._client.events_unsubscribe(address)


def _classify(raw: dict) -> dict | None:
    topic = raw.get("topic", "")
    low = topic.lower().replace("/", "")
    if not any(key in low for key in MOTION_TOPICS):
        return {"kind": "event", "detail": topic[-80:], "source": "native"}
    values = {k.lower(): str(v).lower() for k, v in raw.get("data", {}).items()}
    active = any(v in ("true", "1") for v in values.values()) if values else True
    if not active:
        return None   # end-of-motion notifications are not alerts
    kind = "tamper" if "tamper" in low else "video-loss" if "videoloss" in low else "motion"
    return {"kind": kind, "detail": topic[-80:], "source": "native"}
