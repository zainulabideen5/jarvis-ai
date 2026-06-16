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
import threading
import time
from ctypes import wintypes

from app.core.logging import get_logger

log = get_logger(__name__)

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


# ───────────────────── REAL-input monitor (LL hook) ─────────────────────
# GetLastInputInfo counts the BOT's own pyautogui input too (it resets idle),
# so it can't tell "user came back" from "bot is working". A low-level hook CAN:
# every mouse/keyboard event carries an INJECTED flag (set for SendInput/bot
# input). We record the time of the last NON-injected (REAL human) event only.
# The callback is tiny (flag check + timestamp) so it never lags input.
_WH_MOUSE_LL = 14
_WH_KEYBOARD_LL = 13
_LLMHF_INJECTED = 0x01
_LLKHF_INJECTED = 0x10
_LRESULT = ctypes.c_ssize_t
_ULONG_PTR = ctypes.c_size_t

_last_real_input: float = 0.0
_monitor_started = False
# The bot marks a short "I'm about to send input" window around its own
# clicks/keystrokes. The hook ignores ANY input inside this window — so even
# if a bot event isn't flagged INJECTED, it's still not mistaken for the user.
# (Belt-and-suspenders with the injected flag; this is the reliable part.)
_bot_acting_until: float = 0.0


def mark_bot_acting(seconds: float = 0.5) -> None:
    """Call right before the bot clicks/types — input during this window is
    treated as the bot's own, never as the user returning."""
    global _bot_acting_until
    _bot_acting_until = time.monotonic() + seconds


class _MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", _ULONG_PTR)]


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", _ULONG_PTR)]


_HOOKPROC = ctypes.CFUNCTYPE(_LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
_u32 = ctypes.windll.user32
_k32 = ctypes.windll.kernel32
# CRITICAL: set restype on GetModuleHandleW or the 64-bit handle gets truncated
# to 32-bit (invalid) → SetWindowsHookEx fails. Same for the hook fns.
_k32.GetModuleHandleW.restype = wintypes.HMODULE
_k32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
_u32.SetWindowsHookExW.restype = wintypes.HHOOK
_u32.SetWindowsHookExW.argtypes = [ctypes.c_int, _HOOKPROC, wintypes.HMODULE, wintypes.DWORD]
_u32.CallNextHookEx.restype = _LRESULT
_u32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
_u32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]

_mouse_proc = None   # keep refs alive (else GC → crash)
_kbd_proc = None


def _on_mouse(nCode, wParam, lParam):
    global _last_real_input
    try:
        if nCode >= 0:
            now = time.monotonic()
            ms = ctypes.cast(lParam, ctypes.POINTER(_MSLLHOOKSTRUCT)).contents
            # REAL human = NOT injected-flagged AND NOT inside a bot-acting window
            if not (ms.flags & _LLMHF_INJECTED) and now >= _bot_acting_until:
                _last_real_input = now
    except Exception:
        pass
    return _u32.CallNextHookEx(None, nCode, wParam, lParam)


def _on_kbd(nCode, wParam, lParam):
    global _last_real_input
    try:
        if nCode >= 0:
            now = time.monotonic()
            kb = ctypes.cast(lParam, ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents
            if not (kb.flags & _LLKHF_INJECTED) and now >= _bot_acting_until:
                _last_real_input = now
    except Exception:
        pass
    return _u32.CallNextHookEx(None, nCode, wParam, lParam)


def _pump():
    global _mouse_proc, _kbd_proc
    _mouse_proc = _HOOKPROC(_on_mouse)
    _kbd_proc = _HOOKPROC(_on_kbd)
    hmod = _k32.GetModuleHandleW(None)
    mh = _u32.SetWindowsHookExW(_WH_MOUSE_LL, _mouse_proc, hmod, 0)
    if not mh:
        log.warning("mouse_hook_failed", err=ctypes.get_last_error())
    kh = _u32.SetWindowsHookExW(_WH_KEYBOARD_LL, _kbd_proc, hmod, 0)
    if not kh:
        log.warning("kbd_hook_failed", err=ctypes.get_last_error())
    if not mh and not kh:
        return
    log.info("real_input_monitor_started")
    msg = wintypes.MSG()
    while _u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        _u32.TranslateMessage(ctypes.byref(msg))
        _u32.DispatchMessageW(ctypes.byref(msg))


def start_real_input_monitor() -> None:
    """Idempotent — install the LL hooks in a daemon thread (with its own
    message pump, required for LL hooks)."""
    global _monitor_started
    if _monitor_started:
        return
    _monitor_started = True
    threading.Thread(target=_pump, daemon=True, name="real-input-monitor").start()


def real_user_input_within(seconds: float) -> bool:
    """True if a REAL (non-bot) mouse/keyboard event happened within `seconds`.
    Ignores the bot's own injected input — so it's safe to call mid-task."""
    return _last_real_input > 0 and (time.monotonic() - _last_real_input) < seconds


def user_returned() -> bool:
    """True if the REAL human used mouse/keyboard in the last ~1.2s — used to
    RELEASE control mid-task. Uses the LL hook (NOT idle, which counts the bot's
    own input). Falls back to idle only if the hook isn't running."""
    start_real_input_monitor()
    if _monitor_started and _last_real_input > 0:
        return real_user_input_within(1.2)
    return idle_seconds() < 1.2   # fallback (less accurate; counts bot input)


def should_bot_control(idle_threshold_sec: float = DEFAULT_IDLE_AWAY_SEC) -> dict:
    """May the bot take full control of the session right now?
    Yes if the user explicitly took-over, OR the user is away (idle)."""
    if takeover_active():
        return {"control": True, "reason": "takeover", "idle": idle_seconds()}
    idle = idle_seconds()
    if idle >= idle_threshold_sec:
        return {"control": True, "reason": "away", "idle": idle}
    return {"control": False, "reason": "user_active", "idle": idle}
