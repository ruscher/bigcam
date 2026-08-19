"""Simple video stabilizer and denoiser using OpenCV.

This module provides a lightweight frame-to-frame stabilizer based on
feature tracking (LK optical flow) and smoothed affine transforms.
It is designed to be inexpensive and to run in real-time on consumer
hardware. When `cv2` is not available the class is a noop.
"""

from __future__ import annotations

import collections
import logging
from typing import Deque, Optional

try:
    import cv2
    import numpy as np

    _HAS_CV2 = True
except Exception:
    cv2 = None  # type: ignore
    np = None  # type: ignore
    _HAS_CV2 = False

log = logging.getLogger(__name__)


class VideoStabilizer:
    """Frame stabilizer using LK optical-flow and moving-average smoothing.

    Usage:
        stab = VideoStabilizer(smoothing=10)
        out = stab.stabilize(frame)
    """

    def __init__(self, smoothing: int = 8, max_corners: int = 200) -> None:
        if not _HAS_CV2:
            log.info("OpenCV not available — VideoStabilizer disabled")
            self.enabled = False
            return
        self.enabled = True
        self._smoothing = max(1, smoothing)
        self._max_corners = max_corners
        self._prev_gray = None
        self._transforms: Deque[np.ndarray] = collections.deque(maxlen=self._smoothing)

    def reset(self) -> None:
        if not self.enabled:
            return
        self._prev_gray = None
        self._transforms.clear()

    def denoise(self, frame: "np.ndarray") -> "np.ndarray":
        if not self.enabled:
            return frame
        # Fast non-local means denoising for color images
        return cv2.fastNlMeansDenoisingColored(frame, None, 3, 3, 7, 21)

    def _estimate_transform(
        self, prev_gray: "np.ndarray", gray: "np.ndarray"
    ) -> "np.ndarray":
        # Feature detection + LK optical flow
        prev_pts = cv2.goodFeaturesToTrack(
            prev_gray,
            mask=None,
            maxCorners=self._max_corners,
            qualityLevel=0.01,
            minDistance=8,
        )
        if prev_pts is None or len(prev_pts) < 6:
            return np.eye(3, dtype=np.float32)
        next_pts, status, _ = cv2.calcOpticalFlowPyrLK(prev_gray, gray, prev_pts, None)
        if next_pts is None:
            return np.eye(3, dtype=np.float32)
        # Filter good points
        good_prev = prev_pts[status.flatten() == 1]
        good_next = next_pts[status.flatten() == 1]
        if len(good_prev) < 6:
            return np.eye(3, dtype=np.float32)
        m, inliers = cv2.estimateAffinePartial2D(
            good_prev, good_next, method=cv2.RANSAC, ransacReprojThreshold=3
        )
        if m is None:
            return np.eye(3, dtype=np.float32)
        # Convert 2x3 affine to 3x3 matrix
        M = np.eye(3, dtype=np.float32)
        M[:2, :] = m
        return M

    def _smooth_transform(self, M: "np.ndarray") -> "np.ndarray":
        self._transforms.append(M)
        # Average transforms in log-domain (approx) by averaging matrices
        avg = sum(self._transforms) / len(self._transforms)
        return avg

    def stabilize(self, frame: "np.ndarray") -> "np.ndarray":
        """Denoise + stabilize the input BGR frame and return processed frame."""
        if not self.enabled:
            return frame
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        if self._prev_gray is None:
            self._prev_gray = gray
            # First frame — only denoise
            return self.denoise(frame)
        try:
            M = self._estimate_transform(self._prev_gray, gray)
            M_s = self._smooth_transform(M)
            h, w = frame.shape[:2]
            stabilized = cv2.warpAffine(
                frame,
                M_s[:2, :],
                (w, h),
                flags=cv2.INTER_LINEAR + cv2.WARP_INVERSE_MAP,
                borderMode=cv2.BORDER_REFLECT,
            )
            den = self.denoise(stabilized)
            self._prev_gray = gray
            return den
        except Exception:
            log.debug("Stabilizer failed for a frame", exc_info=True)
            self._prev_gray = gray
            return self.denoise(frame)
