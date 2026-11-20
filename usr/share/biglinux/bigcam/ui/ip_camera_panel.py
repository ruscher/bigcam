"""Sidebar panel for network cameras: PTZ, stream, audio, alerts, recording, device.

Controls appear only for capabilities the device confirmed; anything else is
shown as a dimmed row explaining *why* it is unavailable (password needed,
not reported by the device, vendor-cloud only).
"""

from __future__ import annotations

import logging
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, GLib, Gtk

from core.ipcam.capabilities import CONFIRMED, NEEDS_AUTH, make_client
from core.ipcam.models import IPCameraConfig
from core.ipcam.ptz import PTZController
from core.ipcam.recorder import camera_dir
from utils.async_worker import run_async
from utils.i18n import _

log = logging.getLogger(__name__)

# (row, col, pan, tilt, label/icon, accessible name)
_PAD = [
    (0, 0, -1, 1, "↖", _("Move up and left")), (0, 1, 0, 1, "pan-up-symbolic", _("Move up")),
    (0, 2, 1, 1, "↗", _("Move up and right")),
    (1, 0, -1, 0, "pan-start-symbolic", _("Move left")), (1, 2, 1, 0, "pan-end-symbolic", _("Move right")),
    (2, 0, -1, -1, "↙", _("Move down and left")), (2, 1, 0, -1, "pan-down-symbolic", _("Move down")),
    (2, 2, 1, -1, "↘", _("Move down and right")),
]
_KEYS = {Gdk.KEY_Up: (0, 1), Gdk.KEY_Down: (0, -1), Gdk.KEY_Left: (-1, 0), Gdk.KEY_Right: (1, 0),
         Gdk.KEY_KP_8: (0, 1), Gdk.KEY_KP_2: (0, -1), Gdk.KEY_KP_4: (-1, 0), Gdk.KEY_KP_6: (1, 0),
         Gdk.KEY_KP_7: (-1, 1), Gdk.KEY_KP_9: (1, 1), Gdk.KEY_KP_1: (-1, -1), Gdk.KEY_KP_3: (1, -1)}
_REC_MODES = ["off", "continuous", "motion", "schedule"]


def _unavailable_row(title: str, state: str, detail: str = "") -> Adw.ActionRow:
    reason = {NEEDS_AUTH: _("Requires the camera password"),
              "unsupported": _("Not available on this camera"),
              "unknown": _("Not detected")}.get(state, state)
    if detail and state != NEEDS_AUTH:
        reason = f"{reason} — {detail}"
    row = Adw.ActionRow(title=title, subtitle=GLib.markup_escape_text(reason))
    row.add_css_class("dim-label")
    icon = "dialog-password-symbolic" if state == NEEDS_AUTH else "action-unavailable-symbolic"
    row.add_prefix(Gtk.Image(icon_name=icon))
    return row


class PTZPad(Gtk.Box):
    """Press-and-hold direction pad with guaranteed stop."""

    def __init__(self, controller: PTZController, zoom: bool) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10, halign=Gtk.Align.CENTER)
        self._ptz = controller
        self._held: tuple[float, float, float] | None = None
        self._keepalive_id = 0
        self._speed = 0.5
        self.add_css_class("ptz-pad")
        self.set_focusable(True)
        self.update_property([Gtk.AccessibleProperty.LABEL],
                             [_("Physical pan and tilt. Use the arrow keys while focused.")])
        grid = Gtk.Grid(row_spacing=6, column_spacing=6, halign=Gtk.Align.CENTER)
        grid.add_css_class("ptz-grid")
        for row, col, pan, tilt, icon, name in _PAD:
            btn = self._hold_button(icon, name, pan, tilt, 0)
            btn.add_css_class("ptz-diagonal" if pan and tilt else "ptz-direction")
            grid.attach(btn, col, row, 1, 1)
        stop = Gtk.Button(icon_name="media-playback-stop-symbolic", tooltip_text=_("Stop"))
        stop.add_css_class("circular")
        stop.add_css_class("ptz-stop")
        stop.update_property([Gtk.AccessibleProperty.LABEL], [_("Stop movement")])
        stop.connect("clicked", lambda *_: self.release())
        grid.attach(stop, 1, 1, 1, 1)
        self.append(grid)

        if zoom:
            zbox = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER)
            zbox.append(self._hold_button("zoom-out-symbolic", _("Optical zoom out"), 0, 0, -1))
            label = Gtk.Label(label=_("Optical zoom"))
            label.add_css_class("caption")
            zbox.append(label)
            zbox.append(self._hold_button("zoom-in-symbolic", _("Optical zoom in"), 0, 0, 1))
            self.append(zbox)

        speed_box = Gtk.Box(spacing=8)
        speed_label = Gtk.Label(label=_("Speed"))
        speed_label.add_css_class("caption")
        speed = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.1, 1.0, 0.05)
        speed.set_value(self._speed)
        speed.set_hexpand(True)
        speed.set_draw_value(False)
        speed.update_property([Gtk.AccessibleProperty.LABEL], [_("Movement speed")])
        speed.connect("value-changed", lambda s: setattr(self, "_speed", s.get_value()))
        speed_box.append(speed_label)
        speed_box.append(speed)
        self.append(speed_box)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key_pressed)
        keys.connect("key-released", self._on_key_released)
        self.add_controller(keys)
        focus = Gtk.EventControllerFocus()
        focus.connect("leave", lambda *_: self.release())
        self.add_controller(focus)
        self.connect("unmap", lambda *_: self.release())
        self.connect("map", self._on_map)

    def _on_map(self, *_args) -> None:
        root = self.get_root()
        if root is not None and not getattr(self, "_root_hooked", False):
            self._root_hooked = True
            root.connect("notify::is-active", lambda w, _p: None if w.is_active() else self.release())

    def _hold_button(self, icon: str, name: str, pan: float, tilt: float, zoom: float) -> Gtk.Button:
        if icon.endswith("-symbolic"):
            btn = Gtk.Button(icon_name=icon)
        else:
            btn = Gtk.Button(label=icon)
        btn.add_css_class("circular")
        btn.set_tooltip_text(name)
        btn.update_property([Gtk.AccessibleProperty.LABEL], [name])
        gesture = Gtk.GestureClick()
        gesture.connect("pressed", lambda *_: self.hold(pan, tilt, zoom))
        gesture.connect("released", lambda *_: self.release())
        gesture.connect("cancel", lambda *_: self.release())
        gesture.connect("unpaired-release", lambda *_: self.release())
        btn.add_controller(gesture)
        # Keyboard activation (Space/Enter) performs a short nudge.
        btn.connect("activate", lambda *_: self._nudge(pan, tilt, zoom))
        return btn

    def hold(self, pan: float, tilt: float, zoom: float = 0.0) -> None:
        vector = (pan * self._speed, tilt * self._speed, zoom * self._speed)
        if self._held == vector:
            return
        self._held = vector
        self._ptz.start_move(*vector)
        if not self._keepalive_id:
            self._keepalive_id = GLib.timeout_add(400, self._keepalive)

    def _keepalive(self) -> bool:
        if self._held is None:
            self._keepalive_id = 0
            return GLib.SOURCE_REMOVE
        self._ptz.keepalive()
        return GLib.SOURCE_CONTINUE

    def release(self) -> None:
        if self._keepalive_id:
            GLib.source_remove(self._keepalive_id)
            self._keepalive_id = 0
        if self._held is not None or self._ptz.moving:
            self._held = None
            self._ptz.stop()

    def _nudge(self, pan: float, tilt: float, zoom: float) -> None:
        self.hold(pan, tilt, zoom)
        GLib.timeout_add(350, lambda: (self.release(), False)[1])

    def _on_key_pressed(self, _ctl, keyval, _code, _state) -> bool:
        if keyval in _KEYS:
            self.hold(*_KEYS[keyval])
            return True
        if keyval in (Gdk.KEY_Escape, Gdk.KEY_space, Gdk.KEY_KP_5):
            self.release()
            return True
        return False

    def _on_key_released(self, _ctl, keyval, _code, _state) -> None:
        if keyval in _KEYS:
            self.release()


class IPCameraPanel(Gtk.Box):
    def __init__(self, backend, camera, engine,
                 on_config_changed: Callable[[IPCameraConfig, bool], None],
                 on_edit: Callable[[IPCameraConfig], None],
                 on_remove: Callable[[IPCameraConfig], None],
                 on_show_events: Callable[[IPCameraConfig | None], None],
                 on_show_wall: Callable[[], None]) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.add_css_class("ipcam-panel")
        self._backend, self._camera, self._engine = backend, camera, engine
        self._cfg: IPCameraConfig = backend.config_for(camera)
        self._changed, self._edit, self._remove = on_config_changed, on_edit, on_remove
        self._show_events, self._show_wall = on_show_events, on_show_wall
        self._ptz: PTZController | None = None
        self._pad: PTZPad | None = None
        self._task = None
        self._status_row = Adw.ActionRow(title=GLib.markup_escape_text(self._cfg.name),
                                         subtitle=_("Checking camera features…"))
        self._status_icon = Gtk.Image(icon_name="network-wired-symbolic")
        self._status_row.add_prefix(self._status_icon)
        header = Adw.PreferencesGroup()
        header.add(self._status_row)
        self.append(header)
        self._sections = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.append(self._sections)
        self._engine_handler = engine.connect("connection-status", self._on_connection_status)
        self.connect("destroy", lambda *_: self.dispose_panel())
        self.connect("unrealize", lambda *_: self.dispose_panel())
        caps = backend.capabilities_for(camera)
        if caps is not None:
            self._build(caps)
        else:
            self._sections.append(Gtk.Spinner(spinning=True, height_request=32))
            self._task = run_async(lambda: backend.refresh_capabilities(camera),
                                   on_success=self._build, on_error=lambda _e: self._build(None))

    # ------------------------------------------------------------ lifecycle

    def dispose_panel(self) -> None:
        if self._pad is not None:
            self._pad.release()
        if self._ptz is not None:
            ptz, self._ptz = self._ptz, None
            run_async(ptz.close)
        if self._task is not None:
            self._task.cancel()
        if self._engine_handler:
            self._engine.disconnect(self._engine_handler)
            self._engine_handler = 0

    def _on_connection_status(self, _engine, state: str, message: str) -> None:
        current = self._engine.current_camera
        if current is not None and current.id != self._camera.id:
            return
        texts = {"connecting": (_("Connecting…"), "content-loading-symbolic"),
                 "connected": (_("Connected"), "network-wired-symbolic"),
                 "reconnecting": (message or _("Reconnecting…"), "network-wired-acquiring-symbolic"),
                 "auth-failed": (message or _("Wrong password"), "dialog-password-symbolic"),
                 "not-found": (message or _("Stream not found"), "dialog-warning-symbolic"),
                 "failed": (message or _("Connection failed"), "network-wired-disconnected-symbolic")}
        text, icon = texts.get(state, (message, "network-wired-symbolic"))
        self._status_row.set_subtitle(GLib.markup_escape_text(text))
        self._status_icon.set_from_icon_name(icon)

    # ------------------------------------------------------------ build

    def _clear(self) -> None:
        child = self._sections.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._sections.remove(child)
            child = nxt

    def _build(self, caps) -> None:
        self._task = None
        self._clear()
        cfg = self._cfg
        if caps is None:
            self._status_row.set_subtitle(_("Could not reach the camera"))
            caps = None
        else:
            info = caps.device_info
            model = " ".join(x for x in (cfg.vendor.title() if cfg.vendor and cfg.vendor != "generic" else "",
                                         info.get("Model", "")) if x)
            self._status_row.set_subtitle(GLib.markup_escape_text(
                " · ".join(x for x in (cfg.host, model, caps.video_codec) if x)))
        self._sections.append(self._build_ptz(caps))
        self._sections.append(self._build_stream(caps))
        self._sections.append(self._build_light_alarm(caps))
        self._sections.append(self._build_events(caps))
        self._sections.append(self._build_recording())
        self._sections.append(self._build_device(caps))

    def _build_ptz(self, caps) -> Gtk.Widget:
        group = Adw.PreferencesGroup(
            title=_("Physical movement (PTZ)"),
            description=_("Moves the camera motors. Digital zoom and framing are in the preview toolbar."))
        if caps is None or caps.ptz.state != CONFIRMED or not caps.ptz_profile:
            state = caps.ptz.state if caps else "unknown"
            group.add(_unavailable_row(_("Pan and tilt"), state, caps.ptz.detail if caps else ""))
            return group
        client = make_client(self._cfg, timeout=4)
        # Service URL resolved during the (background) capability probe.
        client.services.ptz = caps.ptz_service or client.device_url
        self._ptz = PTZController(client, caps.ptz_profile,
                                  on_error=lambda e: GLib.idle_add(self._ptz_error, e))
        self._pad = PTZPad(self._ptz, caps.ptz_info.zoom)
        holder = Gtk.Box(margin_top=6, margin_bottom=6, halign=Gtk.Align.CENTER)
        holder.append(self._pad)
        group.add(holder)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.append(group)
        if caps.ptz_info.presets or caps.ptz_info.home:
            # Separate group: rows would otherwise be placed above the pad.
            presets = Adw.PreferencesGroup()
            presets.add(self._build_presets(caps))
            box.append(presets)
        return box

    def _ptz_error(self, error: str) -> bool:
        if error == "stop-failed":
            msg = _("The camera did not confirm the stop command. Check the camera.")
        elif "Auth" in error or "Denied" in error:
            msg = _("The camera refused the movement command (check the password).")
        else:
            msg = _("Movement command failed.")
        self._status_row.set_subtitle(msg)
        return GLib.SOURCE_REMOVE

    def _build_presets(self, caps) -> Gtk.Widget:
        expander = Adw.ExpanderRow(title=_("Saved positions"), subtitle=_("Presets stored in the camera"))
        self._preset_model = Gtk.StringList()
        self._preset_tokens: list[str] = []
        self._preset_row = Adw.ComboRow(title=_("Position"), model=self._preset_model)
        expander.add_row(self._preset_row)
        buttons = Gtk.Box(spacing=6, halign=Gtk.Align.END, margin_top=6, margin_bottom=6, margin_end=6)
        go = Gtk.Button(label=_("Go"))
        go.connect("clicked", lambda *_: self._goto_preset())
        save = Gtk.Button(label=_("Save current…"))
        save.connect("clicked", lambda *_: self._save_preset())
        remove = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text=_("Remove position"))
        remove.connect("clicked", lambda *_: self._remove_preset())
        for btn in (go, save, remove):
            buttons.append(btn)
        if caps.ptz_info.home:
            home = Gtk.Button(icon_name="go-home-symbolic", tooltip_text=_("Home position"))
            home.connect("clicked", lambda *_: self._ptz and self._ptz.goto_home())
            buttons.prepend(home)
        row = Adw.PreferencesRow(activatable=False)
        row.set_child(buttons)
        expander.add_row(row)
        self._refresh_presets()
        return expander

    def _refresh_presets(self) -> None:
        if self._ptz is None:
            return

        def done(result, error):
            GLib.idle_add(self._set_presets, result or [], error)
        self._ptz.list_presets(done)

    def _set_presets(self, presets, error) -> bool:
        self._preset_model.splice(0, self._preset_model.get_n_items(), [n for _t, n in presets])
        self._preset_tokens = [t for t, _n in presets]
        if error:
            self._preset_row.set_subtitle(_("Could not read positions"))
        return GLib.SOURCE_REMOVE

    def _goto_preset(self) -> None:
        index = self._preset_row.get_selected()
        if self._ptz and 0 <= index < len(self._preset_tokens):
            self._ptz.goto_preset(self._preset_tokens[index])

    def _save_preset(self) -> None:
        dialog = Adw.AlertDialog(heading=_("Save current position"),
                                 body=_("The camera stores this position under the name below."))
        entry = Gtk.Entry(placeholder_text=_("e.g. Gate"), max_length=32, activates_default=True)
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("save", _("Save"))
        dialog.set_default_response("save")
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)

        def response(_d, resp):
            name = entry.get_text().strip()
            if resp == "save" and name and self._ptz:
                self._ptz.set_preset(name, lambda _r, _e: GLib.idle_add(self._refresh_presets))
        dialog.connect("response", response)
        dialog.present(self)

    def _remove_preset(self) -> None:
        index = self._preset_row.get_selected()
        if not self._ptz or not (0 <= index < len(self._preset_tokens)):
            return
        dialog = Adw.AlertDialog(heading=_("Remove saved position?"),
                                 body=_("The position will be deleted from the camera."))
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("remove", _("Remove"))
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        token = self._preset_tokens[index]
        dialog.connect("response", lambda _d, r: r == "remove" and self._ptz and self._ptz.remove_preset(
            token, lambda _r, _e: GLib.idle_add(self._refresh_presets)))
        dialog.present(self)

    def _build_stream(self, caps) -> Gtk.Widget:
        cfg = self._cfg
        group = Adw.PreferencesGroup(title=_("Video and audio"))
        labels = [_("Main stream (best quality)")]
        if cfg.sub_path:
            labels.append(_("Secondary stream (lighter)"))
        if caps is not None and caps.profiles:
            for i, prof in enumerate(caps.profiles[:len(labels)]):
                if prof.width:
                    labels[i] += f" — {prof.width}×{prof.height}"
        quality = Adw.ComboRow(title=_("Quality"), model=Gtk.StringList.new(labels))
        quality.set_selected(1 if cfg.stream == "sub" and cfg.sub_path else 0)
        quality.connect("notify::selected", self._on_quality)
        group.add(quality)
        transport = Adw.ComboRow(title=_("Transport"), model=Gtk.StringList.new(
            [_("TCP (reliable)"), _("UDP (lower latency)"), _("Automatic")]))
        transport.set_selected(["tcp", "udp", "auto"].index(cfg.transport))
        transport.connect("notify::selected", lambda r, _p: self._set("transport", ["tcp", "udp", "auto"][r.get_selected()], True))
        group.add(transport)
        if caps is not None and caps.audio.state == CONFIRMED:
            audio = Adw.SwitchRow(title=_("Camera microphone"),
                                  subtitle=caps.audio_codec or _("Live audio from the camera"))
            audio.set_active(cfg.audio and not cfg.muted)
            audio.connect("notify::active", self._on_audio)
            group.add(audio)
        else:
            state = caps.audio.state if caps else "unknown"
            group.add(_unavailable_row(_("Camera microphone"), state, caps.audio.detail if caps else ""))
        return group

    def _on_quality(self, row, _pspec) -> None:
        self._set("stream", "sub" if row.get_selected() == 1 else "main", True)

    def _on_audio(self, row, _pspec) -> None:
        active = row.get_active()
        restart = active and not self._cfg.audio
        self._cfg.audio = self._cfg.audio or active
        self._cfg.muted = not active
        if not restart:
            self._engine.set_ip_muted(not active)
        self._changed(self._cfg, restart)

    def _set(self, key: str, value, restart: bool) -> None:
        if getattr(self._cfg, key) == value:
            return
        setattr(self._cfg, key, value)
        self._changed(self._cfg, restart)

    def _build_light_alarm(self, caps) -> Gtk.Widget:
        group = Adw.PreferencesGroup(title=_("Light, night vision and alarm"))
        if caps is not None and caps.ircut.state == CONFIRMED:
            row = Adw.ActionRow(title=_("Night vision"),
                                subtitle=_("Adjust it in the “Exposure” controls below (infrared filter)."))
            row.add_prefix(Gtk.Image(icon_name="weather-clear-night-symbolic"))
            group.add(row)
        else:
            group.add(_unavailable_row(_("Night vision / infrared"), caps.ircut.state if caps else "unknown",
                                       caps.ircut.detail if caps else ""))
        if caps is not None and caps.relays.state == CONFIRMED:
            for index, token in enumerate(caps.relay_tokens):
                group.add(self._relay_row(index, token))
        for key, title in (("light", _("White light")), ("siren", _("Siren / deterrence"))):
            cap = getattr(caps, key) if caps else None
            group.add(_unavailable_row(title, cap.state if cap else "unknown", cap.detail if cap else ""))
        if caps is not None:
            group.add(_unavailable_row(_("Talk-back (two-way audio)"), caps.talkback.state, caps.talkback.detail))
        return group

    def _relay_row(self, index: int, token: str) -> Gtk.Widget:
        row = Adw.ActionRow(title=_("Alarm output {n}").format(n=index + 1),
                            subtitle=_("Relay wired to a siren or light; pulses for 5 seconds"))
        btn = Gtk.Button(label=_("Trigger"), valign=Gtk.Align.CENTER)
        btn.add_css_class("destructive-action")

        def trigger(_b):
            dialog = Adw.AlertDialog(heading=_("Trigger alarm output?"),
                                     body=_("The output will be switched on for 5 seconds and then off."))
            dialog.add_response("cancel", _("Cancel"))
            dialog.add_response("go", _("Trigger"))
            dialog.set_response_appearance("go", Adw.ResponseAppearance.DESTRUCTIVE)

            def response(_d, resp):
                if resp != "go":
                    return
                client = make_client(self._cfg, timeout=4)

                def pulse():
                    client.set_relay_output_state(token, True)
                    try:
                        GLib.usleep(5_000_000)
                    finally:
                        client.set_relay_output_state(token, False)  # safety: always off
                btn.set_sensitive(False)
                run_async(pulse, on_success=lambda _r: btn.set_sensitive(True),
                          on_error=lambda _e: (btn.set_sensitive(True), self._status_row.set_subtitle(
                              _("The alarm output command failed."))))
            dialog.connect("response", response)
            dialog.present(self)
        btn.connect("clicked", trigger)
        row.add_suffix(btn)
        return row

    def _build_events(self, caps) -> Gtk.Widget:
        ev = self._cfg.events
        group = Adw.PreferencesGroup(title=_("Alerts"),
                                     description=_("Alerts are produced by BigCam on this computer, "
                                                   "not by the vendor's cloud."))
        if caps is not None and caps.events.state == CONFIRMED:
            native = Adw.SwitchRow(title=_("Camera events"), subtitle=_("Motion reported by the camera (ONVIF)"))
            native.set_active(bool(ev.get("native")))
            native.connect("notify::active", lambda r, _p: self._set_event("native", r.get_active()))
            group.add(native)
        else:
            group.add(_unavailable_row(_("Camera events"), caps.events.state if caps else "unknown",
                                       caps.events.detail if caps else ""))
        local = Adw.SwitchRow(title=_("Motion detection on this computer"),
                              subtitle=_("Analyses the lighter stream at 2 frames per second"))
        local.set_active(bool(ev.get("local_motion")))
        local.connect("notify::active", lambda r, _p: self._set_event("local_motion", r.get_active()))
        group.add(local)
        sens = Adw.SpinRow.new_with_range(1, 100, 5)
        sens.set_title(_("Sensitivity"))
        sens.set_value(int(ev.get("sensitivity", 50)))
        sens.connect("notify::value", lambda r, _p: self._set_event("sensitivity", int(r.get_value())))
        group.add(sens)
        notify = Adw.SwitchRow(title=_("Desktop notifications"))
        notify.set_active(bool(ev.get("notify", True)))
        notify.connect("notify::active", lambda r, _p: self._set_event("notify", r.get_active()))
        group.add(notify)
        cooldown = Adw.SpinRow.new_with_range(5, 3600, 5)
        cooldown.set_title(_("Minimum interval between alerts (s)"))
        cooldown.set_value(int(ev.get("cooldown_s", 60)))
        cooldown.connect("notify::value", lambda r, _p: self._set_event("cooldown_s", int(r.get_value())))
        group.add(cooldown)
        history = Adw.ActionRow(title=_("Event history"), activatable=True)
        history.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        history.connect("activated", lambda *_: self._show_events(self._cfg))
        group.add(history)
        return group

    def _set_event(self, key: str, value) -> None:
        if self._cfg.events.get(key) == value:
            return
        self._cfg.events[key] = value
        self._changed(self._cfg, False)

    def _build_recording(self) -> Gtk.Widget:
        rec = self._cfg.recording
        group = Adw.PreferencesGroup(
            title=_("Surveillance recording"),
            description=_("Records the original stream without re-encoding, in segments, "
                          "organised by camera and date."))
        mode = Adw.ComboRow(title=_("Mode"), model=Gtk.StringList.new(
            [_("Off"), _("Continuous"), _("On motion"), _("Scheduled")]))
        mode.set_selected(_REC_MODES.index(rec.get("mode", "off")) if rec.get("mode") in _REC_MODES else 0)
        group.add(mode)
        schedule = Adw.EntryRow(title=_("Schedule (e.g. 22:00-06:00, 12:00-13:00)"),
                                show_apply_button=True)
        schedule.set_text(rec.get("schedule", ""))
        schedule.connect("apply", lambda r: self._set_rec("schedule", r.get_text().strip()))
        schedule.set_visible(rec.get("mode") == "schedule")
        group.add(schedule)

        def on_mode(row, _p):
            value = _REC_MODES[row.get_selected()]
            schedule.set_visible(value == "schedule")
            self._set_rec("mode", value)
        mode.connect("notify::selected", on_mode)
        seg = Adw.SpinRow.new_with_range(1, 60, 1)
        seg.set_title(_("Segment length (minutes)"))
        seg.set_value(int(float(rec.get("segment_min", 10))))
        seg.connect("notify::value", lambda r, _p: self._set_rec("segment_min", int(r.get_value())))
        group.add(seg)
        keep = Adw.SpinRow.new_with_range(0, 365, 1)
        keep.set_title(_("Keep recordings (days)"))
        keep.set_subtitle(_("0 keeps everything"))
        keep.set_value(int(rec.get("retention_days", 7)))
        keep.connect("notify::value", lambda r, _p: self._set_rec("retention_days", int(r.get_value())))
        group.add(keep)
        folder = Adw.ActionRow(title=_("Open recordings folder"), subtitle=str(camera_dir(self._cfg)),
                               activatable=True)
        folder.add_suffix(Gtk.Image(icon_name="folder-open-symbolic"))
        folder.connect("activated", lambda *_: self._open_folder())
        group.add(folder)
        return group

    def _set_rec(self, key: str, value) -> None:
        if self._cfg.recording.get(key) == value:
            return
        self._cfg.recording[key] = value
        self._changed(self._cfg, False)

    def _open_folder(self) -> None:
        path = camera_dir(self._cfg)
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        launcher = Gtk.FileLauncher.new(None)
        from gi.repository import Gio
        launcher.set_file(Gio.File.new_for_path(str(path)))
        launcher.launch(self.get_root(), None, None)

    def _build_device(self, caps) -> Gtk.Widget:
        cfg = self._cfg
        group = Adw.PreferencesGroup(title=_("Device"))
        info = caps.device_info if caps else {}
        rows = [(_("Address"), cfg.host), (_("Manufacturer"), info.get("Manufacturer", cfg.manufacturer)),
                (_("Model"), info.get("Model", cfg.model)),
                (_("Firmware"), info.get("FirmwareVersion", cfg.firmware)),
                (_("Video codec"), caps.video_codec if caps else ""),
                (_("Audio codec"), caps.audio_codec if caps else "")]
        for title, value in rows:
            if value:
                row = Adw.ActionRow(title=title, subtitle=GLib.markup_escape_text(str(value)))
                row.set_subtitle_selectable(True)
                row.add_css_class("property")
                group.add(row)
        if caps is not None and caps.sd_playback.state != CONFIRMED:
            group.add(_unavailable_row(_("Recordings on the memory card"), caps.sd_playback.state,
                                       caps.sd_playback.detail))
        favorite = Adw.SwitchRow(title=_("Favorite"), subtitle=_("Shown first in the camera list"))
        favorite.set_active(cfg.favorite)
        favorite.connect("notify::active", lambda r, _p: self._set("favorite", r.get_active(), False))
        group.add(favorite)
        buttons = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER, margin_top=12)
        for label, icon, cb, css in ((_("Edit"), "document-edit-symbolic", lambda *_: self._edit(cfg), ""),
                                     (_("Camera wall"), "view-grid-symbolic", lambda *_: self._show_wall(), ""),
                                     (_("Remove"), "user-trash-symbolic", lambda *_: self._confirm_remove(), "destructive-action")):
            btn = Gtk.Button(child=Adw.ButtonContent(label=label, icon_name=icon))
            if css:
                btn.add_css_class(css)
            btn.connect("clicked", cb)
            buttons.append(btn)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(group)
        box.append(buttons)
        return box

    def _confirm_remove(self) -> None:
        dialog = Adw.AlertDialog(heading=_("Remove “{name}”?").format(name=self._cfg.name),
                                 body=_("The camera and its saved password are removed from BigCam. "
                                        "Recordings already on disk are kept."))
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("remove", _("Remove"))
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", lambda _d, r: r == "remove" and self._remove(self._cfg))
        dialog.present(self)


__all__ = ["IPCameraPanel", "PTZPad"]
