"""Add / edit IP camera assistant.

Two paths share one form:

* **Find automatically** – WS-Discovery plus a bounded probe of the local
  networks (or of an address/network typed by the user), showing vendor,
  model, address and protocols.
* **Add manually** – name, address, protocol, ports, user and password, with an
  optional full URL for uncommon devices.

"Test connection" runs the real RTSP/ONVIF probes and grabs one frame as a
preview; nothing is saved until the user confirms.  The password goes to the
Secret Service (libsecret) and never to the settings file.
"""

from __future__ import annotations

import logging
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, GLib, GObject, Gtk

from core.backends.ip_backend import IPBackend
from core.ipcam import discovery, rtsp, yoosee
from core.ipcam.capabilities import CONFIRMED, NEEDS_AUTH, probe_capabilities
from core.ipcam.models import IPCameraConfig
from core.ipcam.security import CredentialStore
from utils.async_worker import run_async
from utils.i18n import _

log = logging.getLogger(__name__)

_PROTOCOLS = [("onvif", "rtsp"), ("rtsp", "rtsp"), ("http", "http")]
_TRANSPORTS = ["tcp", "udp", "auto"]


def _spinner() -> Gtk.Widget:
    if hasattr(Adw, "Spinner"):
        return Adw.Spinner(width_request=32, height_request=32)
    return Gtk.Spinner(spinning=True, width_request=32, height_request=32)


def rtsp_message(status: str) -> str:
    return {
        rtsp.OK: _("Connected"),
        rtsp.AUTH_REQUIRED: _("This camera requires a user name and password."),
        rtsp.AUTH_FAILED: _("Wrong user name or password."),
        rtsp.NOT_FOUND: _("The stream path was not found on the camera."),
        rtsp.REFUSED: _("The camera refused the connection on this port."),
        rtsp.TIMEOUT: _("No answer from the camera. Check the address and the network."),
        rtsp.UNREACHABLE: _("The address is unreachable from this computer."),
    }.get(status, _("The camera answered with an unexpected response."))


class IPCameraDialog(Adw.Dialog):
    """Assistant for adding or editing a network camera."""

    __gsignals__ = {
        # Emitted with the validated IPCameraConfig after the password was stored.
        "camera-saved": (GObject.SignalFlags.RUN_LAST, None, (object,)),
        # Legacy signal kept for API compatibility (name, url without credentials).
        "camera-added": (GObject.SignalFlags.RUN_LAST, None, (str, str)),
    }

    def __init__(self, config: IPCameraConfig | None = None) -> None:
        super().__init__()
        self._editing = config
        self._scan_cancel: threading.Event | None = None
        self._scan_task = None
        self._test_task = None
        self._tested_ok = False
        self._caps = None
        self.set_title(_("Edit IP Camera") if config else _("Add IP Camera"))
        self.set_content_width(560)
        self.set_content_height(680)
        self.add_css_class("ipcam-dialog")

        self._nav = Adw.NavigationView()
        self._toasts = Adw.ToastOverlay(child=self._nav)
        self.set_child(self._toasts)
        if config is None:
            self._nav.add(self._build_start_page())
        self._form_page = self._build_form_page()
        if config is not None:
            self._fill_from_config(config)
            self._nav.add(self._form_page)
        self.connect("closed", lambda *_: self._cancel_tasks())

    # ------------------------------------------------------------------ pages

    def _page(self, title: str, child: Gtk.Widget, tag: str) -> Adw.NavigationPage:
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        view.set_content(child)
        return Adw.NavigationPage(title=title, child=view, tag=tag)

    def _build_start_page(self) -> Adw.NavigationPage:
        status = Adw.StatusPage(
            icon_name="network-server-symbolic",
            title=_("Add a network camera"),
            description=_("BigCam can look for ONVIF and RTSP cameras on your network, "
                          "or you can enter the camera details yourself."))
        group = Adw.PreferencesGroup()
        auto = Adw.ActionRow(title=_("Find automatically"),
                             subtitle=_("Search this computer's networks for cameras"),
                             activatable=True)
        auto.add_prefix(Gtk.Image(icon_name="system-search-symbolic"))
        auto.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        auto.connect("activated", lambda *_: self._open_search())
        manual = Adw.ActionRow(title=_("Add manually"),
                               subtitle=_("Address, user and password, or a full stream URL"),
                               activatable=True)
        manual.add_prefix(Gtk.Image(icon_name="document-edit-symbolic"))
        manual.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        manual.connect("activated", lambda *_: self._nav.push(self._form_page))
        group.add(auto)
        group.add(manual)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_start=16,
                      margin_end=16, margin_bottom=24)
        box.append(status)
        box.append(group)
        clamp = Adw.Clamp(maximum_size=480, child=box)
        return self._page(_("Add IP Camera"), Gtk.ScrolledWindow(child=clamp, vexpand=True), "start")

    def _build_search_page(self) -> Adw.NavigationPage:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12,
                      margin_bottom=24, margin_start=16, margin_end=16)
        group = Adw.PreferencesGroup(
            description=_("Leave empty to search the local networks of this computer. "
                          "You can also type an address or a network, e.g. 192.168.15.15 or 192.168.15.0/24."))
        self._target_row = Adw.EntryRow(title=_("Address or network (optional)"))
        self._target_row.connect("entry-activated", lambda *_: self._start_scan())
        group.add(self._target_row)
        box.append(group)
        self._scan_btn = Gtk.Button(label=_("Search"), halign=Gtk.Align.CENTER)
        self._scan_btn.add_css_class("pill")
        self._scan_btn.add_css_class("suggested-action")
        self._scan_btn.connect("clicked", lambda *_: self._toggle_scan())
        box.append(self._scan_btn)
        self._progress = Gtk.ProgressBar(visible=False, show_text=True)
        box.append(self._progress)
        self._results = Adw.PreferencesGroup(title=_("Cameras found"), visible=False)
        box.append(self._results)
        self._results_rows: list[Gtk.Widget] = []
        self._scan_status = Adw.StatusPage(icon_name="camera-disabled-symbolic", visible=False,
                                           title=_("No cameras found"))
        self._scan_status.add_css_class("compact")
        box.append(self._scan_status)
        manual = Gtk.Button(label=_("Enter details manually"), halign=Gtk.Align.CENTER)
        manual.add_css_class("flat")
        manual.connect("clicked", lambda *_: self._nav.push(self._form_page))
        box.append(manual)
        clamp = Adw.Clamp(maximum_size=480, child=box)
        return self._page(_("Find Cameras"), Gtk.ScrolledWindow(child=clamp, vexpand=True), "search")

    def _build_form_page(self) -> Adw.NavigationPage:
        page = self._form_prefs = Adw.PreferencesPage()

        cam = Adw.PreferencesGroup(title=_("Camera"))
        self._name_row = Adw.EntryRow(title=_("Name"))
        self._location_row = Adw.EntryRow(title=_("Location / group (optional)"))
        cam.add(self._name_row)
        cam.add(self._location_row)
        page.add(cam)

        conn = Adw.PreferencesGroup(title=_("Connection"))
        self._host_row = Adw.EntryRow(title=_("IP address or host name"))
        self._host_row.set_input_purpose(Gtk.InputPurpose.URL)
        self._proto_row = Adw.ComboRow(
            title=_("Protocol"),
            model=Gtk.StringList.new([_("ONVIF + RTSP (recommended)"), _("RTSP only"),
                                      _("HTTP / MJPEG")]))
        self._proto_row.connect("notify::selected", self._on_protocol_changed)
        self._rtsp_port = Adw.SpinRow.new_with_range(1, 65535, 1)
        self._rtsp_port.set_title(_("Stream port"))
        self._rtsp_port.set_value(554)
        self._onvif_port = Adw.SpinRow.new_with_range(1, 65535, 1)
        self._onvif_port.set_title(_("ONVIF port"))
        self._onvif_port.set_value(80)
        self._user_row = Adw.EntryRow(title=_("User name"))
        self._pass_row = Adw.PasswordEntryRow(title=_("Password"))
        self._hint = Gtk.Label(wrap=True, xalign=0, visible=False, margin_top=6)
        self._hint.add_css_class("dim-label")
        self._hint.add_css_class("caption")
        for row in (self._host_row, self._proto_row, self._rtsp_port, self._onvif_port,
                    self._user_row, self._pass_row):
            conn.add(row)
        page.add(conn)

        adv_group = Adw.PreferencesGroup()
        adv = Adw.ExpanderRow(title=_("Advanced"), subtitle=_("Stream paths, transport and full URL"))
        self._main_path = Adw.EntryRow(title=_("Main stream path"))
        self._main_path.set_text("/")
        self._sub_path = Adw.EntryRow(title=_("Secondary stream path (optional)"))
        self._transport = Adw.ComboRow(title=_("Transport"),
                                       model=Gtk.StringList.new([_("TCP (reliable)"), _("UDP (lower latency)"),
                                                                 _("Automatic")]))
        self._latency = Adw.SpinRow.new_with_range(0, 2000, 50)
        self._latency.set_title(_("Buffer (ms)"))
        self._latency.set_subtitle(_("Lower values reduce delay; raise on unstable networks"))
        self._latency.set_value(200)
        self._url_row = Adw.EntryRow(title=_("Full URL (fills the fields above)"))
        self._url_row.set_input_purpose(Gtk.InputPurpose.URL)
        self._url_row.connect("apply", self._on_url_apply)
        self._url_row.set_show_apply_button(True)
        for row in (self._main_path, self._sub_path, self._transport, self._latency, self._url_row):
            adv.add_row(row)
        adv_group.add(adv)
        page.add(adv_group)

        # Test result
        self._result_group = Adw.PreferencesGroup(title=_("Connection test"), visible=False)
        self._result_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self._result_status = Adw.ActionRow()
        self._result_icon = Gtk.Image(icon_name="emblem-ok-symbolic")
        self._result_status.add_prefix(self._result_icon)
        self._preview = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, height_request=200,
                                    visible=False, can_shrink=True)
        self._preview.add_css_class("ipcam-preview")
        self._preview.update_property([Gtk.AccessibleProperty.LABEL], [_("Camera preview")])
        self._features = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=6,
                                     column_spacing=6, row_spacing=6)
        list_box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        list_box.add_css_class("boxed-list")
        list_box.append(self._result_status)
        self._result_box.append(list_box)
        self._result_box.append(self._preview)
        self._result_box.append(self._features)
        self._result_group.add(self._result_box)
        page.add(self._result_group)

        buttons = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER, margin_top=6, margin_bottom=18)
        self._test_btn = Gtk.Button(label=_("Test Connection"))
        self._test_btn.add_css_class("pill")
        self._test_btn.connect("clicked", lambda *_: self._run_test())
        self._save_btn = Gtk.Button(label=_("Save") if self._editing else _("Add Camera"))
        self._save_btn.add_css_class("pill")
        self._save_btn.add_css_class("suggested-action")
        self._save_btn.connect("clicked", lambda *_: self._save())
        buttons.append(self._test_btn)
        buttons.append(self._save_btn)
        footer = Adw.PreferencesGroup()
        footer.add(self._hint)
        footer.add(buttons)
        page.add(footer)
        for row in (self._host_row, self._user_row, self._pass_row, self._main_path):
            row.connect("changed", lambda *_: self._invalidate_test())
        self._on_protocol_changed(self._proto_row, None)
        return self._page(_("Camera Details"), page, "form")

    # ---------------------------------------------------------------- search

    def _open_search(self) -> None:
        if not hasattr(self, "_search_page"):
            self._search_page = self._build_search_page()
        self._nav.push(self._search_page)
        GLib.idle_add(self._start_scan)

    def _toggle_scan(self) -> None:
        if self._scan_cancel is not None:
            self._scan_cancel.set()
            return
        self._start_scan()

    def _start_scan(self) -> bool:
        if self._scan_cancel is not None:
            return GLib.SOURCE_REMOVE
        text = self._target_row.get_text().strip()
        try:
            targets = discovery.parse_targets(text) if text else None
        except ValueError as exc:
            self._show_scan_message(_("Invalid address"), str(exc))
            return GLib.SOURCE_REMOVE
        for row in self._results_rows:
            self._results.remove(row)
        self._results_rows.clear()
        self._results.set_visible(False)
        self._scan_status.set_visible(False)
        self._progress.set_visible(True)
        self._progress.set_fraction(0.0)
        self._progress.set_text(_("Searching for cameras…"))
        self._scan_btn.set_label(_("Cancel"))
        self._scan_btn.remove_css_class("suggested-action")
        cancel = self._scan_cancel = threading.Event()

        def progress(fraction: float, stage: str) -> None:
            labels = {"ws-discovery": _("Asking ONVIF cameras to announce themselves…"),
                      "scan": _("Checking camera ports on the network…"),
                      "identify": _("Identifying cameras…"), "done": _("Done")}
            GLib.idle_add(self._set_progress, fraction, labels.get(stage, ""))

        self._scan_task = run_async(
            lambda: discovery.discover(targets, progress=progress, cancel=cancel),
            on_success=self._scan_done, on_error=lambda exc: self._scan_done([], exc))
        return GLib.SOURCE_REMOVE

    def _set_progress(self, fraction: float, text: str) -> bool:
        self._progress.set_fraction(fraction)
        if text:
            self._progress.set_text(text)
        return GLib.SOURCE_REMOVE

    def _scan_done(self, cameras, error=None) -> None:
        cancelled = self._scan_cancel is not None and self._scan_cancel.is_set()
        self._scan_cancel = None
        self._progress.set_visible(False)
        self._scan_btn.set_label(_("Search Again"))
        self._scan_btn.add_css_class("suggested-action")
        if error is not None:
            log.warning("Camera discovery failed: %s", type(error).__name__)
            self._show_scan_message(_("Search failed"), _("The network could not be searched."))
            return
        if cancelled:
            return
        if not cameras:
            self._show_scan_message(
                _("No cameras found"),
                _("Make sure the camera is on the same network. Some cameras only answer "
                  "when their address is typed above, or require ONVIF/RTSP to be enabled in "
                  "the vendor app."))
            return
        self._results.set_visible(True)
        for cam in cameras:
            protocols = " · ".join(p for p, ok in (("ONVIF", cam.onvif_port), ("RTSP", cam.rtsp_port),
                                                     ("HTTP", cam.http_port)) if ok)
            subtitle = f"{cam.host} · {protocols}"
            if cam.auth_required:
                subtitle += " · " + _("password required")
            title = cam.title
            if cam.model and cam.vendor == "yoosee":
                title = f"Yoosee ({cam.manufacturer} {cam.model}, {_('firmware')} {cam.firmware})"
            row = Adw.ActionRow(title=GLib.markup_escape_text(title),
                                subtitle=GLib.markup_escape_text(subtitle), activatable=True)
            row.add_prefix(Gtk.Image(icon_name="camera-video-symbolic"))
            row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
            row.connect("activated", lambda _r, c=cam: self._use_discovered(c))
            self._results.add(row)
            self._results_rows.append(row)

    def _show_scan_message(self, title: str, description: str) -> None:
        self._scan_status.set_title(title)
        self._scan_status.set_description(description)
        self._scan_status.set_visible(True)

    def _use_discovered(self, cam: discovery.DiscoveredCamera) -> None:
        self._host_row.set_text(cam.host)
        if not self._name_row.get_text():
            self._name_row.set_text(cam.name or (f"{cam.title} {cam.host.split('.')[-1]}"))
        if cam.onvif_port:
            self._proto_row.set_selected(0)
            self._onvif_port.set_value(cam.onvif_port)
        elif cam.rtsp_port:
            self._proto_row.set_selected(1)
        else:
            self._proto_row.set_selected(2)
        if cam.rtsp_port:
            self._rtsp_port.set_value(cam.rtsp_port)
        if cam.main_path:
            self._main_path.set_text(cam.main_path)
        if cam.sub_path:
            self._sub_path.set_text(cam.sub_path)
        if cam.vendor == "yoosee":
            self._user_row.set_text(self._user_row.get_text() or yoosee.YOOSEE_USERNAMES[0])
            self._show_hint(_("Yoosee: use the password defined in the Yoosee app under "
                              "Settings → NVR connection (not the app account password). "
                              "The user name is usually “admin”."))
        self._nav.push(self._form_page)
        self._pass_row.grab_focus()

    # ------------------------------------------------------------------ form

    def _fill_from_config(self, cfg: IPCameraConfig) -> None:
        self._name_row.set_text(cfg.name)
        self._location_row.set_text(cfg.location)
        self._host_row.set_text(cfg.host)
        self._proto_row.set_selected(["onvif", "rtsp", "http"].index(cfg.protocol))
        self._rtsp_port.set_value(cfg.rtsp_port if cfg.is_rtsp else cfg.http_port)
        self._onvif_port.set_value(cfg.onvif_port or 80)
        self._user_row.set_text(cfg.username)
        self._main_path.set_text(cfg.main_path)
        self._sub_path.set_text(cfg.sub_path)
        self._transport.set_selected(_TRANSPORTS.index(cfg.transport))
        self._latency.set_value(cfg.latency_ms)

        def got(password):
            if password and not self._pass_row.get_text():
                self._pass_row.set_text(password)
            self._tested_ok = False
        run_async(cfg.password, on_success=got)
        if cfg.vendor == "yoosee":
            self._show_hint(_("Yoosee: the password is the one set in the app under Settings → NVR connection."))

    def _show_hint(self, text: str) -> None:
        self._hint.set_label(text)
        self._hint.set_visible(True)

    def _on_protocol_changed(self, row, _pspec) -> None:
        index = row.get_selected()
        self._onvif_port.set_visible(index == 0)
        self._rtsp_port.set_title(_("HTTP port") if index == 2 else _("RTSP port"))
        if index == 2 and int(self._rtsp_port.get_value()) == 554:
            self._rtsp_port.set_value(80)
        elif index != 2 and int(self._rtsp_port.get_value()) == 80:
            self._rtsp_port.set_value(554)
        self._invalidate_test()

    def _on_url_apply(self, row: Adw.EntryRow) -> None:
        url = row.get_text().strip()
        try:
            cfg, password = IPCameraConfig.from_url(url, self._name_row.get_text())
        except ValueError as exc:
            self._toast(_("Invalid URL: {error}").format(error=str(exc)))
            return
        self._host_row.set_text(cfg.host)
        self._proto_row.set_selected(1 if cfg.is_rtsp else 2)
        self._rtsp_port.set_value(cfg.rtsp_port if cfg.is_rtsp else cfg.http_port)
        self._main_path.set_text(cfg.main_path)
        if cfg.username:
            self._user_row.set_text(cfg.username)
        if password:
            self._pass_row.set_text(password)
        # Never keep the credential-bearing URL around in the widget.
        row.set_text(cfg.stream_url())
        self._url_scheme = cfg.scheme

    def _collect(self) -> IPCameraConfig:
        proto = ["onvif", "rtsp", "http"][self._proto_row.get_selected()]
        port = int(self._rtsp_port.get_value())
        base = self._editing.to_dict() if self._editing else {}
        scheme = getattr(self, "_url_scheme", "") or ("http" if proto == "http" else "rtsp")
        if proto != "http" and scheme.startswith("http"):
            scheme = "rtsp"
        if proto == "http" and scheme.startswith("rtsp"):
            scheme = "http"
        base.update(
            name=self._name_row.get_text().strip() or self._host_row.get_text().strip(),
            location=self._location_row.get_text().strip(),
            host=self._host_row.get_text().strip(),
            protocol=proto, scheme=scheme,
            rtsp_port=port if proto != "http" else base.get("rtsp_port", 554),
            http_port=port if proto == "http" else base.get("http_port", 80),
            onvif_port=int(self._onvif_port.get_value()) if proto == "onvif" else 0,
            username=self._user_row.get_text().strip(),
            main_path=self._main_path.get_text().strip() or "/",
            sub_path=self._sub_path.get_text().strip(),
            transport=_TRANSPORTS[self._transport.get_selected()],
            latency_ms=int(self._latency.get_value()),
        )
        if self._caps is not None and self._caps.vendor != "generic":
            base["vendor"] = self._caps.vendor
            info = self._caps.device_info
            base.update(manufacturer=info.get("Manufacturer", ""), model=info.get("Model", ""),
                        firmware=info.get("FirmwareVersion", ""))
        if self._editing is None:
            return IPCameraConfig.new(**base)
        return IPCameraConfig.from_dict(base)

    def _invalidate_test(self) -> None:
        self._tested_ok = False

    # ------------------------------------------------------------------ test

    def _run_test(self) -> None:
        try:
            cfg = self._collect()
        except (ValueError, TypeError) as exc:
            self._show_result("dialog-error-symbolic", _("Invalid settings"), str(exc), error=True)
            return
        password = self._pass_row.get_text()
        self._test_btn.set_sensitive(False)
        self._show_result("content-loading-symbolic", _("Testing connection…"),
                          _("Contacting {host}").format(host=cfg.host))
        self._preview.set_visible(False)
        self._clear_features()

        def work():
            caps = probe_capabilities(cfg, password=password, timeout=6)
            frame = None
            if caps.video.state == CONFIRMED or not cfg.is_rtsp:
                try:
                    frame = IPBackend.snapshot_bytes(
                        cfg, password, stream="sub" if cfg.sub_path else "main", timeout_s=12)
                except Exception as exc:  # noqa: BLE001 - reported to the user below
                    log.info("Preview frame failed: %s", type(exc).__name__)
                    if not cfg.is_rtsp:
                        caps.video.state = "unknown"
                        caps.video.detail = str(exc)[:160]
                else:
                    caps.video.state = CONFIRMED
            return cfg, caps, frame

        self._test_task = run_async(work, on_success=self._test_done,
                                    on_error=lambda exc: self._test_failed(exc))

    def _test_failed(self, exc) -> None:
        self._test_btn.set_sensitive(True)
        self._show_result("dialog-error-symbolic", _("Test failed"), type(exc).__name__, error=True)

    def _test_done(self, result) -> None:
        cfg, caps, frame = result
        self._caps = caps
        self._test_btn.set_sensitive(True)
        if caps.vendor == "yoosee":
            self._show_hint(_("Yoosee camera detected. Light, siren, talk-back and memory-card "
                              "playback have no documented local API and stay in the Yoosee app."))
        if frame is not None:
            data, w, h, stride = frame
            texture = Gdk.MemoryTexture.new(w, h, Gdk.MemoryFormat.R8G8B8A8, GLib.Bytes.new(data), stride)
            self._preview.set_paintable(texture)
            self._preview.set_visible(True)
        if caps.video.state == CONFIRMED:
            self._tested_ok = True
            codec = " · ".join(x for x in (caps.video_codec, caps.audio_codec) if x)
            info = caps.device_info
            model = " ".join(x for x in (info.get("Manufacturer", ""), info.get("Model", "")) if x)
            self._show_result("emblem-ok-symbolic", _("Connected — live image received"),
                              " · ".join(x for x in (model, codec) if x))
        elif caps.video.state == NEEDS_AUTH:
            msg = rtsp_message(caps.rtsp_status)
            self._show_result("dialog-password-symbolic", msg,
                              _("Check the user name and password."), error=True)
        else:
            msg = rtsp_message(caps.rtsp_status) if caps.rtsp_status else caps.video.detail
            self._show_result("dialog-warning-symbolic", _("No image"), msg, error=True)
        self._show_features(caps)
        GLib.timeout_add(120, self._reveal_result)

    def _reveal_result(self) -> bool:
        """Scroll so the result (status + preview) is visible after a test."""
        scrolled = self._form_prefs.get_first_child()
        while scrolled is not None and not isinstance(scrolled, Gtk.ScrolledWindow):
            scrolled = scrolled.get_first_child()
        if scrolled is not None:
            ok, bounds = self._result_group.compute_bounds(scrolled.get_child())
            if ok:
                adj = scrolled.get_vadjustment()
                adj.set_value(min(bounds.get_y() - 12, adj.get_upper() - adj.get_page_size()))
        self._result_status.grab_focus()
        return GLib.SOURCE_REMOVE

    def _show_result(self, icon: str, title: str, subtitle: str = "", error: bool = False) -> None:
        self._result_group.set_visible(True)
        self._result_icon.set_from_icon_name(icon)
        self._result_status.set_title(GLib.markup_escape_text(title))
        self._result_status.set_subtitle(GLib.markup_escape_text(subtitle or ""))
        for cls in ("success", "error"):
            self._result_icon.remove_css_class(cls)
        self._result_icon.add_css_class("error" if error else "success")

    def _clear_features(self) -> None:
        child = self._features.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._features.remove(child)
            child = nxt

    def _show_features(self, caps) -> None:
        self._clear_features()
        labels = {"video": _("Video"), "audio": _("Audio"), "ptz": _("Pan/Tilt"),
                  "ircut": _("Night vision"), "events": _("Events"), "relays": _("Alarm outputs"),
                  "light": _("Light"), "siren": _("Siren"), "talkback": _("Talk-back"),
                  "sd_playback": _("Card recordings")}
        states = {"confirmed": ("ipcam-chip-ok", "✓"), "needs-auth": ("ipcam-chip-auth", "🔒"),
                  "unsupported": ("ipcam-chip-off", "—"), "unknown": ("ipcam-chip-off", "?")}
        for key, label in labels.items():
            cap = getattr(caps, key)
            css, mark = states.get(cap.state, states["unknown"])
            chip = Gtk.Label(label=f"{mark} {label}")
            chip.add_css_class("ipcam-chip")
            chip.add_css_class(css)
            tip = {"confirmed": _("Supported"), "needs-auth": _("Requires the camera password"),
                   "unsupported": _("Not available"), "unknown": _("Unknown")}[cap.state]
            chip.set_tooltip_text(f"{tip}. {cap.detail}" if cap.detail else tip)
            self._features.append(chip)

    # ------------------------------------------------------------------ save

    def _save(self) -> None:
        try:
            cfg = self._collect()
        except (ValueError, TypeError) as exc:
            self._show_result("dialog-error-symbolic", _("Invalid settings"), str(exc), error=True)
            return
        if not self._tested_ok:
            dialog = Adw.AlertDialog(
                heading=_("Save without a successful test?"),
                body=_("The connection has not been confirmed. You can still save the camera "
                       "and fix it later."))
            dialog.add_response("cancel", _("Cancel"))
            dialog.add_response("save", _("Save Anyway"))
            dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
            dialog.connect("response", lambda _d, r: r == "save" and self._persist(cfg))
            dialog.present(self)
            return
        self._persist(cfg)

    def _persist(self, cfg: IPCameraConfig) -> None:
        password = self._pass_row.get_text()
        self._save_btn.set_sensitive(False)
        store = CredentialStore.instance()

        def work():
            if password:
                return store.store(cfg.id, password, cfg.name)
            store.delete(cfg.id)
            return True

        def done(persisted):
            if password and not persisted:
                self._toast(_("No keyring available: the password is kept only until BigCam closes."))
            self.emit("camera-saved", cfg)
            self.emit("camera-added", cfg.name, cfg.stream_url())
            self.close()

        run_async(work, on_success=done, on_error=lambda _e: done(False))

    def _toast(self, text: str) -> None:
        self._toasts.add_toast(Adw.Toast(title=text, timeout=6))

    def _cancel_tasks(self) -> None:
        if self._scan_cancel is not None:
            self._scan_cancel.set()
        for task in (self._scan_task, self._test_task):
            if task is not None:
                task.cancel()
