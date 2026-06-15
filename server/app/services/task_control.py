"""Global STOP/abort control for long-running tasks.

A running engine/vision loop checks `is_stopped()` between steps; the user can
trip the flag mid-task (via the /api/stop endpoint or a 'stop'/'ruk ja' chat
message) and the loop bails cleanly at its next step. Thread-safe (the loops
run in worker threads via asyncio.to_thread)."""
from __future__ import annotations

import threading

_stop = threading.Event()


def request_stop() -> None:
    """Ask the currently running task to halt at its next step."""
    _stop.set()


def clear_stop() -> None:
    """Reset the flag — call at the START of a fresh task."""
    _stop.clear()


def is_stopped() -> bool:
    return _stop.is_set()
