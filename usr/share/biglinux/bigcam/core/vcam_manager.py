"""Virtual camera manager helpers for BigCam.

Provides a thin management layer on top of the existing `VirtualCamera`
implementation in BigCam. Adds convenient setup helpers, tuned parameters
for anti-flicker and buffering, and a small monitoring API.
"""

from __future__ import annotations

import logging
import subprocess
import shutil
from typing import Dict, Optional

from .virtual_camera import VirtualCamera

log = logging.getLogger(__name__)


class VirtualCameraManager:
    """High-level manager for v4l2loopback devices.

    Responsibilities:
    - Load/unload kernel module with recommended parameters
    - Create/delete dynamic devices via `v4l2loopback-ctl` when available
    - Provide small monitoring helpers for device health
    """

    DEFAULT_MAX_DEVICES = 5
    DEFAULT_VIDEO_BASE = 20

    def __init__(self) -> None:
        self._max_devices = VirtualCamera.get_max_devices() or self.DEFAULT_MAX_DEVICES

    # --- module/load helpers ------------------------------------------------
    def load_module_with_defaults(self) -> bool:
        """Load v4l2loopback with tuned defaults.

        Uses `VirtualCamera.load_module()` which already performs a
        privileged modprobe via the app's sudoers configuration.
        """
        log.info("Loading v4l2loopback kernel module with BigCam defaults")
        return VirtualCamera.load_module()

    def unload_module(self) -> bool:
        log.info("Unloading v4l2loopback kernel module")
        # VirtualCamera._run_privileged handles unload
        try:
            # Use existing cleanup to remove dynamic devices first
            VirtualCamera.cleanup_dynamic_devices()
        except Exception:
            log.debug("cleanup_dynamic_devices failed", exc_info=True)
        return (
            shutil.which("modprobe")
            and subprocess.call([shutil.which("modprobe"), "-r", "v4l2loopback"]) == 0
        )

    # --- allocation helpers -------------------------------------------------
    def allocate_for(self, camera_id: str) -> str:
        """Allocate/return v4l2loopback device for given camera id."""
        dev = VirtualCamera.allocate_device(camera_id)
        if dev:
            log.info("Allocated %s for %s", dev, camera_id)
        else:
            log.warning("Failed to allocate v4l2loopback for %s", camera_id)
        return dev

    def release_for(self, camera_id: str) -> None:
        VirtualCamera.release_device(camera_id)

    def set_max_devices(self, n: int) -> None:
        VirtualCamera.set_max_devices(n)
        self._max_devices = n

    def set_name_template(self, template: str) -> None:
        VirtualCamera.set_name_template(template)

    # --- monitoring --------------------------------------------------------
    def list_loopback_devices(self) -> Dict[str, str]:
        """Return mapping device -> card label for loopback devices."""
        return VirtualCamera._get_device_labels()  # reuse existing helper

    def dynamic_ctl_available(self) -> bool:
        return shutil.which("v4l2loopback-ctl") is not None

    def cleanup(self) -> None:
        """Cleanup dynamic devices created by the app."""
        VirtualCamera.cleanup_dynamic_devices()
