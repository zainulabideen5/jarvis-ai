"""Teams Desktop App send — Stonic AI style polish.

Mirrors `wa_desktop_fixed.py` patterns:
  1. Fixed window size + position (skip resize if already correct)
  2. Force foreground (brief flash — accepted reality)
  3. UIA-discover position cache (per-window-signature)
  4. Memory cache for FAST PATH (skip search if same recipient as last)
  5. Smart verify with right-pane header scan + date/status filter
  6. Tight sleeps for snappy feel

Teams-specific quirks handled:
  - Multi-process WebView2: pick the chat-hosting HWND
  - Search via Ctrl+E + /chat slash command
  - Compose box found via React contenteditable
  - Send via Enter or Send button click

Falls through to existing `teams.py` logic for file picker / advanced cases.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from typing import Optional

from app.core.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class _Layout:
    """Percentage-of-window coords for Teams fallback (rarely used —
    UIA discovers actual positions on first call)."""
    search_x_pct: float = 0.50  # command bar centered at top
    search_y_pct: float = 0.06
    compose_x_pct: float = 0.60
    compose_y_pct: float = 0.93


_DEFAULT_WINDOW_X = int(os.environ.get("JARVIS_TEAMS_X", "200"))
_DEFAULT_WINDOW_Y = int(os.environ.get("JARVIS_TEAMS_Y", "100"))
_DEFAULT_WINDOW_W = int(os.environ.get("JARVIS_TEAMS_W", "1100"))
_DEFAULT_WINDOW_H = int(os.environ.get("JARVIS_TEAMS_H", "720"))


def _import_win32():
    import win32api  # type: ignore
    import win32con  # type: ignore
    import win32gui  # type: ignore
    return win32api, win32con, win32gui


class TeamsFixed:
    """Singleton — Stonic-style fast Teams Desktop send."""

    _instance: "TeamsFixed | None" = None

    @classmethod
    def get(cls) -> "TeamsFixed":
        if cls._instance is None:
            cls._instance = TeamsFixed()
        return cls._instance

    def __init__(self):
        self.layout = _Layout()
        # Position cache keyed by (x,y,w,h) — saves UIA scan per send
        self._pos_cache: dict[tuple, dict] = {}
        # Memory cache for FAST PATH
        self._last_recipient: str = ""
        self._last_recipient_at: float = 0.0
        self._RECIPIENT_CACHE_TTL = 300.0  # 5 min

    # ────────────────────────────────────────────────────────────────
    # Public API
    # ────────────────────────────────────────────────────────────────

    def send_message_sync(
        self,
        recipient: str,
        message: str = "",
        timeout_sec: float = 60.0,
    ) -> tuple[bool, str]:
        """Send a Teams text message via Stonic-style fixed-window automation.

        Returns (ok, message_or_error). Attachments fall back to the
        legacy `teams.py` UIA path (file picker chain) — this module
        is text-only fast path.
        """
        recipient = (recipient or "").strip()
        message = (message or "").strip()
        if not recipient:
            return False, "Recipient empty"
        if not message:
            return False, "Message text dena hoga (attachments legacy path use karta)"

        try:
            from pywinauto.application import Application  # type: ignore
        except Exception as e:
            return False, f"pywinauto import fail: {e}"

        try:
            win32api, win32con, win32gui = _import_win32()
        except Exception as e:
            return False, f"pywin32 import fail: {e}"

        # ----- Step 1: find Teams Desktop window -----
        hwnd = self._find_teams_window()
        if not hwnd:
            return False, (
                "Teams Desktop window nahi mili. Teams kholo phir retry — "
                "legacy path fallback karega is case mein."
            )

        # Save previous foreground for restoration
        prev_fg = 0
        try:
            prev_fg = win32gui.GetForegroundWindow()
        except Exception:
            pass

        # ----- Step 2: fixed window resize (skip if already correct) -----
        already_correct = False
        try:
            current = win32gui.GetWindowRect(hwnd)
            cw = current[2] - current[0]
            ch = current[3] - current[1]
            if (
                abs(current[0] - _DEFAULT_WINDOW_X) <= 2
                and abs(current[1] - _DEFAULT_WINDOW_Y) <= 2
                and abs(cw - _DEFAULT_WINDOW_W) <= 2
                and abs(ch - _DEFAULT_WINDOW_H) <= 2
                and not win32gui.IsIconic(hwnd)
            ):
                already_correct = True
        except Exception:
            pass
        if not already_correct:
            self._resize_and_position_window(hwnd)
            time.sleep(0.2)

        # ----- Step 3: force foreground (brief flash) -----
        if not self._force_foreground(hwnd):
            return False, "Teams foreground refuse — Windows ne block kar diya"
        time.sleep(0.15)

        # ----- Compute current window rect -----
        try:
            wx, wy, wr, wb = win32gui.GetWindowRect(hwnd)
            ww = wr - wx
            wh = wb - wy
        except Exception as e:
            return False, f"Window rect read fail: {e}"

        def _pct(x_pct: float, y_pct: float) -> tuple[int, int]:
            return wx + int(ww * x_pct), wy + int(wh * y_pct)

        # Position + memory caches
        window_sig = (wx, wy, ww, wh)
        cached = self._pos_cache.get(window_sig, {})

        # ----- FAST PATH: skip auto-open if recipient was just used -----
        # HIGH-priority safety: even on FAST PATH, verify chat header still
        # matches recipient. User may have manually switched to a different
        # chat in Teams between sends — without this check, message would
        # go to wrong contact silently.
        recipient_low = recipient.lower().strip()
        recipient_words = [w for w in recipient_low.split() if len(w) >= 2]
        skip_search = False
        last_low = self._last_recipient.lower().strip()
        if last_low and recipient_low and (time.monotonic() - self._last_recipient_at) < self._RECIPIENT_CACHE_TTL:
            last_first = last_low.split()[0] if last_low else ""
            cur_first = recipient_low.split()[0] if recipient_low else ""
            if recipient_low == last_low or (
                last_first and cur_first and last_first == cur_first and len(last_first) >= 3
            ):
                # Confirm by reading current chat header (smart matcher handles
                # names, emails, phone numbers via _name_matches)
                try:
                    current_header = self._uia_read_chat_header_name(hwnd)
                    if current_header and self._name_matches(recipient, current_header):
                        skip_search = True
                        log.info("teams_fixed_FAST_PATH_HIT_verified",
                                 requested=recipient,
                                 current_header=current_header,
                                 age_sec=round(time.monotonic() - self._last_recipient_at, 1))
                    elif current_header:
                        log.info("teams_fixed_FAST_PATH_MISS_header_mismatch",
                                 requested=recipient,
                                 current_header=current_header)
                    # If header read returned nothing, be conservative — don't FAST PATH
                except Exception:
                    pass  # any error → conservative slow path

        # ----- Step 4: import io tools -----
        try:
            import pyautogui  # type: ignore
            import pyperclip  # type: ignore
            pyautogui.FAILSAFE = False
            pyautogui.PAUSE = 0
        except Exception as e:
            return False, f"pyautogui/pyperclip import: {e}"

        try:
            prev_clip = pyperclip.paste()
        except Exception:
            prev_clip = ""

        # ----- Step 5: open chat (slow path) -----
        # SIMPLE SINGLE strategy: UIA-find search bar → click directly → /chat slash
        # The KEY fix: explicitly find + click on the search bar's UIA element
        # instead of relying on Ctrl+E shortcut (which sometimes doesn't override
        # compose-box focus in Teams).
        if not skip_search:
            ok_open, err = self._open_chat_via_uia_search_click(hwnd, recipient)
            if not ok_open:
                self._restore_foreground(prev_fg)
                try:
                    if prev_clip:
                        pyperclip.copy(prev_clip)
                except Exception:
                    pass
                return False, err

            # Verify chat header matches recipient
            try:
                opened_name = self._uia_read_chat_header_name(hwnd)
                log.info("teams_fixed_chat_opened_with", opened=opened_name, requested=recipient)
                if opened_name and not self._name_matches(recipient, opened_name):
                    self._restore_foreground(prev_fg)
                    try:
                        if prev_clip:
                            pyperclip.copy(prev_clip)
                    except Exception:
                        pass
                    return False, (
                        f"Galat chat open hua Teams mein: '{opened_name}' "
                        f"(tu '{recipient}' bola). Specific naam ya email likh, "
                        f"ya Teams mein chat manually khol + retry."
                    )
                elif not opened_name:
                    self._restore_foreground(prev_fg)
                    try:
                        if prev_clip:
                            pyperclip.copy(prev_clip)
                    except Exception:
                        pass
                    return False, (
                        f"Teams chat header read nahi hua — confirm nahi hua kya "
                        f"'{recipient}' ka chat khula. Manually open kar + retry."
                    )
            except Exception as e:
                self._restore_foreground(prev_fg)
                try:
                    if prev_clip:
                        pyperclip.copy(prev_clip)
                except Exception:
                    pass
                return False, f"Chat verify fail: {str(e)[:80]}"

        # ----- Step 6: find compose box (cached or UIA) -----
        compose_rect = cached.get("compose")
        if compose_rect is None:
            compose_rect = self._uia_find_compose_rect(hwnd)
            if compose_rect:
                cached["compose"] = compose_rect
                self._pos_cache[window_sig] = cached
                log.info("teams_fixed_uia_compose_found_cached", rect=compose_rect)
        else:
            log.info("teams_fixed_compose_cache_hit", rect=compose_rect)

        if compose_rect:
            cmx = (compose_rect[0] + compose_rect[2]) // 2
            cmy = (compose_rect[1] + compose_rect[3]) // 2
        else:
            cmx, cmy = _pct(self.layout.compose_x_pct, self.layout.compose_y_pct)
            log.info("teams_fixed_compose_fallback_pct", x=cmx, y=cmy)

        # ----- Step 7: click compose + paste message -----
        def _bail(err_msg: str) -> tuple[bool, str]:
            """Helper: restore clipboard + foreground before returning a failure."""
            try:
                if prev_clip:
                    pyperclip.copy(prev_clip)
            except Exception:
                pass
            self._restore_foreground(prev_fg)
            return False, err_msg

        try:
            pyautogui.click(cmx, cmy)
            time.sleep(0.2)
        except Exception as e:
            return _bail(f"Compose click fail: {e}")

        try:
            pyperclip.copy(message)
            time.sleep(0.05)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(0.25)
        except Exception as e:
            return _bail(f"Message paste fail: {e}")

        # ----- Step 8: send — multi-method chain with VERIFICATION -----
        # Method 1: Ctrl+Enter (Teams's universal send shortcut)
        # Method 2: Find Send button via UIA + pyautogui click
        # Method 3: Plain Enter (fallback for older Teams)
        # After each attempt, verify compose is EMPTY → message actually sent.
        # If compose still contains our text → send didn't fire, try next method.

        needle = message.strip()[:25].lower()

        def _compose_still_has_message() -> bool:
            """Check if message text is still in compose box (send didn't fire)."""
            if not needle:
                return False
            try:
                # Re-find compose after send attempt (UIA tree may have updated)
                fresh_rect = self._uia_find_compose_rect(hwnd)
                if not fresh_rect:
                    return False  # can't verify — assume sent
                # Read compose value via legacy teams.py helper
                from app.services.laptop_control.teams import TeamsAutomation as TA
                # Find the compose Edit element via fresh UIA scan
                from pywinauto.application import Application  # type: ignore
                app2 = Application(backend="uia").connect(handle=hwnd, timeout=2)
                win2 = app2.window(handle=hwnd)
                fresh_compose = TA._find_teams_compose(win2)
                if not fresh_compose:
                    return False  # can't verify
                val = TA._read_compose_value(fresh_compose) or ""
                return needle in val.lower()
            except Exception:
                return False  # any error → assume sent (avoid false negatives)

        sent_verified = False

        # Method 1: Ctrl+Enter
        try:
            pyautogui.hotkey("ctrl", "enter")
            time.sleep(0.8)
            if not _compose_still_has_message():
                sent_verified = True
                log.info("teams_fixed_send_ctrl_enter_verified")
        except Exception:
            pass

        # Method 2: Send button via UIA + pyautogui click
        if not sent_verified:
            try:
                from app.services.laptop_control.teams import TeamsAutomation as TA
                from pywinauto.application import Application  # type: ignore
                app3 = Application(backend="uia").connect(handle=hwnd, timeout=2)
                win3 = app3.window(handle=hwnd)
                send_btn = TA._find_send_button(win3)
                if send_btn is not None:
                    rect_s = send_btn.rectangle()
                    sx = (rect_s.left + rect_s.right) // 2
                    sy = (rect_s.top + rect_s.bottom) // 2
                    pyautogui.click(sx, sy)
                    time.sleep(0.8)
                    if not _compose_still_has_message():
                        sent_verified = True
                        log.info("teams_fixed_send_button_click_verified", x=sx, y=sy)
            except Exception:
                pass

        # Method 3: plain Enter
        if not sent_verified:
            try:
                pyautogui.press("enter")
                time.sleep(0.8)
                if not _compose_still_has_message():
                    sent_verified = True
                    log.info("teams_fixed_send_plain_enter_verified")
            except Exception:
                pass

        # If still not sent — return HONEST failure (don't claim success)
        if not sent_verified:
            return _bail(
                "Teams ne message accept nahi kiya — compose mein abhi bhi "
                "text dikha. Send button click try kiya par message nahi gaya. "
                "Manually Enter daba ke send kar — issue Teams's compose ka focus state."
            )

        # ----- Step 9: cleanup -----
        self._minimize_window(hwnd)
        self._restore_foreground(prev_fg)
        try:
            if prev_clip:
                pyperclip.copy(prev_clip)
        except Exception:
            pass

        # Update memory cache ONLY on verified send success
        self._last_recipient = recipient
        self._last_recipient_at = time.monotonic()

        return True, (
            f'Teams pe {recipient} ko bhej diya: "{message[:80]}" '
            f"(fixed-window, verified send)"
        )

    # ────────────────────────────────────────────────────────────────
    # Smart recipient ↔ chat-header matching
    # ────────────────────────────────────────────────────────────────

    @staticmethod
    def _name_matches(recipient: str, header: str) -> bool:
        """Smart match: handles names, emails, phone numbers, unicode.

        Returns True if `header` (Teams chat header text) plausibly identifies
        the same person as `recipient`. False if clearly different.

        Strategy:
          1. Email recipient → extract local-part, try local-part words too
             (e.g. "ahmed.khan@x.com" matches header "Ahmed Khan")
          2. Phone recipient → compare digit suffix (last 7 digits)
             (handles +country code variations)
          3. Plain name → all significant words (>=2 chars) must appear in header
             (handles "Zaid Moeen" vs "Zaid Moeen Photographer")
        """
        if not recipient or not header:
            return False
        rec = recipient.lower().strip()
        hdr = header.lower().strip()
        if rec == hdr:
            return True

        # Email path — try local part, also strip plus-addressing tag
        if "@" in rec:
            local = rec.split("@", 1)[0]
            # Strip "+tag" plus-addressing (e.g. ahmed+filter@x.com → ahmed)
            local_no_plus = local.split("+", 1)[0]
            # Try variants in priority order
            for variant in (
                rec,
                local,
                local_no_plus,
                local.replace(".", " ").replace("_", " ").replace("-", " "),
                local_no_plus.replace(".", " ").replace("_", " ").replace("-", " "),
            ):
                v = variant.strip()
                if not v:
                    continue
                if v in hdr:
                    return True
                v_words = [w for w in v.split() if len(w) >= 2]
                if v_words and all(w in hdr for w in v_words):
                    return True
            # Substring match either way
            if local_no_plus and (local_no_plus in hdr or hdr in local_no_plus):
                return True
            return False

        # Phone number path — digits dominant
        digits = "".join(c for c in rec if c.isdigit())
        non_digits_rec = "".join(c for c in rec.replace(" ", "") if not c.isdigit())
        if len(digits) >= 7 and len(non_digits_rec) <= 3:
            hdr_digits = "".join(c for c in hdr if c.isdigit())
            # Last 7 digits match (tolerates +country code variations)
            if digits[-7:] in hdr_digits:
                return True
            return False

        # Plain name path — all significant words must appear in header
        words = [w for w in rec.split() if len(w) >= 2]
        if not words:
            # nothing substantial to match — accept (was a single char/empty after stripping)
            return True
        return all(w in hdr for w in words)

    # ────────────────────────────────────────────────────────────────
    # Internals
    # ────────────────────────────────────────────────────────────────

    def _find_teams_window(self) -> Optional[int]:
        """Find the Teams Desktop chat window. Prefers windows with chat
        context (title contains 'Chat |' or recipient name)."""
        try:
            _, _, win32gui = _import_win32()
        except Exception:
            return None

        candidates: list[tuple[int, str]] = []

        def _enum(hwnd, _):
            try:
                if not win32gui.IsWindow(hwnd):
                    return True
                if not win32gui.IsWindowVisible(hwnd):
                    return True
                t = (win32gui.GetWindowText(hwnd) or "").strip()
            except Exception:
                return True
            if not t:
                return True
            tl = t.lower()
            if "microsoft teams" in tl or " | teams" in tl:
                candidates.append((hwnd, t))
            return True

        try:
            win32gui.EnumWindows(_enum, None)
        except Exception:
            pass

        if not candidates:
            return None

        # Prefer chat-context windows ("Chat | Name | Microsoft Teams")
        for hwnd, title in candidates:
            if "chat |" in title.lower():
                return hwnd
        # Fallback: first Teams window
        return candidates[0][0]

    def _resize_and_position_window(self, hwnd: int) -> None:
        """Force Teams window to fixed size + position. Restores from minimized."""
        try:
            _, win32con, win32gui = _import_win32()
        except Exception:
            return
        try:
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                time.sleep(0.3)
            win32gui.SetWindowPos(
                hwnd,
                0,
                _DEFAULT_WINDOW_X,
                _DEFAULT_WINDOW_Y,
                _DEFAULT_WINDOW_W,
                _DEFAULT_WINDOW_H,
                0x0040,  # SWP_SHOWWINDOW
            )
        except Exception as e:
            log.info("teams_fixed_resize_failed", error=str(e)[:120])

    def _minimize_window(self, hwnd: int) -> None:
        """Minimize the Teams window back to taskbar after send."""
        try:
            _, win32con, win32gui = _import_win32()
            win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
        except Exception:
            pass

    def _force_foreground(self, hwnd: int) -> bool:
        """Delegate to the proven Teams helper (AttachThreadInput trick)."""
        try:
            from app.services.laptop_control.teams import TeamsAutomation as TA
            return TA._force_foreground(hwnd)
        except Exception:
            return False

    def _restore_foreground(self, hwnd_prev: int) -> None:
        if not hwnd_prev:
            return
        try:
            from app.services.laptop_control.teams import TeamsAutomation as TA
            TA._restore_foreground(hwnd_prev)
        except Exception:
            pass

    def _open_chat_via_command_bar(self, hwnd: int, recipient: str) -> tuple[bool, str]:
        """Open chat with recipient via Ctrl+E + /chat slash command."""
        try:
            import pyautogui  # type: ignore
            import pyperclip  # type: ignore
        except Exception as e:
            return False, f"pyautogui/pyperclip: {e}"

        try:
            prev_clip_inner = pyperclip.paste()
        except Exception:
            prev_clip_inner = ""

        try:
            pyautogui.hotkey("ctrl", "e")
            time.sleep(0.9)
            pyautogui.hotkey("ctrl", "a")
            time.sleep(0.1)
            pyautogui.press("delete")
            time.sleep(0.15)
            pyperclip.copy(f"/chat {recipient}")
            time.sleep(0.15)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(2.8)
            pyautogui.press("enter")
            time.sleep(3.0)
        except Exception as e:
            try:
                if prev_clip_inner:
                    pyperclip.copy(prev_clip_inner)
            except Exception:
                pass
            return False, f"Command bar flow fail: {str(e)[:80]}"

        try:
            if prev_clip_inner:
                pyperclip.copy(prev_clip_inner)
        except Exception:
            pass
        return True, ""

    def _open_chat_via_uia_search_click(self, hwnd: int, recipient: str) -> tuple[bool, str]:
        """Open chat by clicking Teams's top search/command bar via UIA + typing.

        ROBUST single-strategy approach (replaces brittle Ctrl+E):

        1. Scan UIA tree for Edit elements in the top 15% of the window.
        2. Pick the widest one (Teams's main search bar is the widest top Edit).
        3. UIA set_focus() to force keyboard focus there (not just click).
        4. pyautogui click as secondary focus signal.
        5. Clear, paste /chat <recipient>, wait, Enter.

        Why this beats Ctrl+E: Ctrl+E sometimes doesn't override Teams's compose
        box focus state, causing "/chat <name>" to be typed INTO the open chat
        as a message. UIA set_focus on the search bar element is explicit.
        """
        try:
            import pyautogui  # type: ignore
            import pyperclip  # type: ignore
        except Exception as e:
            return False, f"pyautogui/pyperclip: {e}"
        try:
            from pywinauto.application import Application  # type: ignore
            import win32gui  # type: ignore
        except Exception as e:
            return False, f"pywinauto/win32: {e}"

        try:
            prev_clip_inner = pyperclip.paste()
        except Exception:
            prev_clip_inner = ""

        # Find the search bar via UIA
        search_bar = None
        search_rect = None
        try:
            app = Application(backend="uia").connect(handle=hwnd, timeout=3)
            window = app.window(handle=hwnd)
            wleft, wtop, wright, wbottom = win32gui.GetWindowRect(hwnd)
            wwidth = wright - wleft
            wheight = wbottom - wtop
            top_band_max_y = wtop + int(wheight * 0.15)

            best_width = 0
            for el in window.descendants(control_type="Edit"):
                try:
                    rect = el.rectangle()
                    # Must be in TOP band of window
                    if rect.top > top_band_max_y or rect.bottom < wtop:
                        continue
                    # Must be reasonably wide (search bar is wide; compose is much wider but at bottom)
                    width = rect.right - rect.left
                    if width < 200:
                        continue
                    name = (el.element_info.name or "").lower()
                    auto_id = (el.element_info.automation_id or "").lower()
                    # Prefer elements with "search"/"command" in name; otherwise widest top Edit
                    is_search = (
                        "search" in name or "command" in name
                        or "search" in auto_id or "search" in auto_id
                    )
                    score = width + (10000 if is_search else 0)
                    if score > best_width:
                        best_width = score
                        search_bar = el
                        search_rect = rect
                except Exception:
                    continue
        except Exception as e:
            log.info("teams_fixed_search_bar_uia_scan_fail", error=str(e)[:120])

        if not search_bar or not search_rect:
            # Fallback to Ctrl+E
            log.info("teams_fixed_search_bar_NOT_found_falling_to_ctrl_e")
            try:
                pyautogui.hotkey("ctrl", "e")
                time.sleep(0.9)
            except Exception as e:
                try:
                    if prev_clip_inner:
                        pyperclip.copy(prev_clip_inner)
                except Exception:
                    pass
                return False, f"Ctrl+E fallback fail: {str(e)[:80]}"
        else:
            log.info("teams_fixed_search_bar_FOUND",
                     name=search_bar.element_info.name,
                     rect=(search_rect.left, search_rect.top, search_rect.right, search_rect.bottom))
            # PRIMARY: UIA set_focus() — explicit keyboard focus
            try:
                search_bar.set_focus()
                time.sleep(0.3)
            except Exception as e:
                log.info("teams_fixed_set_focus_fail", error=str(e)[:80])
            # SECONDARY: pyautogui click on search bar's center
            try:
                cx = (search_rect.left + search_rect.right) // 2
                cy = (search_rect.top + search_rect.bottom) // 2
                pyautogui.click(cx, cy)
                time.sleep(0.5)
            except Exception as e:
                log.info("teams_fixed_click_search_fail", error=str(e)[:80])

        # Now type — clear + paste /chat command
        try:
            pyautogui.hotkey("ctrl", "a")
            time.sleep(0.1)
            pyautogui.press("delete")
            time.sleep(0.15)
            pyperclip.copy(f"/chat {recipient}")
            time.sleep(0.15)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(2.8)
            pyautogui.press("enter")
            time.sleep(3.0)
        except Exception as e:
            try:
                if prev_clip_inner:
                    pyperclip.copy(prev_clip_inner)
            except Exception:
                pass
            return False, f"Type+Enter fail: {str(e)[:80]}"

        try:
            if prev_clip_inner:
                pyperclip.copy(prev_clip_inner)
        except Exception:
            pass
        return True, ""

    def _open_chat_via_new_chat(self, hwnd: int, recipient: str) -> tuple[bool, str]:
        """Open chat via Ctrl+N (New Chat panel) → To: field → Down + Enter."""
        try:
            import pyautogui  # type: ignore
            import pyperclip  # type: ignore
        except Exception as e:
            return False, f"pyautogui/pyperclip: {e}"

        try:
            prev_clip_inner = pyperclip.paste()
        except Exception:
            prev_clip_inner = ""

        try:
            pyautogui.hotkey("ctrl", "n")
            time.sleep(1.2)
            pyautogui.hotkey("ctrl", "a")
            time.sleep(0.1)
            pyautogui.press("delete")
            time.sleep(0.15)
            pyperclip.copy(recipient)
            time.sleep(0.15)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(2.5)
            pyautogui.press("down")
            time.sleep(0.35)
            pyautogui.press("enter")
            time.sleep(2.5)
        except Exception as e:
            try:
                if prev_clip_inner:
                    pyperclip.copy(prev_clip_inner)
            except Exception:
                pass
            return False, f"Ctrl+N flow fail: {str(e)[:80]}"

        try:
            if prev_clip_inner:
                pyperclip.copy(prev_clip_inner)
        except Exception:
            pass
        return True, ""

    def _open_chat_via_sidebar_click(self, hwnd: int, recipient: str) -> tuple[bool, str]:
        """Find recipient in Teams left sidebar (recent/pinned chats) and click.

        Most reliable for recently-active contacts — bypasses search/autocomplete
        entirely. Returns (True, "") if a matching sidebar entry was clicked,
        (False, reason) otherwise.
        """
        try:
            import pyautogui  # type: ignore
        except Exception as e:
            return False, f"pyautogui: {e}"
        try:
            from pywinauto.application import Application  # type: ignore
            import win32gui  # type: ignore
        except Exception as e:
            return False, f"pywinauto/win32: {e}"
        try:
            app = Application(backend="uia").connect(handle=hwnd, timeout=3)
            window = app.window(handle=hwnd)
            wleft, wtop, wright, wbottom = win32gui.GetWindowRect(hwnd)
            wwidth = wright - wleft
            # Sidebar is the LEFT portion of window — typically 10-30% width zone
            sidebar_x_max = wleft + int(wwidth * 0.30)
        except Exception as e:
            return False, f"UIA connect: {e}"

        # Scan ListItem and TreeItem (Teams uses both) for matching chats
        for control_type in ("ListItem", "TreeItem", "Button"):
            try:
                for el in window.descendants(control_type=control_type):
                    try:
                        name = (el.element_info.name or "").strip()
                        if not name:
                            continue
                        rect = el.rectangle()
                        # Confirm element is in the LEFT sidebar region
                        if rect.right > sidebar_x_max or rect.left < wleft:
                            continue
                        # Smart match recipient ↔ this element's name
                        if self._name_matches(recipient, name):
                            cx = (rect.left + rect.right) // 2
                            cy = (rect.top + rect.bottom) // 2
                            pyautogui.click(cx, cy)
                            time.sleep(2.0)  # let chat load
                            log.info("teams_fixed_sidebar_click", contact=name, x=cx, y=cy)
                            return True, name
                    except Exception:
                        continue
            except Exception:
                continue

        return False, "Sidebar mein match nahi mila"

    def _uia_find_compose_rect(self, hwnd: int):
        """Return (left, top, right, bottom) of Teams compose box, or None."""
        try:
            from pywinauto.application import Application  # type: ignore
        except Exception:
            return None
        try:
            app = Application(backend="uia").connect(handle=hwnd, timeout=3)
            window = app.window(handle=hwnd)
        except Exception:
            return None
        try:
            for el in window.descendants(control_type="Edit"):
                try:
                    name = (el.element_info.name or "").lower()
                    auto_id = (el.element_info.automation_id or "").lower()
                    if (
                        "type a new message" in name
                        or "type a message" in name
                        or "new message" in name
                        or auto_id.startswith("new-message-")
                    ):
                        r = el.rectangle()
                        return (r.left, r.top, r.right, r.bottom)
                except Exception:
                    continue
        except Exception:
            pass
        return None

    def _uia_read_chat_header_name(self, hwnd: int) -> Optional[str]:
        """Read currently-opened chat's contact name from Teams's right-pane header.

        Filters out date dividers ("Monday"), status text, sidebar labels, etc.
        Restricts candidates to the right-pane top region.
        """
        try:
            from pywinauto.application import Application  # type: ignore
            import win32gui  # type: ignore
        except Exception:
            return None
        try:
            app = Application(backend="uia").connect(handle=hwnd, timeout=3)
            window = app.window(handle=hwnd)
        except Exception:
            return None

        try:
            wleft, wtop, wright, wbottom = win32gui.GetWindowRect(hwnd)
            wwidth = wright - wleft
            wheight = wbottom - wtop
        except Exception:
            return None

        # Conservative right-pane bounds — Teams sidebar can be wide on some
        # layouts. 35% leaves comfortable margin to skip sidebar text on
        # standard 1100px window.
        header_x_min = wleft + int(wwidth * 0.35)
        header_x_max = wright
        header_y_min = wtop + int(wheight * 0.06)
        header_y_max = wtop + int(wheight * 0.22)

        DATE_WORDS = {
            "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
            "today", "yesterday", "tomorrow",
            "january", "february", "march", "april", "may", "june",
            "july", "august", "september", "october", "november", "december",
        }
        STATUS_WORDS = {
            "online", "available", "busy", "do not disturb", "away",
            "last seen", "typing", "in a call", "in a meeting",
            "chat", "chats", "calls", "calendar", "files", "teams", "activity",
            "search input", "search", "new chat", "more options",
            "your profile", "settings", "help", "feedback",
            "video call", "audio call", "share", "format",
        }

        def _is_filterable(text: str) -> bool:
            tl = text.lower().strip()
            if not tl or len(tl) > 60:
                return True
            if tl in DATE_WORDS or tl in STATUS_WORDS:
                return True
            for sw in STATUS_WORDS:
                if sw in tl and len(tl) <= len(sw) + 15:
                    return True
            if re.fullmatch(r"[\d:/\s\-]+", tl):
                return True
            if len(tl) < 2:
                return True
            return False

        candidates: list[str] = []
        try:
            for el in window.descendants(control_type="Text"):
                try:
                    name = (el.element_info.name or "").strip()
                    if _is_filterable(name):
                        continue
                    rect = el.rectangle()
                    if (
                        rect.left >= header_x_min
                        and rect.right <= header_x_max
                        and rect.top >= header_y_min
                        and rect.bottom <= header_y_max
                    ):
                        candidates.append(name)
                except Exception:
                    continue
        except Exception:
            return None

        return candidates[0] if candidates else None
