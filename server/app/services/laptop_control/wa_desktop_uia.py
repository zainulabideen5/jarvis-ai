"""WhatsApp Desktop App control via UIA + brief-flash typing.

Targets the Microsoft Store WhatsApp Desktop App (Electron-based).
Mirrors teams.py pattern:
  1. EnumWindows → find WhatsApp window by title
  2. Restore minimized silently (SW_SHOWNOACTIVATE)
  3. Connect UIA via HWND (multi-process Electron safe)
  4. UIA tree → search box + compose box discovery
  5. Force foreground (AttachThreadInput) → physical click on compose
  6. Clipboard paste text → Enter → verify
  7. For files: clipboard CF_HDROP → Ctrl+V into compose → Enter
  8. Restore previous foreground window

NOT pure background — brief 2-3 sec window flash is inherent to React WebView2.
See teams.py for the same architectural reasoning.
"""

from __future__ import annotations

import os
import time

from app.core.logging import get_logger

log = get_logger(__name__)


def _import_win32():
    import win32api  # type: ignore
    import win32con  # type: ignore
    import win32gui  # type: ignore
    import win32process  # type: ignore
    return win32api, win32con, win32gui, win32process


class WhatsAppDesktopUIA:
    """Singleton — WhatsApp Desktop App silent (brief-flash) automation via UIA."""

    _instance: "WhatsAppDesktopUIA | None" = None

    @classmethod
    def get(cls) -> "WhatsAppDesktopUIA":
        if cls._instance is None:
            cls._instance = WhatsAppDesktopUIA()
        return cls._instance

    # ────────────────────────────────────────────────────────────
    # Public API
    # ────────────────────────────────────────────────────────────

    def send_message_sync(
        self,
        recipient: str,
        message: str = "",
        attachment_path: str = "",
        timeout_sec: float = 60.0,
    ) -> tuple[bool, str]:
        """Send a WhatsApp Desktop message (and optional attachment).

        Approach: find chat by typing into the search bar (Ctrl+F or visible
        search Edit) → ArrowDown + Enter to open → paste compose → Enter.

        Returns (ok, message_or_error).
        """
        recipient = (recipient or "").strip()
        message = (message or "").strip()
        attachment = (attachment_path or "").strip()
        if not recipient:
            return False, "Recipient empty"
        if not message and not attachment:
            return False, "Message ya attachment dena hoga"
        if attachment and not os.path.isfile(attachment):
            return False, f"Attachment file nahi mili: {attachment}"

        # Reuse the Teams helpers — they're generic enough (force_foreground,
        # cursor save/restore, clipboard ops) and we don't want to duplicate.
        from app.services.laptop_control.teams import TeamsAutomation as TA

        try:
            from pywinauto.application import Application  # type: ignore
        except Exception as e:
            return False, f"pywinauto import fail: {e}"

        try:
            win32api, win32con, win32gui, _ = _import_win32()
        except Exception as e:
            return False, f"pywin32 import fail: {e}"

        # ----- Step 1: find WhatsApp Desktop window -----
        target_hwnd: int | None = None
        target_title: str | None = None

        import re as _re
        # Match "WhatsApp", "WhatsApp Beta", "WhatsApp - <Name>" but exclude
        # browser tabs which have suffixes like " - Google Chrome", " - Microsoft Edge"
        WA_TITLE_RE = _re.compile(r"^\s*whatsapp(\b|\s|-)", _re.IGNORECASE)
        BROWSER_TAIL_RE = _re.compile(
            r"\b(google chrome|microsoft edge|brave|firefox|opera|vivaldi|chromium)\b",
            _re.IGNORECASE,
        )

        def _enum_cb(hwnd, _):
            nonlocal target_hwnd, target_title
            try:
                if not win32gui.IsWindow(hwnd):
                    return True
                title = win32gui.GetWindowText(hwnd) or ""
            except Exception:
                return True
            if not title:
                return True
            t = title.strip()
            # Must START with "WhatsApp" (handles "WhatsApp Beta", "WhatsApp - X")
            if not WA_TITLE_RE.search(t):
                return True
            # Reject if title contains a browser name as a whole word
            if BROWSER_TAIL_RE.search(t):
                return True
            target_hwnd = hwnd
            target_title = title
            return False

        try:
            win32gui.EnumWindows(_enum_cb, None)
        except Exception:
            pass

        if not target_hwnd:
            return False, (
                "WhatsApp Desktop App window nahi mili. "
                "Microsoft Store se WhatsApp install kar aur QR scan kar phir retry."
            )

        # Save current foreground
        prev_fg = 0
        try:
            prev_fg = win32gui.GetForegroundWindow()
        except Exception:
            pass

        # Restore minimized silently
        try:
            if win32gui.IsIconic(target_hwnd):
                win32gui.ShowWindow(target_hwnd, win32con.SW_SHOWNOACTIVATE)
                time.sleep(0.4)
        except Exception:
            pass

        # ----- Step 2: connect UIA via HWND -----
        window = None
        try:
            app = Application(backend="uia").connect(handle=target_hwnd, timeout=4)
            window = app.window(handle=target_hwnd)
        except Exception as e:
            return False, f"WhatsApp UIA connect fail: {str(e)[:120]}"

        # ----- Step 3: force foreground -----
        if not TA._force_foreground(target_hwnd):
            return False, (
                "WhatsApp window foreground nahi aa raha — Windows ne focus block kar diya. "
                "WhatsApp pe ek baar manually click kar phir retry."
            )

        # ----- Step 4: open the chat with `recipient` -----
        ok_open, err = self._open_chat(window, target_hwnd, recipient)
        if not ok_open:
            TA._restore_foreground(prev_fg)
            return False, err

        time.sleep(1.0)  # let chat render fully

        # ----- Step 5: find compose box AFTER chat opened -----
        compose = self._find_compose_box(window)
        if not compose:
            TA._restore_foreground(prev_fg)
            return False, (
                f"'{recipient}' ka chat khuli par compose box UIA mein nahi mila. "
                "WhatsApp pe ek baar click kar + retry."
            )

        # ----- Step 6: ATTACHMENT (if any) — CF_HDROP paste -----
        if attachment:
            ok_clip = TA._copy_file_to_clipboard_cf_hdrop(attachment)
            if not ok_clip:
                TA._restore_foreground(prev_fg)
                return False, "File clipboard pe copy nahi hua (CF_HDROP fail)"

            try:
                rect = compose.rectangle()
                cx = (rect.left + rect.right) // 2
                cy = (rect.top + rect.bottom) // 2
                import pyautogui  # type: ignore
                pyautogui.FAILSAFE = False
                pyautogui.PAUSE = 0
                pyautogui.click(cx, cy)
                time.sleep(0.35)
                pyautogui.hotkey("ctrl", "v")
                time.sleep(3.5)  # WA generates attachment preview overlay
            except Exception as e:
                TA._restore_foreground(prev_fg)
                return False, f"File paste fail: {e}"

            # WhatsApp Desktop, unlike Teams, often pastes files DIRECTLY (no picker).
            # It shows a preview overlay with the image/file + a caption box.
            # We type the caption (if any) into the overlay's caption Edit.
            time.sleep(1.0)
            # Look for the caption Edit (different from main compose)
            caption_el = self._find_caption_box(window)
            if caption_el and message:
                try:
                    rect2 = caption_el.rectangle()
                    cx2 = (rect2.left + rect2.right) // 2
                    cy2 = (rect2.top + rect2.bottom) // 2
                    import pyautogui  # type: ignore
                    pyautogui.click(cx2, cy2)
                    time.sleep(0.3)
                    import pyperclip  # type: ignore
                    pyperclip.copy(message)
                    time.sleep(0.15)
                    pyautogui.hotkey("ctrl", "v")
                    time.sleep(0.4)
                except Exception as e:
                    log.warning("wa_desktop_caption_type_fail", error=str(e)[:120])

            # Send via Enter (works in WA Desktop overlay)
            try:
                import pyautogui  # type: ignore
                pyautogui.press("enter")
            except Exception as e:
                TA._restore_foreground(prev_fg)
                return False, f"Send Enter fail: {e}"
            time.sleep(2.0)
            TA._restore_foreground(prev_fg)
            return True, (
                f"WhatsApp pe {recipient} ko bhej diya: file({os.path.basename(attachment)})"
                + (f' + caption: "{message[:60]}"' if message else "")
            )

        # ----- Step 7: TEXT-ONLY send -----
        try:
            rect = compose.rectangle()
            cx = (rect.left + rect.right) // 2
            cy = (rect.top + rect.bottom) // 2
            import pyautogui  # type: ignore
            pyautogui.FAILSAFE = False
            pyautogui.PAUSE = 0
            pyautogui.click(cx, cy)
            time.sleep(0.35)
            pyautogui.hotkey("ctrl", "a")
            time.sleep(0.12)
            pyautogui.press("delete")
            time.sleep(0.15)

            import pyperclip  # type: ignore
            try:
                prev_clip = pyperclip.paste()
            except Exception:
                prev_clip = ""
            pyperclip.copy(message)
            time.sleep(0.15)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(0.5)
            try:
                if prev_clip:
                    pyperclip.copy(prev_clip)
            except Exception:
                pass
        except Exception as e:
            TA._restore_foreground(prev_fg)
            return False, f"Compose paste fail: {str(e)[:120]}"

        # Verify text in compose (via UIA read)
        in_compose = TA._read_compose_value(compose)
        needle = message.strip()[:25].lower()
        if needle and needle not in (in_compose or "").lower():
            TA._restore_foreground(prev_fg)
            return False, (
                f"Compose mein text inject nahi hua. "
                f"Compose state: '{(in_compose or '')[:80]}'. "
                "WhatsApp Desktop ne focus refuse kiya — retry karo."
            )

        # Send via Enter
        try:
            import pyautogui  # type: ignore
            pyautogui.press("enter")
        except Exception as e:
            TA._restore_foreground(prev_fg)
            return False, f"Enter fail: {e}"

        time.sleep(1.5)
        TA._restore_foreground(prev_fg)
        return True, f'WhatsApp pe {recipient} ko bhej diya: "{message[:80]}"'

    # ────────────────────────────────────────────────────────────
    # UIA discovery helpers
    # ────────────────────────────────────────────────────────────

    def _open_chat(self, window, hwnd: int, recipient: str) -> tuple[bool, str]:
        """Open WhatsApp chat with `recipient` by searching."""
        # WhatsApp Desktop has a search bar at top-left.
        # Approach: focus search via Ctrl+F or click search edit, type name,
        # press ArrowDown + Enter to select first match.
        try:
            import pyautogui  # type: ignore
            pyautogui.FAILSAFE = False
            pyautogui.PAUSE = 0
        except Exception as e:
            return False, f"pyautogui import: {e}"

        # Try UIA search-box click first
        search_el = self._find_search_box(window)
        if search_el is not None:
            try:
                rect = search_el.rectangle()
                cx = (rect.left + rect.right) // 2
                cy = (rect.top + rect.bottom) // 2
                pyautogui.click(cx, cy)
                time.sleep(0.3)
            except Exception:
                # Fallback to Ctrl+F shortcut
                pyautogui.hotkey("ctrl", "f")
                time.sleep(0.3)
        else:
            # No search UIA element found — use keyboard shortcut
            pyautogui.hotkey("ctrl", "f")
            time.sleep(0.3)

        # Clear any pre-existing search text
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.1)
        pyautogui.press("delete")
        time.sleep(0.1)

        # Type recipient via clipboard (unicode-safe)
        try:
            import pyperclip  # type: ignore
            try:
                prev_clip = pyperclip.paste()
            except Exception:
                prev_clip = ""
            pyperclip.copy(recipient)
            time.sleep(0.15)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(1.2)  # let WA filter contacts
            try:
                if prev_clip:
                    pyperclip.copy(prev_clip)
            except Exception:
                pass
        except Exception as e:
            return False, f"Search type fail: {e}"

        # Select first match
        pyautogui.press("down")
        time.sleep(0.3)
        pyautogui.press("enter")
        time.sleep(0.8)
        return True, "ok"

    def _find_search_box(self, window):
        """Find WhatsApp Desktop's search input. Returns UIA Edit element or None."""
        if window is None:
            return None
        try:
            for el in window.descendants(control_type="Edit"):
                try:
                    name = (el.element_info.name or "").lower()
                    if (
                        "search input" in name
                        or "search or start new chat" in name
                        or "search a chat" in name
                        or name == "search"
                    ):
                        return el
                except Exception:
                    continue
        except Exception:
            pass
        # Fallback: first Edit in window
        try:
            edits = list(window.descendants(control_type="Edit"))
            if edits:
                return edits[0]
        except Exception:
            pass
        return None

    def _find_compose_box(self, window):
        """Find WhatsApp Desktop's 'Type a message' compose Edit element."""
        if window is None:
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
                        return el
                except Exception:
                    continue
        except Exception:
            pass
        # Fallback to Document control
        try:
            for el in window.descendants(control_type="Document"):
                try:
                    name = (el.element_info.name or "").lower()
                    if "type a message" in name or "message" in name:
                        return el
                except Exception:
                    continue
        except Exception:
            pass
        return None

    def _find_caption_box(self, window):
        """Find the caption Edit inside the file-preview overlay (WA Desktop).

        After CF_HDROP paste, WA shows an overlay with the file preview + a caption
        textbox separate from the main compose. We need to type the caption there.
        """
        if window is None:
            return None
        try:
            for el in window.descendants(control_type="Edit"):
                try:
                    name = (el.element_info.name or "").lower()
                    if (
                        "add a caption" in name
                        or "caption" in name
                        or "type a caption" in name
                    ):
                        return el
                except Exception:
                    continue
        except Exception:
            pass
        return None
