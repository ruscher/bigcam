"""Physical PTZ control with guaranteed stop.

Safety layers against a motor left running:

1. Every ``ContinuousMove`` carries an ONVIF ``Timeout`` (``MOVE_TIMEOUT_S``),
   so a compliant camera stops by itself even if BigCam disappears.
2. While a direction is held, the UI calls :meth:`keepalive`; the worker
   re-issues the move before the device timeout.  If keepalives stop (UI
   frozen, window closed, focus lost) a local watchdog sends ``Stop``.
3. A hard cap (``MAX_HOLD_S``) stops any continuous move.
4. :meth:`stop` and :meth:`close` always send ``Stop``; if the device rejects
   it, a zero-velocity ``ContinuousMove`` is sent as fallback (some Yoosee
   firmwares only implement ContinuousMove).

All network I/O runs on a dedicated worker thread; the latest command wins so
slow devices never accumulate a backlog.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable

from core.ipcam.onvif import OnvifClient, OnvifError

log = logging.getLogger(__name__)

MOVE_TIMEOUT_S = 1.5
KEEPALIVE_S = 0.8
WATCHDOG_S = 2.0
MAX_HOLD_S = 30.0


@dataclass
class _Command:
    kind: str                 # move, stop, relative, goto, set_preset, remove_preset, home, presets
    pan: float = 0.0
    tilt: float = 0.0
    zoom: float = 0.0
    token: str = ""
    name: str = ""
    callback: Callable | None = None


class PTZController:
    def __init__(self, client: OnvifClient, profile_token: str,
                 on_error: Callable[[str], None] | None = None) -> None:
        self._client = client
        self._profile = profile_token
        self._on_error = on_error or (lambda _m: None)
        self._cond = threading.Condition()
        self._pending: _Command | None = None
        self._queue: list[_Command] = []
        self._moving = False
        self._move_started = 0.0
        self._last_keepalive = 0.0
        self._last_sent = 0.0
        self._current = (0.0, 0.0, 0.0)
        self._closed = False
        self._thread = threading.Thread(target=self._run, name="bigcam-ptz", daemon=True)
        self._thread.start()

    # -- public API (any thread) ---------------------------------------------

    @property
    def moving(self) -> bool:
        return self._moving

    def start_move(self, pan: float, tilt: float, zoom: float = 0.0) -> None:
        with self._cond:
            self._pending = _Command("move", pan, tilt, zoom)
            self._last_keepalive = time.monotonic()
            self._cond.notify()

    def keepalive(self) -> None:
        with self._cond:
            self._last_keepalive = time.monotonic()

    def stop(self) -> None:
        with self._cond:
            self._pending = _Command("stop")
            self._cond.notify()

    def nudge(self, pan: float, tilt: float, zoom: float = 0.0) -> None:
        self._enqueue(_Command("relative", pan, tilt, zoom))

    def goto_preset(self, token: str, callback: Callable | None = None) -> None:
        self._enqueue(_Command("goto", token=token, callback=callback))

    def set_preset(self, name: str, callback: Callable | None = None, token: str = "") -> None:
        self._enqueue(_Command("set_preset", name=name, token=token, callback=callback))

    def remove_preset(self, token: str, callback: Callable | None = None) -> None:
        self._enqueue(_Command("remove_preset", token=token, callback=callback))

    def goto_home(self, callback: Callable | None = None) -> None:
        self._enqueue(_Command("home", callback=callback))

    def list_presets(self, callback: Callable) -> None:
        self._enqueue(_Command("presets", callback=callback))

    def close(self) -> None:
        """Stop the motor (synchronously, bounded) and end the worker."""
        with self._cond:
            was_moving = self._moving
            self._closed = True
            self._pending = None
            self._queue.clear()
            self._cond.notify()
        self._thread.join(timeout=self._client.timeout + 1)
        if was_moving and self._thread.is_alive():
            # Worker blocked in I/O: issue the stop from here as last resort.
            self._send_stop()

    # -- worker ----------------------------------------------------------------

    def _enqueue(self, cmd: _Command) -> None:
        with self._cond:
            self._queue.append(cmd)
            self._cond.notify()

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._closed and self._pending is None and not self._queue:
                    timeout = None
                    if self._moving:
                        now = time.monotonic()
                        timeout = max(0.05, min(
                            self._last_sent + KEEPALIVE_S - now,
                            self._last_keepalive + WATCHDOG_S - now,
                            self._move_started + MAX_HOLD_S - now))
                    if not self._cond.wait(timeout) and self._moving:
                        break  # timer expired: evaluate keepalive/watchdog below
                if self._closed:
                    break
                cmd = self._pending
                self._pending = None
                if cmd is None and self._queue:
                    cmd = self._queue.pop(0)
            now = time.monotonic()
            if cmd is None and self._moving:
                if (now - self._last_keepalive > WATCHDOG_S or now - self._move_started > MAX_HOLD_S):
                    log.info("PTZ watchdog stop (keepalive lost or hold limit reached)")
                    self._do_stop()
                elif now - self._last_sent >= KEEPALIVE_S:
                    self._do_move(*self._current, refresh=True)
                continue
            if cmd is not None:
                self._execute(cmd)
        if self._moving:
            self._do_stop()

    def _execute(self, cmd: _Command) -> None:
        if cmd.kind == "move":
            if not (cmd.pan or cmd.tilt or cmd.zoom):
                self._do_stop()
            else:
                self._do_move(cmd.pan, cmd.tilt, cmd.zoom)
            return
        if cmd.kind == "stop":
            self._do_stop()
            return
        result, error = None, ""
        try:
            if cmd.kind == "relative":
                self._client.ptz_relative_move(self._profile, cmd.pan, cmd.tilt, cmd.zoom)
            elif cmd.kind == "goto":
                self._client.ptz_goto_preset(self._profile, cmd.token)
            elif cmd.kind == "set_preset":
                result = self._client.ptz_set_preset(self._profile, cmd.name, cmd.token)
            elif cmd.kind == "remove_preset":
                self._client.ptz_remove_preset(self._profile, cmd.token)
            elif cmd.kind == "home":
                self._client.ptz_goto_home(self._profile)
            elif cmd.kind == "presets":
                result = self._client.ptz_get_presets(self._profile)
        except OnvifError as exc:
            error = type(exc).__name__
            log.info("PTZ %s failed: %s", cmd.kind, error)
        if cmd.callback:
            try:
                cmd.callback(result, error)
            except Exception:
                log.exception("PTZ callback failed")
        elif error:
            self._on_error(error)

    def _do_move(self, pan: float, tilt: float, zoom: float, refresh: bool = False) -> None:
        try:
            self._client.ptz_continuous_move(self._profile, pan, tilt, zoom, timeout_s=MOVE_TIMEOUT_S)
            if not refresh or not self._moving:
                self._move_started = time.monotonic()
            self._moving = True
            self._current = (pan, tilt, zoom)
            self._last_sent = time.monotonic()
        except OnvifError as exc:
            log.info("PTZ move failed: %s", type(exc).__name__)
            self._moving = False
            self._send_stop()
            self._on_error(type(exc).__name__)

    def _do_stop(self) -> None:
        self._moving = False
        self._current = (0.0, 0.0, 0.0)
        self._send_stop()

    def _send_stop(self) -> None:
        try:
            self._client.ptz_stop(self._profile)
            return
        except OnvifError as exc:
            log.info("PTZ Stop rejected (%s); sending zero-velocity move", type(exc).__name__)
        try:
            self._client.ptz_continuous_move(self._profile, 0.0, 0.0, 0.0, timeout_s=0.1)
        except OnvifError as exc:
            log.warning("PTZ could not confirm stop: %s", type(exc).__name__)
            self._on_error("stop-failed")
