"""Away / Take-Over mode — the AV-safe path to true background control.

Verified reality (2026-06-16): consumer Windows allows only ONE interactive
session, and a 2nd session needs an AV-flagged hack → ruled out. So the honest
"background" model is: the bot uses the SINGLE session FULLY when the user is
AWAY (idle) or explicitly hands control via a "Take Over" button. No conflict
(the user isn't using the machine), no VM, no hack — covers the 90% case
(night, lunch, AFK). On any real user input the bot RELEASES instantly.

This module only DECIDES whether the bot may take control. The actual sends
reuse the existing chat_send / chat_send_file / vision.
"""
from __future__ import annotations

import ctypes
import time

# manual "Take Over" — user explicitly handed the machine to the bot
_takeover_until: float = 0.0          # bot controls until this monotonic time
_DEFAULT_TAKEOVER_SEC = 600           # a take-over grants 10 min of control
# how long with NO user input before we consider the user "away"
DEFAULT_IDLE_AWAY_SEC = 120


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def idle_seconds() -> float:
    """Seconds since the user's last mouse/keyboard input (whole machine).
    Reliable Win32 GetLastInputInfo — 0 while the user is active."""
    li = _LASTINPUTINFO()
    li.cbSize = ctypes.sizeof(li)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(li)):
        return 0.0
    return (ctypes.windll.kernel32.GetTickCount() - li.dwTime) / 1000.0


def is_user_away(threshold_sec: float = DEFAULT_IDLE_AWAY_SEC) -> bool:
    return idle_seconds() >= threshold_sec


def request_takeover(duration_sec: int = _DEFAULT_TAKEOVER_SEC) -> None:
    """User pressed 'Take Over' — bot may use the machine for `duration_sec`."""
    global _takeover_until
    _takeover_until = time.monotonic() + duration_sec


def release_takeover() -> None:
    global _takeover_until
    _takeover_until = 0.0


def takeover_active() -> bool:
    return time.monotonic() < _takeover_until


def user_returned() -> bool:
    """True if the user is actively using the machine RIGHT NOW (any input in
    the last ~1.5s) — used to RELEASE control mid-task."""
    return idle_seconds() < 1.5


def should_bot_control(idle_threshold_sec: float = DEFAULT_IDLE_AWAY_SEC) -> dict:
    """May the bot take full control of the session right now?
    Yes if the user explicitly took-over, OR the user is away (idle)."""
    if takeover_active():
        return {"control": True, "reason": "takeover", "idle": idle_seconds()}
    idle = idle_seconds()
    if idle >= idle_threshold_sec:
        return {"control": True, "reason": "away", "idle": idle}
    return {"control": False, "reason": "user_active", "idle": idle}
