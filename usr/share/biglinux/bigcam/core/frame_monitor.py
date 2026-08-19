"""Frame integrity and performance monitoring for virtual camera pipelines.

This lightweight monitor tracks:
- instantaneous FPS
- frame jitter / delivery latency
- dropped frames (when timestamps jump ahead)
- low-level integrity checks for arbitrary frame payloads
- rolling history for troubleshooting
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, Iterable, Optional

log = logging.getLogger(__name__)


@dataclass
class FrameSample:
    timestamp_ns: int
    width: int
    height: int
    frame_hash: str
    dropped: bool = False
    latency_ms: float = 0.0


@dataclass
class FrameMetrics:
    fps: float = 0.0
    avg_latency_ms: float = 0.0
    dropped_frames: int = 0
    total_frames: int = 0
    last_width: int = 0
    last_height: int = 0
    healthy: bool = True
    note: str = ""


class FrameMonitor:
    """Track each emitted frame for integrity and performance."""

    def __init__(self, window_seconds: float = 5.0, max_samples: int = 240) -> None:
        self._lock = threading.Lock()
        self._samples: Deque[FrameSample] = deque(maxlen=max_samples)
        self._window_seconds = max(1.0, float(window_seconds))
        self._dropped = 0
        self._total = 0
        self._last_ts_ns: Optional[int] = None
        self._last_wall_ns: Optional[int] = None
        self._start_wall_ns = time.monotonic_ns()

    @staticmethod
    def _hash_frame(payload: bytes) -> str:
        return hashlib.sha256(payload).hexdigest()[:16]

    def record(
        self,
        payload: bytes,
        width: int,
        height: int,
        timestamp_ns: Optional[int] = None,
    ) -> FrameMetrics:
        """Record a frame payload and return current metrics snapshot."""
        if timestamp_ns is None:
            timestamp_ns = time.monotonic_ns()

        now_ns = time.monotonic_ns()
        dropped = False
        if self._last_ts_ns is not None and timestamp_ns > self._last_ts_ns:
            delta = timestamp_ns - self._last_ts_ns
            if delta > 1_000_000_000 // 30:
                dropped = True
                self._dropped += 1

        sample = FrameSample(
            timestamp_ns=timestamp_ns,
            width=width,
            height=height,
            frame_hash=self._hash_frame(payload),
            dropped=dropped,
            latency_ms=(
                0.0
                if self._last_wall_ns is None
                else (now_ns - self._last_wall_ns) / 1_000_000.0
            ),
        )

        with self._lock:
            self._samples.append(sample)
            self._total += 1
            self._last_ts_ns = timestamp_ns
            self._last_wall_ns = now_ns

            cutoff = now_ns - int(self._window_seconds * 1_000_000_000)
            recent = [s for s in self._samples if s.timestamp_ns >= cutoff]
            if recent:
                fps = len(recent) / self._window_seconds
                avg_latency = sum(s.latency_ms for s in recent) / len(recent)
            else:
                fps = 0.0
                avg_latency = 0.0

            healthy = True
            note = "ok"
            if self._dropped:
                healthy = False
                note = "dropped frames observed"
            if avg_latency > 80.0:
                healthy = False
                note = "high latency"
            if not recent:
                healthy = False
                note = "no recent frames"

            return FrameMetrics(
                fps=fps,
                avg_latency_ms=avg_latency,
                dropped_frames=self._dropped,
                total_frames=self._total,
                last_width=width,
                last_height=height,
                healthy=healthy,
                note=note,
            )

    def snapshot(self) -> FrameMetrics:
        with self._lock:
            now_ns = time.monotonic_ns()
            cutoff = now_ns - int(self._window_seconds * 1_000_000_000)
            recent = [s for s in self._samples if s.timestamp_ns >= cutoff]
            if not recent:
                return FrameMetrics(healthy=False, note="no recent frames")
            fps = len(recent) / self._window_seconds
            avg_latency = sum(s.latency_ms for s in recent) / len(recent)
            return FrameMetrics(
                fps=fps,
                avg_latency_ms=avg_latency,
                dropped_frames=self._dropped,
                total_frames=self._total,
                last_width=recent[-1].width,
                last_height=recent[-1].height,
                healthy=(self._dropped == 0 and avg_latency < 80.0),
                note=(
                    "ok"
                    if self._dropped == 0 and avg_latency < 80.0
                    else "frame instability"
                ),
            )

    def reset(self) -> None:
        with self._lock:
            self._samples.clear()
            self._dropped = 0
            self._total = 0
            self._last_ts_ns = None
            self._last_wall_ns = None

    def __len__(self) -> int:
        return len(self._samples)
