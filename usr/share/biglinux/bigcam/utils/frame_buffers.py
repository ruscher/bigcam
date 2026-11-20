"""Packed video layout and bounded latest-value handoff, independent of GTK."""
from __future__ import annotations
import threading
import numpy as np

try:
    import cv2 as _cv2
except ImportError:  # OpenCV is optional for this helper
    _cv2 = None


def bgr_from_bgra(data, width: int, height: int, stride: int, offset: int = 0) -> np.ndarray:
    if width <= 0 or height <= 0 or stride < width * 4 or offset < 0:
        raise ValueError("Invalid packed video layout")
    required = offset + (height - 1) * stride + width * 4
    if required > len(data):
        raise ValueError("Video buffer is shorter than its declared layout")
    view = np.ndarray((height, width, 4), dtype=np.uint8, buffer=data,
                      offset=offset, strides=(stride, 4, 1))
    if _cv2 is not None:
        try:
            # cvtColor allocates a new contiguous array (safe after unmap) and is
            # ~60x faster than a strided numpy gather on 720p frames.
            return _cv2.cvtColor(view, _cv2.COLOR_BGRA2BGR)
        except _cv2.error:
            pass
    # Copy before the GStreamer mapping is released; do not retain a borrowed view.
    return view[:, :, :3].copy()


class LatestValue:
    """At most one pending callback and one owned value, regardless of producer FPS."""
    def __init__(self):
        self._lock = threading.Lock()
        self._value = None
        self._scheduled = False

    def publish(self, value) -> bool:
        with self._lock:
            self._value = value
            if self._scheduled:
                return False
            self._scheduled = True
            return True

    def take(self):
        with self._lock:
            value, self._value = self._value, None
            self._scheduled = False
            return value

    def clear(self):
        # Keep the scheduled flag until the existing idle callback consumes it.
        # Otherwise a stop/start could schedule two callbacks for the same slot.
        with self._lock:
            self._value = None
