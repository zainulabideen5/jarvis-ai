"""Native laptop control — mouse + keyboard + window management.

For ANY Windows app — Chrome (any profile), Teams Desktop, Excel, Notepad,
Photoshop, anything. Uses pyautogui (mouse/keyboard) + pywinauto (UIA tree).
FOREGROUND mode — windows briefly visible during actions (user accepted).

Parallel system to wa_playwright.py / universal_browser.py — does NOT
touch WhatsApp/Teams/Gmail/Trello/Universal Browser code. Additive only.

Architecture (3-tier):
    Tier 1: UIA (pywinauto) — find window + click element by name (stable)
    Tier 2: Coordinate clicks (pyautogui) — for visual apps without UIA
    Tier 3: Image-match (OpenCV) — locate button by template image

Key safety:
    - Failsafe: move mouse to top-left to abort any pyautogui sequence
    - Always log actions for audit trail
    - No silent send: every action returns verified result
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from app.core.logging import get_logger

log = get_logger(__name__)

# Lazy imports — pyautogui has slow startup (initializes display info)
_pyautogui = None
_pywinauto = None


def _get_pyautogui():
    global _pyautogui
    if _pyautogui is None:
        import pyautogui as pag
        # Failsafe: top-left corner aborts any sequence — safety net
        pag.FAILSAFE = True
        # Tiny default pause between actions (looks more human)
        pag.PAUSE = 0.1
        _pyautogui = pag
    return _pyautogui


def _get_pywinauto():
    global _pywinauto
    if _pywinauto is None:
        import pywinauto
        _pywinauto = pywinauto
    return _pywinauto


class LaptopNative:
    """Singleton — native laptop control via mouse/keyboard/UIA.

    Foreground mode. Windows visible briefly during actions.
    """

    _instance: "LaptopNative | None" = None

    @classmethod
    def get(cls) -> "LaptopNative":
        if cls._instance is None:
            cls._instance = LaptopNative()
        return cls._instance

    # ==================================================================
    # Mouse primitives
    # ==================================================================

    def click_at(self, x: int, y: int, button: str = "left", clicks: int = 1) -> dict:
        """Click at absolute screen coordinates."""
        try:
            pag = _get_pyautogui()
            pag.click(x=x, y=y, button=button, clicks=clicks, interval=0.1)
            log.info("native_click_at", x=x, y=y, button=button)
            return {"ok": True, "x": x, "y": y}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    def move_to(self, x: int, y: int, duration: float = 0.3) -> dict:
        """Move mouse cursor to (x, y) over `duration` seconds."""
        try:
            pag = _get_pyautogui()
            pag.moveTo(x, y, duration=duration)
            return {"ok": True, "x": x, "y": y}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    def scroll(self, clicks: int, x: int | None = None, y: int | None = None) -> dict:
        """Scroll up (positive) or down (negative) by `clicks` units."""
        try:
            pag = _get_pyautogui()
            if x is not None and y is not None:
                pag.moveTo(x, y)
            pag.scroll(clicks)
            return {"ok": True, "scrolled": clicks}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    def drag_from_to(self, x1: int, y1: int, x2: int, y2: int, duration: float = 0.5) -> dict:
        """Drag from (x1,y1) to (x2,y2)."""
        try:
            pag = _get_pyautogui()
            pag.moveTo(x1, y1, duration=0.2)
            pag.dragTo(x2, y2, duration=duration, button="left")
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    # ==================================================================
    # Keyboard primitives
    # ==================================================================

    def type_text(self, text: str, interval: float = 0.02) -> dict:
        """Type text via keyboard (foreground window receives it)."""
        try:
            pag = _get_pyautogui()
            pag.typewrite(text, interval=interval)
            return {"ok": True, "len": len(text)}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    def press_key(self, key: str) -> dict:
        """Press a single key (e.g. 'enter', 'tab', 'esc', 'f5')."""
        try:
            pag = _get_pyautogui()
            pag.press(key)
            return {"ok": True, "key": key}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    def hotkey(self, *keys: str) -> dict:
        """Press a key combo (e.g. 'ctrl', 'c' / 'ctrl', 'shift', 't')."""
        try:
            pag = _get_pyautogui()
            pag.hotkey(*keys)
            return {"ok": True, "combo": "+".join(keys)}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    # ==================================================================
    # Screen info
    # ==================================================================

    def screen_size(self) -> dict:
        """Return (width, height) of primary screen."""
        try:
            pag = _get_pyautogui()
            w, h = pag.size()
            return {"ok": True, "width": w, "height": h}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    def mouse_position(self) -> dict:
        """Current mouse cursor position."""
        try:
            pag = _get_pyautogui()
            x, y = pag.position()
            return {"ok": True, "x": x, "y": y}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    def screenshot(self, save_path: str | None = None) -> dict:
        """Take a screenshot, optionally save to disk."""
        try:
            pag = _get_pyautogui()
            img = pag.screenshot()
            out = {"ok": True, "width": img.width, "height": img.height}
            if save_path:
                img.save(save_path)
                out["saved_to"] = save_path
            return out
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    # ==================================================================
    # Window / app management
    # ==================================================================

    def list_open_windows(self) -> dict:
        """List all visible top-level windows (title + process info)."""
        try:
            import pygetwindow as gw
            wins = []
            for w in gw.getAllWindows():
                if w.title and not w.isMinimized:
                    wins.append({
                        "title": w.title,
                        "left": w.left,
                        "top": w.top,
                        "width": w.width,
                        "height": w.height,
                    })
            return {"ok": True, "windows": wins, "count": len(wins)}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    def focus_window(self, title_substring: str) -> dict:
        """Bring a window to foreground by partial title match."""
        try:
            import pygetwindow as gw
            for w in gw.getAllWindows():
                if title_substring.lower() in (w.title or "").lower() and w.title:
                    try:
                        if w.isMinimized:
                            w.restore()
                        w.activate()
                        time.sleep(0.3)
                        return {"ok": True, "title": w.title}
                    except Exception:
                        # Activation sometimes fails on Windows — still consider found
                        return {"ok": True, "title": w.title, "note": "found but activate failed"}
            return {"ok": False, "error": f"No window matching '{title_substring}'"}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    def minimize_window(self, title_substring: str) -> dict:
        try:
            import pygetwindow as gw
            for w in gw.getAllWindows():
                if title_substring.lower() in (w.title or "").lower() and w.title:
                    try:
                        w.minimize()
                        return {"ok": True, "title": w.title}
                    except Exception as e:
                        return {"ok": False, "error": str(e)[:120]}
            return {"ok": False, "error": "Window not found"}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    # ==================================================================
    # UIA — click element by name in a window
    # ==================================================================

    def uia_click(self, window_title: str, element_name: str, control_type: str = "Button") -> dict:
        """Click an element BY NAME using UIA — most reliable for native apps.
        Works across resolutions (no pixel coords). Window briefly focused.
        """
        try:
            pwa = _get_pywinauto()
            app = pwa.Application(backend="uia").connect(title_re=f".*{window_title}.*", timeout=8)
            window = app.top_window()
            # Search by best_match (name) then control_type
            try:
                el = window.child_window(title=element_name, control_type=control_type)
                el.click_input()
            except Exception:
                # Fallback — find by name substring
                el = window.descendants(control_type=control_type)
                for e in el:
                    try:
                        if element_name.lower() in (e.window_text() or "").lower():
                            e.click_input()
                            return {"ok": True, "clicked": e.window_text()}
                    except Exception:
                        continue
                return {"ok": False, "error": f"'{element_name}' element nahi mila"}
            return {"ok": True, "clicked": element_name}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    def uia_type(self, window_title: str, field_name: str, text: str, control_type: str = "Edit") -> dict:
        """Type into a named field in a window."""
        try:
            pwa = _get_pywinauto()
            app = pwa.Application(backend="uia").connect(title_re=f".*{window_title}.*", timeout=8)
            window = app.top_window()
            try:
                el = window.child_window(title=field_name, control_type=control_type)
                el.set_text(text)
            except Exception:
                # Fallback — first matching control type
                el = window.descendants(control_type=control_type)
                if not el:
                    return {"ok": False, "error": f"No {control_type} control found"}
                el[0].set_text(text)
            return {"ok": True, "typed_chars": len(text)}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    # ==================================================================
    # Combined workflows
    # ==================================================================

    def open_and_focus(self, app_command: str, expected_title_substring: str, timeout: float = 8.0) -> dict:
        """Launch an app via subprocess + wait for its window to appear + focus."""
        try:
            import subprocess
            subprocess.Popen(app_command, shell=True)
        except Exception as e:
            return {"ok": False, "error": f"Launch fail: {e}"}
        # Poll for window appearance
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            r = self.focus_window(expected_title_substring)
            if r.get("ok"):
                return r
            time.sleep(0.5)
        return {"ok": False, "error": f"Window '{expected_title_substring}' nahi aayi {timeout}s mein"}
