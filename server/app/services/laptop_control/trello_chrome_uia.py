"""Trello card creation via Chrome UIA + brief-flash keyboard shortcut.

Approach (per Zain: NO API, NO CDP, NO Playwright):
  1. UIA scan Chrome/Edge windows for a tab whose title contains "Trello"
  2. If found, select that tab via UIA TabItem.select() — brings it active
     inside Chrome (window may still be background)
  3. Force foreground Chrome window (AttachThreadInput) — brief flash
  4. Press 'n' (Trello shortcut: opens "Add a card" overlay on current list)
  5. Wait for overlay, paste card name via clipboard
  6. Press Enter — card saved
  7. Restore previous foreground

REQUIRES:
  - Trello.com tab open in user's Chrome (any board)
  - User already logged in (Chrome session)
  - The board's target LIST is the one currently focused (Trello opens
    overlay on whichever list received the last interaction)

LIMITATIONS:
  - 'n' shortcut adds to the most-recently-active list. To target a specific
    list, user can click that list once before chat command, OR we can later
    add URL-based navigation.
"""

from __future__ import annotations

import time

from app.core.logging import get_logger

log = get_logger(__name__)

_BROWSER_TITLE_MARKERS = ("google chrome", "chromium", "brave", "microsoft edge", "edge")


def _import_win32():
    import win32api  # type: ignore
    import win32con  # type: ignore
    import win32gui  # type: ignore
    return win32api, win32con, win32gui


class TrelloChromeUIA:
    """Singleton — Trello card add via Chrome UIA (no API, brief flash)."""

    _instance: "TrelloChromeUIA | None" = None

    @classmethod
    def get(cls) -> "TrelloChromeUIA":
        if cls._instance is None:
            cls._instance = TrelloChromeUIA()
        return cls._instance

    def add_card_sync(
        self,
        card_name: str,
        board_hint: str = "",
        timeout_sec: float = 30.0,
    ) -> tuple[bool, str]:
        """Add a card to Trello via the 'n' keyboard shortcut.

        Args:
            card_name: card title
            board_hint: optional — if provided, ensures a Trello tab whose
                       title contains this hint is selected. If not provided,
                       any Trello tab works.
            timeout_sec: not used heavily; legacy compatibility.

        Returns (ok, message_or_error).
        """
        card_name = (card_name or "").strip()
        if not card_name:
            return False, "Card ka title dena hoga"

        try:
            from pywinauto.application import Application  # type: ignore
            import pygetwindow as gw  # type: ignore
        except Exception as e:
            return False, f"pywinauto/pygetwindow import fail: {e}"

        try:
            win32api, win32con, win32gui = _import_win32()
        except Exception as e:
            return False, f"pywin32 import fail: {e}"

        # Reuse Teams helpers (force_foreground, restore_foreground, clipboard)
        from app.services.laptop_control.teams import TeamsAutomation as TA

        # ----- Step 1: scan Chrome/Edge windows + UIA tab-strip for Trello tab -----
        target_hwnd: int | None = None
        target_title: str | None = None
        target_tab_element = None
        chrome_windows: list = []

        for w in gw.getAllWindows():
            t = (w.title or "").strip()
            if not t:
                continue
            tl = t.lower()
            if any(b in tl for b in _BROWSER_TITLE_MARKERS):
                chrome_windows.append(w)

        def _hwnd_of(pgw_window) -> int | None:
            """Get raw HWND from a pygetwindow window — fork-safe."""
            for attr in ("_hWnd", "hwnd", "_hwnd"):
                try:
                    val = getattr(pgw_window, attr, None)
                    if val:
                        return int(val)
                except Exception:
                    continue
            return None

        # First pass: tab is currently active inside its Chrome window (title shows "Trello | <Board>")
        for cw in chrome_windows:
            t = cw.title or ""
            tl = t.lower()
            if "trello" in tl and (not board_hint or board_hint.lower() in tl):
                target_hwnd = _hwnd_of(cw)
                target_title = t
                break

        # Second pass: tab is background — scan UIA tab strip for "Trello"
        if not target_hwnd:
            for cw in chrome_windows:
                try:
                    app = Application(backend="uia").connect(title=cw.title, timeout=3)
                    window = app.top_window()
                    for tab in window.descendants(control_type="TabItem"):
                        try:
                            tname = (tab.element_info.name or "").lower()
                            if "trello" in tname and (not board_hint or board_hint.lower() in tname):
                                target_hwnd = _hwnd_of(cw)
                                target_title = cw.title
                                target_tab_element = tab
                                break
                        except Exception:
                            continue
                    if target_hwnd:
                        break
                except Exception:
                    continue

        if not target_hwnd:
            hint_msg = f" containing '{board_hint}'" if board_hint else ""
            return False, (
                f"Chrome mein koi Trello tab{hint_msg} nahi mili. "
                "Chrome mein trello.com kholo + login confirm karo phir retry."
            )

        # Save previous foreground for restoration
        prev_fg = 0
        try:
            prev_fg = win32gui.GetForegroundWindow()
        except Exception:
            pass

        # ----- Step 2: if Trello is on a background tab, select it via UIA -----
        if target_tab_element is not None:
            try:
                target_tab_element.select()
                time.sleep(0.4)
            except Exception:
                try:
                    target_tab_element.click_input()
                    time.sleep(0.4)
                except Exception:
                    pass

        # ----- Step 3: restore minimized window silently -----
        try:
            if win32gui.IsIconic(target_hwnd):
                win32gui.ShowWindow(target_hwnd, win32con.SW_SHOWNOACTIVATE)
                time.sleep(0.4)
        except Exception:
            pass

        # ----- Step 4: force foreground (brief flash) -----
        if not TA._force_foreground(target_hwnd):
            return False, (
                "Trello Chrome window foreground nahi aa raha. "
                "Chrome pe ek baar manually click kar phir retry."
            )

        time.sleep(0.4)

        # ----- Step 5: press 'n' to open Trello's "Add a card" overlay -----
        try:
            import pyautogui  # type: ignore
            pyautogui.FAILSAFE = False
            pyautogui.PAUSE = 0
            pyautogui.press("n")
            time.sleep(1.0)  # let overlay render
        except Exception as e:
            TA._restore_foreground(prev_fg)
            return False, f"'n' keystroke fail: {e}"

        # ----- Step 6: paste card name via clipboard -----
        try:
            import pyperclip  # type: ignore
            try:
                prev_clip = pyperclip.paste()
            except Exception:
                prev_clip = ""
            pyperclip.copy(card_name)
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
            return False, f"Card name paste fail: {e}"

        # ----- Step 7: press Enter to save -----
        try:
            import pyautogui  # type: ignore
            pyautogui.press("enter")
            time.sleep(1.0)
        except Exception as e:
            TA._restore_foreground(prev_fg)
            return False, f"Save Enter fail: {e}"

        # Press Escape to close overlay (cleaner state for next time)
        try:
            import pyautogui  # type: ignore
            pyautogui.press("escape")
            time.sleep(0.2)
        except Exception:
            pass

        TA._restore_foreground(prev_fg)

        return True, (
            f"Trello card add ho gaya: \"{card_name[:80]}\" "
            f"(Chrome UIA — '{target_title[:60]}')"
        )
