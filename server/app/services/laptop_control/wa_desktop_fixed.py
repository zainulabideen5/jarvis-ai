"""WhatsApp Desktop App send via FIXED-WINDOW + percentage coords.

The Stonic AI / Inventor Usman approach:
  1. Find WhatsApp Desktop window (launch if not running)
  2. Force window to FIXED size + position (e.g. 1000x700 at 200,150)
  3. Force foreground (brief flash — accept this)
  4. Click search/contact/compose/send via PERCENTAGE-BASED coordinates
     (no UIA selector lookup — pure pixel automation)
  5. Type via pyautogui (Unicode-safe via clipboard)
  6. Minimize WhatsApp back to taskbar

Why this beats UIA selectors:
  - UIA selectors drift with WA updates → 70-90% reliability
  - Fixed-window pixel coords → 95%+ reliability
  - Faster (no UIA tree walk)
  - Works even when WhatsApp UI updates (positions don't move much)

Why PERCENTAGE coords instead of absolute pixels:
  - Different DPI scaling (100%, 125%, 150%) shifts pixel coords
  - Different monitor sizes
  - Percentage of window rect = same UI element regardless of scaling

REQUIRES:
  - WhatsApp Desktop App installed (Microsoft Store, free)
  - User logged in once via phone QR scan
  - 100% display scaling (recommended) — percentages handle 125%/150% reasonably
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from typing import Optional

from app.core.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class _Layout:
    """Percentage-of-window coords for each UI element.

    Calibrated for WhatsApp Desktop circa 2026 at 1000x700 window size.
    If WhatsApp moves elements significantly, tune these values.
    """
    # Search box at top-left ("Search or start new chat")
    search_x_pct: float = 0.15
    search_y_pct: float = 0.12
    # First contact in result list (after typing)
    first_contact_x_pct: float = 0.20
    first_contact_y_pct: float = 0.28
    # Compose box (bottom area, wide)
    compose_x_pct: float = 0.60
    compose_y_pct: float = 0.93
    # Send button (bottom-right corner, ▷ icon)
    send_x_pct: float = 0.97
    send_y_pct: float = 0.93
    # Attach button (paperclip, left of compose at bottom)
    attach_x_pct: float = 0.30
    attach_y_pct: float = 0.93


# Default fixed-window target — can be overridden via env vars.
_DEFAULT_WINDOW_X = int(os.environ.get("JARVIS_WA_X", "200"))
_DEFAULT_WINDOW_Y = int(os.environ.get("JARVIS_WA_Y", "150"))
_DEFAULT_WINDOW_W = int(os.environ.get("JARVIS_WA_W", "1000"))
_DEFAULT_WINDOW_H = int(os.environ.get("JARVIS_WA_H", "700"))

# OFF-SCREEN positioning for "behind JARVIS dashboard" mode. When enabled,
# WhatsApp window is placed at negative X coords so it's not visible on the
# user's monitor. Window is still technically focused for input — pyautogui
# clicks at off-screen coords reach it via Windows messaging.
# Experimental: Chromium-based apps may refuse off-screen input on some
# systems (~60-70% reliability across Windows versions).
# Enable globally via: set JARVIS_WA_BEHIND_MODE=1
# Or per-call: pass behind_mode=True to send_message_sync()
_OFFSCREEN_WINDOW_X = int(os.environ.get("JARVIS_WA_OFFSCREEN_X", "-1500"))
_OFFSCREEN_WINDOW_Y = int(os.environ.get("JARVIS_WA_OFFSCREEN_Y", "100"))
_BEHIND_MODE_DEFAULT = os.environ.get("JARVIS_WA_BEHIND_MODE", "0") == "1"


def _import_win32():
    import win32api  # type: ignore
    import win32con  # type: ignore
    import win32gui  # type: ignore
    return win32api, win32con, win32gui


class WhatsAppDesktopFixed:
    """Singleton — Stonic-style fixed-window WhatsApp Desktop send."""

    _instance: "WhatsAppDesktopFixed | None" = None

    @classmethod
    def get(cls) -> "WhatsAppDesktopFixed":
        if cls._instance is None:
            cls._instance = WhatsAppDesktopFixed()
        return cls._instance

    def __init__(self):
        self.layout = _Layout()
        # Position cache — keyed by window signature (x,y,w,h). When the
        # window is forced to the same size/position on subsequent calls,
        # UIA tree scan is skipped (saves ~1 sec per call).
        self._pos_cache: dict[tuple, dict] = {}
        # Memory cache for last-sent recipient — enables FAST PATH on repeat
        # sends to the same contact (skips entire search step). 5-min validity
        # because WhatsApp keeps the chat open as long as user doesn't switch.
        self._last_recipient: str = ""
        self._last_recipient_at: float = 0.0
        # Track the mode the last successful send used. FAST PATH only fires
        # when current behind_mode matches the cached one — different modes
        # mean different window position, so the cached compose rect would
        # be at wrong coords for the new send.
        self._last_behind_mode: bool = False
        self._RECIPIENT_CACHE_TTL = 300.0  # 5 minutes

    # ────────────────────────────────────────────────────────────────
    # Public API
    # ────────────────────────────────────────────────────────────────

    def send_message_sync(
        self,
        recipient: str,
        message: str = "",
        attachment_path: str = "",
        timeout_sec: float = 60.0,
        prefer_business: bool = False,
        behind_mode: Optional[bool] = None,
    ) -> tuple[bool, str]:
        """Send a WhatsApp Desktop message.

        Args:
            recipient: contact name or phone number to search
            message:   message text (can be empty if attachment provided)
            attachment_path: optional file path (image/video/document)
            timeout_sec: not used heavily; legacy compatibility
            prefer_business: if True, prefer "WhatsApp Business" window over
                             regular WhatsApp. Defaults to False (regular wins).
            behind_mode: if True, position WhatsApp window OFF-SCREEN so user
                         only sees the JARVIS dashboard. WhatsApp still receives
                         clicks (foreground state at negative coords). Experimental:
                         60-70% reliable depending on Chromium version. If None,
                         uses env var JARVIS_WA_BEHIND_MODE (default off).

        Returns (ok, message_or_error).
        """
        # Resolve behind_mode: explicit param wins, else env var, else False
        if behind_mode is None:
            behind_mode = _BEHIND_MODE_DEFAULT
        recipient = (recipient or "").strip()
        message = (message or "").strip()
        attachment = (attachment_path or "").strip()
        if not recipient:
            return False, "Recipient empty"
        if not message and not attachment:
            return False, "Message ya attachment dena hoga"
        if attachment and not os.path.isfile(attachment):
            return False, f"Attachment file nahi mili: {attachment}"

        try:
            win32api, win32con, win32gui = _import_win32()
        except Exception as e:
            return False, f"pywin32 import fail: {e}"

        # ----- Step 1: find or launch WhatsApp Desktop window -----
        hwnd = self._find_whatsapp_window(prefer_business=prefer_business)
        if not hwnd:
            app_label = "WhatsApp Business" if prefer_business else "WhatsApp"
            log.info("wa_desktop_not_running_launching", prefer_business=prefer_business)
            self._launch_whatsapp(business=prefer_business)
            # Wait up to 10 sec for window to appear
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                time.sleep(0.5)
                hwnd = self._find_whatsapp_window(prefer_business=prefer_business)
                if hwnd:
                    break
            if not hwnd:
                return False, (
                    f"{app_label} Desktop install karo Microsoft Store se, "
                    "QR scan kar lo phone se, phir retry kar."
                )

        # Save previous foreground for restoration
        prev_fg = 0
        try:
            prev_fg = win32gui.GetForegroundWindow()
        except Exception:
            pass

        # ----- Step 2: force window to fixed size + position (skip if already correct) -----
        # Saves ~0.3 sec per call when WA is already at target size.
        # When behind_mode is True, position window OFF-SCREEN (negative X).
        target_x = _OFFSCREEN_WINDOW_X if behind_mode else _DEFAULT_WINDOW_X
        target_y = _OFFSCREEN_WINDOW_Y if behind_mode else _DEFAULT_WINDOW_Y
        already_correct = False
        try:
            current = win32gui.GetWindowRect(hwnd)
            cw = current[2] - current[0]
            ch = current[3] - current[1]
            if (
                abs(current[0] - target_x) <= 2
                and abs(current[1] - target_y) <= 2
                and abs(cw - _DEFAULT_WINDOW_W) <= 2
                and abs(ch - _DEFAULT_WINDOW_H) <= 2
                and not win32gui.IsIconic(hwnd)
            ):
                already_correct = True
        except Exception:
            pass
        if not already_correct:
            self._resize_and_position_window(hwnd, behind_mode=behind_mode)
            time.sleep(0.18)
            # Verify resize actually took effect — silent SetWindowPos failures
            # would leave window at old coords, causing pyautogui clicks at the
            # wrong screen position. If post-resize position is too far off, log
            # and continue but with explicit awareness.
            try:
                post = win32gui.GetWindowRect(hwnd)
                if abs(post[0] - target_x) > 20 or abs(post[1] - target_y) > 20:
                    log.info("wa_desktop_resize_drift_detected",
                             expected_x=target_x, expected_y=target_y,
                             actual_x=post[0], actual_y=post[1])
                    # Try once more — Windows may have snapped the window
                    self._resize_and_position_window(hwnd, behind_mode=behind_mode)
                    time.sleep(0.2)
            except Exception:
                pass
        if behind_mode:
            log.info("wa_desktop_BEHIND_MODE_active",
                     x=target_x, y=target_y, w=_DEFAULT_WINDOW_W, h=_DEFAULT_WINDOW_H)

        # ----- Step 3: force foreground (brief flash, accepted) -----
        if not self._force_foreground(hwnd):
            return False, "WhatsApp window foreground nahi aa raha — manually click karo + retry"
        time.sleep(0.15)  # compressed from 0.2

        # Now compute absolute coordinates from the (now-fixed) window rect
        try:
            window_rect = win32gui.GetWindowRect(hwnd)
            wx, wy, wr, wb = window_rect
            ww = wr - wx
            wh = wb - wy
            log.info("wa_desktop_window_rect", x=wx, y=wy, w=ww, h=wh)
        except Exception as e:
            return False, f"Window rect read fail: {e}"

        def _pct(x_pct: float, y_pct: float) -> tuple[int, int]:
            return wx + int(ww * x_pct), wy + int(wh * y_pct)

        # ----- Step 4: search for contact (UIA-discover position + pyautogui click) -----
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

        # Use cached position if window signature unchanged — saves ~1 sec UIA scan.
        # Include behind_mode in the key so position cache doesn't return stale
        # coords from a different mode (e.g. normal-mode coords when behind_mode
        # is now True). Without this, FAST PATH could fire with wrong rect.
        window_sig = (wx, wy, ww, wh, behind_mode)
        cached = self._pos_cache.get(window_sig, {})

        # ----- FAST PATH: skip search entirely if recipient was just used -----
        # Memory cache is INSTANT (no UIA scan). FIRST-WORD match for tolerance —
        # e.g. "Zaid Zenesa" and "Zaid Zenesa Final" both match each other.
        # IMPORTANT: only fire FAST PATH when behind_mode matches the last send.
        # Otherwise the window may be at different coords than the cached compose
        # rect (e.g. last was normal mode at +200,+150; this is behind at -1500,+100).
        recipient_low = recipient.lower().strip()
        # Keep ALL words for matching — single-char names ("Z", "A") are valid.
        # Filter only OBVIOUSLY noise tokens (empty, punctuation-only).
        recipient_words = [w for w in recipient_low.split() if w and any(c.isalnum() for c in w)]
        skip_search = False
        last_low = self._last_recipient.lower().strip()
        last_mode_match = (self._last_behind_mode == behind_mode)
        if last_low and recipient_low and last_mode_match and (time.monotonic() - self._last_recipient_at) < self._RECIPIENT_CACHE_TTL:
            # Exact match OR first-word match (handles "Zaid" vs "Zaid Zenesa").
            # Both sides must have first-word >= 3 chars to avoid false-positive
            # FAST PATH on short names like "Z A" matching "Z Other".
            last_first = last_low.split()[0] if last_low else ""
            cur_first = recipient_low.split()[0] if recipient_low else ""
            if recipient_low == last_low or (
                last_first and cur_first
                and last_first == cur_first
                and len(last_first) >= 3 and len(cur_first) >= 3
            ):
                skip_search = True
                log.info("wa_desktop_FAST_PATH_HIT",
                         requested=recipient,
                         last=self._last_recipient,
                         age_sec=round(time.monotonic() - self._last_recipient_at, 1))
            else:
                log.info("wa_desktop_FAST_PATH_MISS_diff_recipient",
                         requested=recipient, last=self._last_recipient)
        else:
            log.info("wa_desktop_FAST_PATH_MISS_no_cache_or_expired",
                     last=self._last_recipient,
                     age_sec=round(time.monotonic() - self._last_recipient_at, 1) if self._last_recipient_at else "never")

        if not skip_search:
            # SLOW PATH: search → type → Down + Enter → verify
            search_rect = cached.get("search")
            if search_rect is None:
                search_rect = self._uia_find_search_rect(hwnd)
                if search_rect:
                    cached["search"] = search_rect
                    self._pos_cache[window_sig] = cached
                    log.info("wa_desktop_uia_search_found_cached", rect=search_rect)
            else:
                log.info("wa_desktop_search_cache_hit", rect=search_rect)

            if search_rect:
                sx = (search_rect[0] + search_rect[2]) // 2
                sy = (search_rect[1] + search_rect[3]) // 2
            else:
                sx, sy = _pct(self.layout.search_x_pct, self.layout.search_y_pct)
                log.info("wa_desktop_search_fallback_pct", x=sx, y=sy)

            try:
                pyautogui.click(sx, sy)
                time.sleep(0.2)
            except Exception as e:
                return False, f"Search click fail: {e}"

            # Clear any pre-existing search content (compressed)
            try:
                pyautogui.hotkey("ctrl", "a")
                time.sleep(0.05)
                pyautogui.press("delete")
                time.sleep(0.07)
            except Exception:
                pass

            # Type recipient via clipboard (unicode-safe)
            try:
                pyperclip.copy(recipient)
                time.sleep(0.05)
                pyautogui.hotkey("ctrl", "v")
                time.sleep(0.7)  # WA filters fast — compressed from 1.0
            except Exception as e:
                return False, f"Recipient paste fail: {e}"

            # Open first match via keyboard (ArrowDown + Enter)
            try:
                pyautogui.press("down")
                time.sleep(0.15)
                pyautogui.press("enter")
                time.sleep(0.6)  # chat loads — compressed from 0.9
            except Exception as e:
                return False, f"Contact select via keyboard fail: {e}"

            # ----- VERIFY (now with strict header-region filtering) -----
            # Reader only considers Text elements in the right-pane top region
            # (header). Date dividers ("Monday", "Today"), status text, and
            # sidebar labels are filtered out by region + word blocklist.
            try:
                opened_name = self._uia_read_chat_header_name(hwnd)
                log.info("wa_desktop_chat_opened_with", opened=opened_name, requested=recipient)
                if opened_name and recipient_words:
                    opened_low = opened_name.lower().strip()
                    # Phone numbers: strip non-digits for comparison
                    is_phone = recipient_low.replace("+", "").replace(" ", "").replace("-", "").isdigit()
                    if is_phone:
                        rec_digits = "".join(c for c in recipient_low if c.isdigit())
                        opened_digits = "".join(c for c in opened_low if c.isdigit())
                        # Last 7 digits should match (handles +country code variations)
                        if rec_digits and opened_digits and rec_digits[-7:] not in opened_digits:
                            self._minimize_window(hwnd)
                            self._restore_foreground(prev_fg)
                            return False, (
                                f"Galat contact open hua: '{opened_name}' (tu '{recipient}' bola). "
                                f"Full number ya specific naam likh."
                            )
                    else:
                        # Names: require ALL words to appear in opened name
                        missing = [w for w in recipient_words if w not in opened_low]
                        if missing:
                            self._minimize_window(hwnd)
                            self._restore_foreground(prev_fg)
                            return False, (
                                f"Galat contact open hua: '{opened_name}' (tu '{recipient}' bola). "
                                f"Full naam ya number specific likh."
                            )
            except Exception:
                pass  # verify failure not fatal — proceed with send

        # ----- Step 5: attachment (if any) — CF_HDROP paste + Ctrl+V -----
        # WhatsApp Desktop accepts Ctrl+V of any file type:
        #   PNG/JPG/GIF/BMP → image preview overlay
        #   MP4/MOV          → video preview overlay
        #   PDF/DOCX/XLSX   → document attachment
        #   MP3/AAC         → audio attachment
        # All via the same CF_HDROP clipboard mechanism.
        if attachment:
            # File size sanity (WhatsApp limit: 2GB documents, 16MB images/videos)
            try:
                size_mb = os.path.getsize(attachment) / (1024 * 1024)
                if size_mb > 2000:
                    self._minimize_window(hwnd)
                    self._restore_foreground(prev_fg)
                    return False, f"File 2GB se zyada: {size_mb:.1f} MB"
                log.info("wa_desktop_attachment", size_mb=round(size_mb, 1), path=os.path.basename(attachment))
            except OSError:
                pass

            from app.services.laptop_control.teams import TeamsAutomation as TA
            ok_clip = TA._copy_file_to_clipboard_cf_hdrop(attachment)
            if not ok_clip:
                self._minimize_window(hwnd)
                self._restore_foreground(prev_fg)
                return False, "File clipboard pe copy nahi hua (CF_HDROP fail)"

            # Use cached compose position (consistent with text path)
            attach_compose_rect = cached.get("compose")
            if attach_compose_rect is None:
                attach_compose_rect = self._uia_find_compose_rect(hwnd)
                if attach_compose_rect:
                    cached["compose"] = attach_compose_rect
                    self._pos_cache[window_sig] = cached

            if attach_compose_rect:
                acx = (attach_compose_rect[0] + attach_compose_rect[2]) // 2
                acy = (attach_compose_rect[1] + attach_compose_rect[3]) // 2
            else:
                acx, acy = _pct(self.layout.compose_x_pct, self.layout.compose_y_pct)

            try:
                pyautogui.click(acx, acy)
                time.sleep(0.25)
                pyautogui.hotkey("ctrl", "v")
                # WhatsApp shows attachment preview overlay — wait varies by type
                # Images/videos: 2-3 sec for thumbnail render
                # Documents: 1-2 sec for icon render
                time.sleep(2.8)
            except Exception as e:
                self._minimize_window(hwnd)
                self._restore_foreground(prev_fg)
                return False, f"Attachment paste fail: {e}"

            # If a caption was provided, type it in the overlay's caption box.
            # WA opens a "send media" dialog that auto-focuses a caption Edit.
            if message:
                try:
                    pyperclip.copy(message)
                    time.sleep(0.15)
                    pyautogui.hotkey("ctrl", "v")
                    time.sleep(0.4)
                except Exception:
                    pass

            # Press Enter to send the attachment (with caption if any)
            try:
                pyautogui.press("enter")
                time.sleep(2.0)
            except Exception as e:
                self._minimize_window(hwnd)
                self._restore_foreground(prev_fg)
                return False, f"Send Enter fail: {e}"

            self._minimize_window(hwnd)
            self._restore_foreground(prev_fg)
            try:
                if prev_clip:
                    pyperclip.copy(prev_clip)
            except Exception:
                pass
            # Update memory cache for FAST PATH on next send to same recipient
            self._last_recipient = recipient
            self._last_recipient_at = time.monotonic()
            self._last_behind_mode = behind_mode
            kind = " + caption" if message else ""
            return True, (
                f"WhatsApp pe {recipient} ko bhej diya: file({os.path.basename(attachment)}){kind} "
                f"(fixed-window pixel coords, brief flash)"
            )

        # ----- Step 6: TEXT-only send (cached or UIA-discover compose position) -----
        compose_rect = cached.get("compose")
        if compose_rect is None:
            compose_rect = self._uia_find_compose_rect(hwnd)
            if compose_rect:
                cached["compose"] = compose_rect
                self._pos_cache[window_sig] = cached
                log.info("wa_desktop_uia_compose_found_cached", rect=compose_rect)
        else:
            log.info("wa_desktop_compose_cache_hit", rect=compose_rect)

        if compose_rect:
            cmx = (compose_rect[0] + compose_rect[2]) // 2
            cmy = (compose_rect[1] + compose_rect[3]) // 2
        else:
            cmx, cmy = _pct(self.layout.compose_x_pct, self.layout.compose_y_pct)
            log.info("wa_desktop_compose_fallback_pct", x=cmx, y=cmy)

        try:
            pyautogui.click(cmx, cmy)
            time.sleep(0.15)  # compressed from 0.25
        except Exception as e:
            return False, f"Compose click fail: {e}"

        # Paste message (ultra-tight)
        try:
            pyperclip.copy(message)
            time.sleep(0.05)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(0.15)  # compressed from 0.25
        except Exception as e:
            return False, f"Message paste fail: {e}"

        # Send via Enter
        try:
            pyautogui.press("enter")
            time.sleep(0.4)  # compressed from 0.7
        except Exception as e:
            return False, f"Enter send fail: {e}"

        # Restore previous foreground + minimize WA
        self._minimize_window(hwnd)
        self._restore_foreground(prev_fg)
        try:
            if prev_clip:
                pyperclip.copy(prev_clip)
        except Exception:
            pass
        # Update memory cache for FAST PATH on next send to same recipient
        self._last_recipient = recipient
        self._last_recipient_at = time.monotonic()
        self._last_behind_mode = behind_mode

        return True, (
            f'WhatsApp pe {recipient} ko bhej diya: "{message[:80]}" '
            f"(fixed-window pixel coords, brief flash)"
        )

    # ────────────────────────────────────────────────────────────────
    # Internals
    # ────────────────────────────────────────────────────────────────

    def _find_whatsapp_window(self, prefer_business: bool = False) -> Optional[int]:
        """Find WhatsApp Desktop App's HWND. Distinguishes regular vs Business.

        When both apps are installed and running simultaneously, we pick based
        on `prefer_business`. Default: regular WhatsApp wins (most users).
        """
        try:
            _, _, win32gui = _import_win32()
        except Exception:
            return None

        import re
        WA_TITLE_RE = re.compile(r"^\s*whatsapp(\b|\s|-)", re.IGNORECASE)
        BUSINESS_RE = re.compile(r"^\s*whatsapp\s+business\b", re.IGNORECASE)
        BROWSER_TAIL_RE = re.compile(
            r"\b(google chrome|microsoft edge|brave|firefox|opera|vivaldi|chromium)\b",
            re.IGNORECASE,
        )

        regular_hits: list[int] = []
        business_hits: list[int] = []

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
            if not WA_TITLE_RE.search(t):
                return True
            if BROWSER_TAIL_RE.search(t):
                return True
            if BUSINESS_RE.search(t):
                business_hits.append(hwnd)
            else:
                regular_hits.append(hwnd)
            return True  # keep scanning so we get both

        try:
            win32gui.EnumWindows(_enum, None)
        except Exception:
            pass

        if prefer_business:
            return business_hits[0] if business_hits else (regular_hits[0] if regular_hits else None)
        # Default — prefer regular WhatsApp
        return regular_hits[0] if regular_hits else (business_hits[0] if business_hits else None)

    def _launch_whatsapp(self, business: bool = False) -> None:
        """Try to launch WhatsApp Desktop via standard Windows protocols.

        When `business=True` we launch the Business app (which uses a
        different AppsFolder ID + URL scheme).
        """
        if business:
            # WhatsApp Business app
            try:
                subprocess.Popen(
                    ["cmd", "/c", "start", "", "whatsapp-business://"],
                    shell=False,
                    creationflags=0x08000000,
                )
            except Exception:
                try:
                    subprocess.Popen(
                        ["cmd", "/c", "start", "", "shell:AppsFolder\\5319275A.WhatsAppBusiness_cv1g1gvanyjgm!App"],
                        shell=False,
                    )
                except Exception:
                    pass
            return
        # Regular WhatsApp
        try:
            subprocess.Popen(
                ["cmd", "/c", "start", "", "whatsapp://"],
                shell=False,
                creationflags=0x08000000,
            )
        except Exception:
            try:
                subprocess.Popen(
                    ["cmd", "/c", "start", "", "shell:AppsFolder\\5319275A.WhatsAppDesktop_cv1g1gvanyjgm!App"],
                    shell=False,
                )
            except Exception:
                pass

    def _resize_and_position_window(self, hwnd: int, behind_mode: bool = False) -> None:
        """Force WA window to fixed size + position. Restores from minimized.

        When behind_mode=True, places window at OFF-SCREEN coords so user
        only sees JARVIS dashboard. Window stays focused for input — clicks
        still reach it via pyautogui at off-screen coords.
        """
        try:
            win32api, win32con, win32gui = _import_win32()
        except Exception:
            return
        target_x = _OFFSCREEN_WINDOW_X if behind_mode else _DEFAULT_WINDOW_X
        target_y = _OFFSCREEN_WINDOW_Y if behind_mode else _DEFAULT_WINDOW_Y
        try:
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                time.sleep(0.3)
            # SetWindowPos: (hwnd, hwnd_insert_after, x, y, w, h, flags)
            win32gui.SetWindowPos(
                hwnd,
                0,
                target_x,
                target_y,
                _DEFAULT_WINDOW_W,
                _DEFAULT_WINDOW_H,
                0x0040,  # SWP_SHOWWINDOW
            )
        except Exception as e:
            log.info("wa_desktop_resize_failed", error=str(e)[:120])

    def _minimize_window(self, hwnd: int) -> None:
        """Minimize the WhatsApp window after send (silent restore back to taskbar)."""
        try:
            _, win32con, win32gui = _import_win32()
            win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
        except Exception:
            pass

    # ── UIA position discovery (hybrid Stonic + JARVIS) ──
    # Use UIA to discover where the search box / compose box actually are
    # on the user's current WhatsApp Desktop layout, then click via pyautogui
    # at the discovered rect center. This combines Stonic's pixel-click speed
    # with UIA's per-user/per-version reliability.

    def _uia_find_search_rect(self, hwnd: int):
        """Return (left, top, right, bottom) of WA search box, or None."""
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
                    if (
                        "search input" in name
                        or "search or start" in name
                        or "search a chat" in name
                        or name == "search"
                    ):
                        r = el.rectangle()
                        return (r.left, r.top, r.right, r.bottom)
                except Exception:
                    continue
        except Exception:
            pass
        # Fallback: first Edit at top of window
        try:
            edits = list(window.descendants(control_type="Edit"))
            if edits:
                r = edits[0].rectangle()
                return (r.left, r.top, r.right, r.bottom)
        except Exception:
            pass
        return None

    def _uia_read_chat_header_name(self, hwnd: int) -> Optional[str]:
        """Read the currently-opened chat's contact name from WA's header UI.

        Filters out:
          - Date dividers (Monday, Tuesday, Today, Yesterday, etc.)
          - Status text (online, last seen, typing)
          - Sidebar labels (Chats, Calls, Status, Settings, Communities, Channels)
          - System tooltips and accessibility hints

        Restricts candidates to the RIGHT-PANE header area (right side, top
        portion) using window rect math, since sidebar/messages area can
        contain misleading text.
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

        # Header area: right pane (after sidebar ~30% width) + top ~15% of height
        header_x_min = wleft + int(wwidth * 0.30)
        header_x_max = wright
        header_y_min = wtop + int(wheight * 0.03)
        header_y_max = wtop + int(wheight * 0.18)

        # Date/time/status/sidebar words to exclude (case-insensitive substring match)
        DATE_WORDS = {
            "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
            "today", "yesterday", "tomorrow",
            "january", "february", "march", "april", "may", "june",
            "july", "august", "september", "october", "november", "december",
        }
        STATUS_WORDS = {
            "online", "last seen", "typing", "click here", "tap here",
            "status", "calls", "chats", "channels", "communities", "settings",
            "search input", "search a chat", "search or start", "search results",
            "new chat", "new group", "menu",
            "voice call", "video call", "mute", "search messages",
            "ago", "am", "pm",
        }

        def _is_filterable(text: str) -> bool:
            tl = text.lower().strip()
            if not tl or len(tl) > 60:
                return True
            # Exact match against date/status sets
            if tl in DATE_WORDS or tl in STATUS_WORDS:
                return True
            # Substring match for status patterns
            for sw in STATUS_WORDS:
                if sw in tl and len(tl) <= len(sw) + 15:
                    # "last seen today at 10:30" is filterable
                    return True
            # Pure number or date format
            import re as _re
            if _re.fullmatch(r"[\d:/\s\-]+", tl):
                return True
            # Single character or punctuation
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
                    # Restrict to the right-pane header region
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

        if not candidates:
            return None
        # Return the FIRST candidate in the header region (usually the contact name)
        return candidates[0]

    def _uia_find_compose_rect(self, hwnd: int):
        """Return (left, top, right, bottom) of WA compose box, or None."""
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
                    if (
                        "type a message" in name
                        or "type message" in name
                        or "message" == name
                        or "new message" in name
                    ):
                        r = el.rectangle()
                        return (r.left, r.top, r.right, r.bottom)
                except Exception:
                    continue
        except Exception:
            pass
        return None

    def _force_foreground(self, hwnd: int) -> bool:
        """Bypass Windows focus-stealing prevention. Mirrors Teams approach."""
        # Delegate to the proven Teams helper — same trick (AttachThreadInput).
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
