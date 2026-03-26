"""Async worker helpers – run I/O off the main thread, deliver results via GLib.idle_add."""

import logging
import threading
from typing import Any, Callable

from gi.repository import GLib

log = logging.getLogger(__name__)


def run_async(
    task: Callable[..., Any],
    args: tuple = (),
    on_success: Callable[[Any], None] | None = None,
    on_error: Callable[[Exception], None] | None = None,
) -> None:
    """Run *task* in a daemon thread; post result/error to the GTK main loop."""

    def _worker() -> None:
        try:
            result = task(*args)
            if on_success is not None:
                GLib.idle_add(on_success, result)
        except Exception as exc:
            if on_error is not None:
                GLib.idle_add(on_error, exc)
            else:
                log.exception("Unhandled error in async task %s", task)

    threading.Thread(target=_worker, daemon=True).start()
