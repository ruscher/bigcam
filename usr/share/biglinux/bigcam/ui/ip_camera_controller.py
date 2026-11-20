"""Glue between the main window and the IP camera subsystem.

Owns persistence (settings without secrets, passwords in the keyring), the
add/edit/remove flows, the background monitor service, desktop
notifications, the camera wall and the event history.  Keeps ``window.py``
changes small.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib

from constants import BackendType
from core.backends.ip_backend import IPBackend
from core.ipcam.models import IPCameraConfig
from core.ipcam.monitor import IPMonitorService
from core.ipcam.recorder import camera_dir
from core.ipcam.security import CredentialStore
from utils.async_worker import run_async
from utils.i18n import _

if TYPE_CHECKING:
    from ui.window import BigCamWindow

log = logging.getLogger(__name__)


class IPCameraController:
    def __init__(self, window: "BigCamWindow") -> None:
        self.win = window
        self.backend: IPBackend = window._camera_manager.get_backend(BackendType.IP)
        self.monitors = IPMonitorService(self.backend, self._on_event, self._on_recording, self._on_status)
        self._configs: list[IPCameraConfig] = []
        self._wall = None
        self._caps_task = None

    # ---------------------------------------------------------------- persistence

    def load(self) -> None:
        """Read settings, migrating legacy credential URLs (keyring I/O off the UI thread)."""
        entries = self.win._settings.get("ip_cameras") or []

        def work():
            configs, migrated = [], False
            for entry in entries:
                try:
                    if isinstance(entry, dict) and "url" in entry and "host" not in entry:
                        migrated = True
                    configs.append(IPCameraConfig.from_dict(dict(entry)))
                except (TypeError, ValueError, KeyError):
                    log.warning("Ignoring an invalid IP camera entry")
                    migrated = True
            return configs, migrated

        def done(result):
            configs, migrated = result
            self._configs = configs
            if migrated:
                # Rewrites the file without the credential-bearing URLs.
                self._persist()
                log.info("Migrated %d IP camera entr(y/ies) to the secure format", len(configs))
            self._apply()
            self._refresh_capabilities_background()

        run_async(work, on_success=done, on_error=lambda exc: log.warning("IP camera load failed: %s", exc))

    def _persist(self) -> None:
        self.win._settings.set("ip_cameras", [cfg.to_dict() for cfg in self._configs])

    def _apply(self) -> None:
        ordered = sorted(self._configs, key=lambda c: (not c.favorite, c.location.lower(), c.name.lower()))
        self.win._camera_manager.add_ip_cameras([c.to_dict() for c in ordered])
        # add_ip_cameras rebuilt the backend registry: use its config objects from now on.
        self._configs = [self.backend.config_for(c.id) or c for c in ordered]
        self.monitors.reconfigure(self.cameras())

    def cameras(self) -> list:
        return [c for c in self.win._camera_manager.cameras if c.backend == BackendType.IP]

    def _refresh_capabilities_background(self) -> None:
        """Probe devices that need capabilities for background features (events)."""
        def work():
            for camera in self.cameras():
                cfg = self.backend.config_for(camera)
                if cfg and cfg.onvif_port and cfg.events.get("native") and \
                        self.backend.capabilities_for(camera) is None:
                    self.backend.refresh_capabilities(camera)
            return True
        self._caps_task = run_async(work, on_success=lambda _r: self.monitors.reconfigure(self.cameras()))

    # ---------------------------------------------------------------- flows

    def add(self) -> None:
        from ui.ip_camera_dialog import IPCameraDialog
        dialog = IPCameraDialog()
        dialog.connect("camera-saved", lambda _d, cfg: self._saved(cfg, new=True))
        self.win._immersion.present_dialog(dialog, self.win)

    def edit(self, cfg: IPCameraConfig) -> None:
        from ui.ip_camera_dialog import IPCameraDialog
        dialog = IPCameraDialog(cfg)
        dialog.connect("camera-saved", lambda _d, new_cfg: self._saved(new_cfg, new=False))
        self.win._immersion.present_dialog(dialog, self.win)

    def _saved(self, cfg: IPCameraConfig, new: bool) -> None:
        self._configs = [c for c in self._configs if c.id != cfg.id] + [cfg]
        self.backend._caps.pop(cfg.id, None)  # force a fresh probe with the new settings
        self._persist()
        self._apply()
        if not CredentialStore.instance().persistent:
            self.toast(_("No keyring available: the camera password is kept only until BigCam closes."))
        self.toast(_("Camera “{name}” saved").format(name=cfg.name))
        if new or (self.win._active_camera and self.win._active_camera.id == cfg.id):
            self.select(cfg.id, restart=True)

    def remove(self, cfg: IPCameraConfig) -> None:
        active = self.win._active_camera
        if active and active.id == cfg.id:
            self.win._stream_engine.stop()
            self.win._active_camera = None
        self._configs = [c for c in self._configs if c.id != cfg.id]
        self._persist()
        run_async(lambda: CredentialStore.instance().delete(cfg.id))
        self._apply()
        self.toast(_("Camera “{name}” removed").format(name=cfg.name))

    def config_changed(self, cfg: IPCameraConfig, restart: bool) -> None:
        self._persist()
        self.monitors.reconfigure(self.cameras())
        if restart and self.win._active_camera and self.win._active_camera.id == cfg.id:
            self.select(cfg.id, restart=True)

    def select(self, camera_id: str, restart: bool = False) -> None:
        def go() -> bool:
            for index, camera in enumerate(self.win._camera_manager.cameras):
                if camera.id == camera_id:
                    if restart and self.win._active_camera and self.win._active_camera.id == camera_id:
                        self.win._stream_engine.stop()
                        self.win._active_camera = None
                    self.win._camera_selector.set_selected_silent(index)
                    self.win._on_camera_selected(self.win._camera_selector, camera)
                    break
            return GLib.SOURCE_REMOVE
        GLib.idle_add(go)

    def make_panel(self, camera):
        from ui.ip_camera_panel import IPCameraPanel
        if self.backend.config_for(camera) is None:
            return None
        return IPCameraPanel(self.backend, camera, self.win._stream_engine,
                             on_config_changed=self.config_changed, on_edit=self.edit,
                             on_remove=self.remove, on_show_events=self.show_events,
                             on_show_wall=self.show_wall)

    def show_wall(self, *_args) -> None:
        from ui.ip_camera_wall import IPCameraWall
        if self._wall is not None:
            self._wall.present()
            return
        self._wall = IPCameraWall(self.win, self.backend, self.cameras(),
                                  on_open=lambda cam: (self.select(cam.id), self.win.present()))
        self._wall.connect("close-request", lambda *_: setattr(self, "_wall", None) or False)
        self._wall.present()

    def show_events(self, cfg: IPCameraConfig | None = None) -> None:
        from ui.ip_camera_wall import IPEventsDialog
        names = {c.id: c.name for c in self._configs}
        dialog = IPEventsDialog(self.monitors.log, names, cfg.id if cfg else "")
        self.win._immersion.present_dialog(dialog, self.win)

    # ---------------------------------------------------------------- monitor callbacks

    def _on_event(self, cfg: IPCameraConfig, event: dict) -> None:
        if cfg.events.get("snapshot", True) and event.get("kind") == "motion":
            camera = next((c for c in self.cameras() if c.id == cfg.id), None)
            if camera is not None:
                folder = camera_dir(cfg) / "events" / dt.datetime.now().strftime("%Y-%m-%d")
                path = folder / (dt.datetime.now().strftime("%H-%M-%S") + ".jpg")

                def snap():
                    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
                    return self.backend.capture_photo(camera, str(path))
                run_async(snap)
        if not cfg.events.get("notify", True):
            return
        app = self.win.get_application()
        if app is None:
            return
        kind = {"motion": _("Motion detected"), "tamper": _("Camera tampering"),
                "video-loss": _("Video lost")}.get(event.get("kind"), _("Camera event"))
        origin = _("detected by BigCam") if event.get("source") == "local" else _("reported by the camera")
        note = Gio.Notification.new(f"{kind}: {cfg.name}")
        note.set_body(f"{dt.datetime.now().strftime('%H:%M:%S')} · {origin}")
        note.set_priority(Gio.NotificationPriority.NORMAL)
        note.set_default_action("app.activate") if app.lookup_action("activate") else None
        # Replaces the previous notification of the same camera instead of stacking.
        app.send_notification(f"ipcam-{cfg.id}", note)

    def _on_recording(self, cfg: IPCameraConfig, state: str, detail: str) -> None:
        if state == "recording":
            self.toast(_("Recording started: {name}").format(name=cfg.name))
        elif state == "error":
            msg = {"low-disk": _("Recording stopped for {name}: less than 1 GB free on disk."),
                   "stream": _("Recording error for {name}: the stream was interrupted."),
                   }.get(detail, _("Recording error for {name}."))
            self.toast(msg.format(name=cfg.name))

    def _on_status(self, cfg: IPCameraConfig, what: str, state: str) -> None:
        log.debug("IP monitor %s %s: %s", cfg.id, what, state)

    # ---------------------------------------------------------------- misc

    @property
    def surveillance_active(self) -> list[str]:
        return [m.cfg.name for m in self.monitors._monitors.values()]

    def toast(self, text: str) -> None:
        self.win._toast_overlay.add_toast(Adw.Toast(title=text, timeout=4))

    def shutdown(self) -> None:
        if self._caps_task is not None:
            self._caps_task.cancel()
        if self._wall is not None:
            self._wall.close()
        self.monitors.shutdown()
