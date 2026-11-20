"""Multi-camera wall (up to 2×2 per page) and the IP camera event history.

Each tile decodes the camera's *secondary* stream when one exists and renders
it with ``gtk4paintablesink`` (no Python per-frame work), so a wall of four
cameras costs roughly what one main stream costs.  Tiles reconnect on their
own with exponential backoff.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")

from gi.repository import Adw, GLib, Gst, Gtk

from core.ipcam.security import redact
from utils.i18n import _

log = logging.getLogger(__name__)
PAGE_SIZE = 4


class CameraTile(Gtk.Overlay):
    def __init__(self, backend, camera, on_open: Callable, on_toggle_zoom: Callable) -> None:
        super().__init__()
        self.add_css_class("ipcam-tile")
        self.camera = camera
        self._backend = backend
        self._pipeline = None
        self._bus_id = 0
        self._retry = 0
        self._retry_id = 0
        self._picture = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, hexpand=True, vexpand=True,
                                    can_shrink=True)
        self._picture.update_property([Gtk.AccessibleProperty.LABEL], [camera.name])
        self.set_child(self._picture)
        bar = Gtk.Box(spacing=6, valign=Gtk.Align.END, halign=Gtk.Align.FILL)
        bar.add_css_class("ipcam-tile-bar")
        self._dot = Gtk.Label(label="●")
        self._dot.add_css_class("ipcam-dot-off")
        name = Gtk.Label(label=camera.name, xalign=0, hexpand=True, ellipsize=3)
        self._state = Gtk.Label(label=_("Connecting…"))
        self._state.add_css_class("caption")
        open_btn = Gtk.Button(icon_name="view-fullscreen-symbolic", tooltip_text=_("Open in BigCam"))
        open_btn.add_css_class("flat")
        open_btn.add_css_class("circular")
        open_btn.connect("clicked", lambda *_: on_open(camera))
        for widget in (self._dot, name, self._state, open_btn):
            bar.append(widget)
        self.add_overlay(bar)
        click = Gtk.GestureClick()
        click.connect("pressed", lambda _g, n, _x, _y: n == 2 and on_toggle_zoom(self))
        self.add_controller(click)
        self.set_focusable(True)

    def start(self) -> None:
        self.stop()
        cfg = self._backend.config_for(self.camera)
        stream = "sub" if cfg and cfg.sub_path else "main"
        try:
            pipe = Gst.parse_launch(
                f"{self._backend.get_gst_source(self.camera, audio=False, stream=stream)} ! "
                "queue max-size-buffers=2 leaky=downstream ! gtk4paintablesink name=sink sync=false")
        except GLib.Error as exc:
            self._set_state(_("Unavailable"), False)
            log.warning("Wall tile failed: %s", redact(exc))
            return
        self._backend.prepare_pipeline(pipe, self.camera)
        self._picture.set_paintable(pipe.get_by_name("sink").get_property("paintable"))
        bus = pipe.get_bus()
        bus.add_signal_watch()
        self._bus_id = bus.connect("message", self._on_message)
        self._pipeline = pipe
        pipe.set_state(Gst.State.PLAYING)

    def stop(self) -> None:
        if self._retry_id:
            GLib.source_remove(self._retry_id)
            self._retry_id = 0
        pipe, self._pipeline = self._pipeline, None
        if pipe is None:
            return
        bus = pipe.get_bus()
        if self._bus_id:
            bus.disconnect(self._bus_id)
            self._bus_id = 0
        bus.remove_signal_watch()
        pipe.set_state(Gst.State.NULL)

    def _on_message(self, _bus, msg) -> None:
        if msg.type == Gst.MessageType.STATE_CHANGED and msg.src == self._pipeline:
            _old, new, _pending = msg.parse_state_changed()
            if new == Gst.State.PLAYING:
                self._retry = 0
                self._set_state(_("Live"), True)
        elif msg.type in (Gst.MessageType.ERROR, Gst.MessageType.EOS):
            text = ""
            if msg.type == Gst.MessageType.ERROR:
                err, _dbg = msg.parse_error()
                text = err.message.lower()
            self.stop()
            if "unauthorized" in text or "401" in text:
                self._set_state(_("Wrong password"), False)
                return
            self._retry += 1
            delay = min(30, 2 ** min(self._retry, 5))
            self._set_state(_("Reconnecting in {s}s").format(s=delay), False)
            self._retry_id = GLib.timeout_add_seconds(delay, self._retry_start)

    def _retry_start(self) -> bool:
        self._retry_id = 0
        self.start()
        return GLib.SOURCE_REMOVE

    def _set_state(self, text: str, live: bool) -> None:
        self._state.set_label(text)
        self._dot.remove_css_class("ipcam-dot-on" if not live else "ipcam-dot-off")
        self._dot.add_css_class("ipcam-dot-on" if live else "ipcam-dot-off")


class IPCameraWall(Adw.Window):
    def __init__(self, parent: Gtk.Window, backend, cameras: list, on_open: Callable) -> None:
        super().__init__(title=_("IP Camera Wall"), default_width=1100, default_height=680,
                         transient_for=parent)
        self._backend = backend
        self._on_open = on_open
        self._cameras = sorted(cameras, key=lambda c: (not getattr(backend.config_for(c), "favorite", False),
                                                       getattr(backend.config_for(c), "location", ""), c.name))
        self._page = 0
        self._tiles: list[CameraTile] = []
        self._zoomed: CameraTile | None = None
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self._prev = Gtk.Button(icon_name="go-previous-symbolic", tooltip_text=_("Previous cameras"))
        self._next = Gtk.Button(icon_name="go-next-symbolic", tooltip_text=_("Next cameras"))
        self._prev.connect("clicked", lambda *_: self._show_page(self._page - 1))
        self._next.connect("clicked", lambda *_: self._show_page(self._page + 1))
        header.pack_start(self._prev)
        header.pack_start(self._next)
        view.add_top_bar(header)
        self._grid = Gtk.Grid(row_homogeneous=True, column_homogeneous=True, row_spacing=6,
                              column_spacing=6, margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
        if not self._cameras:
            view.set_content(Adw.StatusPage(icon_name="network-server-symbolic", title=_("No IP cameras"),
                                            description=_("Add a camera from the main menu.")))
        else:
            view.set_content(self._grid)
        self.set_content(view)
        self.connect("close-request", self._on_close)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)
        self._show_page(0)

    def _on_key(self, _c, keyval, _code, _state) -> bool:
        from gi.repository import Gdk
        if keyval == Gdk.KEY_Escape and self._zoomed is not None:
            self._toggle_zoom(self._zoomed)
            return True
        return False

    def _show_page(self, page: int) -> None:
        pages = max(1, (len(self._cameras) + PAGE_SIZE - 1) // PAGE_SIZE)
        self._page = max(0, min(page, pages - 1))
        self._prev.set_visible(pages > 1)
        self._next.set_visible(pages > 1)
        self._prev.set_sensitive(self._page > 0)
        self._next.set_sensitive(self._page < pages - 1)
        for tile in self._tiles:
            tile.stop()
            self._grid.remove(tile)
        self._tiles.clear()
        self._zoomed = None
        subset = self._cameras[self._page * PAGE_SIZE:(self._page + 1) * PAGE_SIZE]
        cols = 1 if len(subset) == 1 else 2
        for index, camera in enumerate(subset):
            tile = CameraTile(self._backend, camera, self._open, self._toggle_zoom)
            self._grid.attach(tile, index % cols, index // cols, 1, 1)
            self._tiles.append(tile)
            tile.start()

    def _toggle_zoom(self, tile: CameraTile) -> None:
        zoom = self._zoomed is None
        self._zoomed = tile if zoom else None
        for other in self._tiles:
            if other is not tile:
                other.set_visible(not zoom)
                # Hidden tiles stop decoding to save CPU.
                other.stop() if zoom else other.start()

    def _open(self, camera) -> None:
        self._on_open(camera)

    def _on_close(self, *_args) -> bool:
        for tile in self._tiles:
            tile.stop()
        return False


class IPEventsDialog(Adw.Dialog):
    """Event history with a camera/kind filter; data from core.ipcam.events.EventLog."""

    def __init__(self, event_log, cameras: dict[str, str], camera_id: str = "") -> None:
        super().__init__(title=_("IP Camera Events"), content_width=560, content_height=620)
        self._log = event_log
        self._ids = [""] + list(cameras)
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        clear = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text=_("Clear history"))
        clear.connect("clicked", lambda *_: self._confirm_clear())
        header.pack_end(clear)
        view.add_top_bar(header)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=12,
                      margin_start=12, margin_end=12)
        filters = Adw.PreferencesGroup()
        self._camera = Adw.ComboRow(title=_("Camera"), model=Gtk.StringList.new(
            [_("All cameras")] + [cameras[i] for i in self._ids[1:]]))
        if camera_id in self._ids:
            self._camera.set_selected(self._ids.index(camera_id))
        self._kind = Adw.ComboRow(title=_("Type"), model=Gtk.StringList.new(
            [_("All"), _("Motion"), _("Recording errors"), _("Other")]))
        for row in (self._camera, self._kind):
            row.connect("notify::selected", lambda *_: self._reload())
            filters.add(row)
        box.append(filters)
        self._list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self._list.add_css_class("boxed-list")
        self._empty = Adw.StatusPage(icon_name="security-medium-symbolic", title=_("No events"))
        box.append(self._list)
        box.append(self._empty)
        view.set_content(Gtk.ScrolledWindow(child=Adw.Clamp(child=box), vexpand=True))
        self.set_child(view)
        self._reload()

    def _reload(self) -> None:
        self._list.remove_all()
        cam_id = self._ids[self._camera.get_selected()]
        kinds = [None, {"motion"}, {"recording-error"}, {"event", "tamper", "video-loss"}][self._kind.get_selected()]
        events = self._log.read(limit=300, camera_id=cam_id, kinds=kinds)
        source_label = {"native": _("camera"), "local": _("BigCam detection"), "system": _("system")}
        kind_label = {"motion": _("Motion"), "recording-error": _("Recording error"),
                      "tamper": _("Tampering"), "video-loss": _("Video lost"), "event": _("Event")}
        for ev in events:
            when = dt.datetime.fromtimestamp(float(ev.get("time", 0))).strftime("%d/%m/%Y %H:%M:%S")
            title = f"{kind_label.get(ev.get('kind'), ev.get('kind', ''))} — {ev.get('camera_name', '')}"
            sub = f"{when} · {source_label.get(ev.get('source'), ev.get('source', ''))}"
            if ev.get("detail"):
                sub += f" · {ev['detail']}"
            row = Adw.ActionRow(title=GLib.markup_escape_text(title), subtitle=GLib.markup_escape_text(sub))
            icon = "dialog-warning-symbolic" if ev.get("kind") == "recording-error" else "camera-video-symbolic"
            row.add_prefix(Gtk.Image(icon_name=icon))
            self._list.append(row)
        self._list.set_visible(bool(events))
        self._empty.set_visible(not events)

    def _confirm_clear(self) -> None:
        dialog = Adw.AlertDialog(heading=_("Clear event history?"),
                                 body=_("Recordings are not affected."))
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("clear", _("Clear"))
        dialog.set_response_appearance("clear", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", lambda _d, r: r == "clear" and (self._log.clear(), self._reload()))
        dialog.present(self)
