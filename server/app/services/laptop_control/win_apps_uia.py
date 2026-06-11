"""Native Windows app drivers via UIA (Windows UI Automation).

Specialized drivers for common Windows apps. Built on top of pywinauto's
UIA backend — works across Windows versions (10/11), respects accessibility,
no pixel coordinates needed.

Apps covered:
    - Notepad        — create/save text files
    - Calculator     — arithmetic operations
    - File Explorer  — navigate, search
    - Settings       — open Windows Settings panels
    - Generic        — any app via "open → click element → type → save" workflow

All operations are FOREGROUND (window visible during action). User accepted
this tradeoff — true silent only for Office (COM) and Web (Playwright).
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from app.core.logging import get_logger

log = get_logger(__name__)


def _get_pwa():
    """Lazy import pywinauto."""
    import pywinauto
    return pywinauto


def _get_pyautogui():
    import pyautogui as pag
    pag.FAILSAFE = True
    pag.PAUSE = 0.1
    return pag


class WinAppsUIA:
    """Singleton — native Windows app drivers."""

    _instance: "WinAppsUIA | None" = None

    @classmethod
    def get(cls) -> "WinAppsUIA":
        if cls._instance is None:
            cls._instance = WinAppsUIA()
        return cls._instance

    # ==================================================================
    # NOTEPAD
    # ==================================================================

    def notepad_write_and_save(self, content: str, save_path: str = "") -> dict:
        """Open Notepad, type content, save to path. If save_path empty,
        opens new doc and just types (user can save manually later).
        """
        try:
            pwa = _get_pwa()
            pag = _get_pyautogui()
            # Launch
            subprocess.Popen("notepad.exe", shell=False)
            time.sleep(1.5)
            # Connect
            app = pwa.Application(backend="uia").connect(title_re=".*Notepad", timeout=10)
            window = app.top_window()
            window.set_focus()
            time.sleep(0.3)
            # Type
            if content:
                # Find edit control + type
                try:
                    edit = window.child_window(control_type="Edit")
                    edit.set_focus()
                    edit.type_keys(self._escape_for_send(content), with_spaces=True, with_newlines=True, pause=0.01)
                except Exception:
                    # Fallback: just type via keyboard
                    pag.typewrite(content, interval=0.01)
            time.sleep(0.5)
            if save_path:
                # Save via Ctrl+S → file dialog
                pag.hotkey("ctrl", "s")
                time.sleep(1.5)
                # File dialog: type full path in filename field
                save_dialog = None
                try:
                    save_dialog = app.window(title_re="Save As|Save")
                    save_dialog.wait("visible", timeout=5)
                except Exception:
                    pass
                # Type path
                abs_path = os.path.abspath(save_path)
                Path(abs_path).parent.mkdir(parents=True, exist_ok=True)
                pag.typewrite(abs_path, interval=0.01)
                time.sleep(0.3)
                pag.press("enter")
                time.sleep(1.0)
                # If "Replace?" prompt appears, press Enter for Yes
                try:
                    confirm = app.window(title_re="Confirm Save As|Replace")
                    if confirm.exists(timeout=1):
                        pag.press("enter")
                        time.sleep(0.5)
                except Exception:
                    pass
                if os.path.exists(abs_path):
                    return {"ok": True, "saved_to": abs_path, "chars": len(content)}
                return {"ok": False, "error": "Save confirm — file disk pe nahi mili"}
            return {"ok": True, "typed_chars": len(content), "note": "save_path not provided — unsaved"}
        except Exception as e:
            return {"ok": False, "error": str(e)[:300]}

    def notepad_read(self, file_path: str) -> dict:
        """Read a text file. Doesn't even need Notepad — direct Python file read."""
        if not os.path.exists(file_path):
            return {"ok": False, "error": f"File not found: {file_path}"}
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            return {"ok": True, "content": content, "chars": len(content)}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    @staticmethod
    def _escape_for_send(text: str) -> str:
        """pywinauto type_keys uses special chars for modifier keys.
        Escape literal + ^ % ~ ( ) { } [ ] characters by wrapping in {}."""
        if not text:
            return ""
        special = "+^%~(){}[]"
        out = []
        for ch in text:
            if ch in special:
                out.append("{" + ch + "}")
            else:
                out.append(ch)
        return "".join(out)

    # ==================================================================
    # CALCULATOR
    # ==================================================================

    def calculator_compute(self, expression: str) -> dict:
        """Use Windows Calculator to compute an expression.

        Supports basic arithmetic — digits, + - * /. Returns the displayed result.
        Note: For complex math (sqrt, sin, etc.) use eval() or sympy instead —
        Calculator is overkill for non-display use.
        """
        # Validate expression chars (safety)
        if not re.match(r"^[\d\s+\-*/.()]+$", expression or ""):
            return {"ok": False, "error": "Expression mein sirf digits + - * / ( ) . allowed"}
        try:
            pwa = _get_pwa()
            pag = _get_pyautogui()
            subprocess.Popen("calc.exe", shell=False)
            time.sleep(2.0)  # Calculator UWP app slow to start
            # Different connect strategies for Win10/11
            app = None
            for title_re in (".*Calculator", "Calculator"):
                try:
                    app = pwa.Application(backend="uia").connect(title_re=title_re, timeout=8)
                    break
                except Exception:
                    continue
            if app is None:
                return {"ok": False, "error": "Calculator window connect nahi hua"}
            window = app.top_window()
            window.set_focus()
            time.sleep(0.3)
            # Clear first (ESC = Clear)
            pag.press("escape")
            time.sleep(0.2)
            # Type the expression as digits + operators
            expr_clean = expression.replace(" ", "")
            for ch in expr_clean:
                if ch.isdigit() or ch == ".":
                    pag.typewrite(ch)
                elif ch == "+":
                    pag.typewrite("+")
                elif ch == "-":
                    pag.typewrite("-")
                elif ch == "*":
                    pag.typewrite("*")
                elif ch == "/":
                    pag.typewrite("/")
                elif ch in "()":
                    pag.typewrite(ch)
                time.sleep(0.05)
            pag.press("enter")
            time.sleep(0.5)
            # Read result from display
            result = ""
            try:
                # Win11 Calculator: AutomationId="CalculatorResults"
                for sel in [
                    {"auto_id": "CalculatorResults"},
                    {"auto_id": "CalculatorOutput"},
                ]:
                    try:
                        el = window.child_window(**sel)
                        result = el.window_text() or ""
                        if result:
                            break
                    except Exception:
                        continue
            except Exception:
                pass
            # Clean up "Display is 4,250" → "4,250"
            m = re.search(r"[-]?[\d,.]+", result)
            value = m.group(0).replace(",", "") if m else ""
            return {"ok": True, "expression": expression, "result": value, "raw": result}
        except Exception as e:
            return {"ok": False, "error": str(e)[:300]}

    # ==================================================================
    # FILE EXPLORER
    # ==================================================================

    def explorer_open(self, path: str = "") -> dict:
        """Open File Explorer at a path (default: This PC)."""
        target = path if path else "shell:MyComputerFolder"
        try:
            subprocess.Popen(f'explorer "{target}"', shell=True)
            time.sleep(1.2)
            return {"ok": True, "opened": target}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    def explorer_search(self, folder: str, query: str) -> dict:
        """Open a folder in Explorer + perform a search via the address bar."""
        if not os.path.isdir(folder):
            return {"ok": False, "error": f"Folder not found: {folder}"}
        try:
            pag = _get_pyautogui()
            subprocess.Popen(f'explorer "{folder}"', shell=True)
            time.sleep(1.5)
            # Ctrl+F focuses search box
            pag.hotkey("ctrl", "f")
            time.sleep(0.3)
            pag.typewrite(query, interval=0.02)
            time.sleep(0.2)
            pag.press("enter")
            time.sleep(1.0)
            return {"ok": True, "folder": folder, "query": query}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    # ==================================================================
    # SETTINGS
    # ==================================================================

    def settings_open(self, panel: str = "") -> dict:
        """Open Windows Settings, optionally to a specific panel.

        panel examples:
            ""           → main Settings page
            "network"    → ms-settings:network
            "display"    → ms-settings:display
            "apps"       → ms-settings:appsfeatures
            "wifi"       → ms-settings:network-wifi
            "bluetooth"  → ms-settings:bluetooth
            "battery"    → ms-settings:batterysaver
            "sound"      → ms-settings:sound
        """
        uri_map = {
            "": "ms-settings:",
            "main": "ms-settings:",
            "network": "ms-settings:network",
            "wifi": "ms-settings:network-wifi",
            "ethernet": "ms-settings:network-ethernet",
            "vpn": "ms-settings:network-vpn",
            "bluetooth": "ms-settings:bluetooth",
            "display": "ms-settings:display",
            "sound": "ms-settings:sound",
            "apps": "ms-settings:appsfeatures",
            "battery": "ms-settings:batterysaver",
            "update": "ms-settings:windowsupdate",
            "privacy": "ms-settings:privacy",
            "personalize": "ms-settings:personalization",
            "accounts": "ms-settings:accounts",
        }
        uri = uri_map.get(panel.lower().strip(), uri_map[""])
        try:
            subprocess.Popen(["start", uri], shell=True)
            time.sleep(1.2)
            return {"ok": True, "panel": panel, "uri": uri}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    # ==================================================================
    # GENERIC — any app workflow
    # ==================================================================

    def workflow_open_type_save(
        self,
        app_command: str,
        expected_title: str,
        text_to_type: str,
        save_path: str = "",
        save_hotkey: tuple = ("ctrl", "s"),
    ) -> dict:
        """Generic workflow: launch app → focus → type text → optionally save.

        Works for ANY app that supports basic typing + Ctrl+S save dialog.
        Examples: WordPad, Sticky Notes, Code editors.
        """
        try:
            pwa = _get_pwa()
            pag = _get_pyautogui()
            subprocess.Popen(app_command, shell=True)
            time.sleep(1.5)
            try:
                app = pwa.Application(backend="uia").connect(title_re=f".*{expected_title}.*", timeout=10)
                window = app.top_window()
                window.set_focus()
            except Exception as e:
                return {"ok": False, "error": f"Window '{expected_title}' connect fail: {e}"}
            time.sleep(0.3)
            if text_to_type:
                pag.typewrite(text_to_type, interval=0.01)
            if save_path:
                time.sleep(0.4)
                pag.hotkey(*save_hotkey)
                time.sleep(1.5)
                abs_path = os.path.abspath(save_path)
                Path(abs_path).parent.mkdir(parents=True, exist_ok=True)
                pag.typewrite(abs_path, interval=0.01)
                time.sleep(0.3)
                pag.press("enter")
                time.sleep(1.5)
                # Replace? prompt
                pag.press("enter")
                time.sleep(0.5)
                if os.path.exists(abs_path):
                    return {"ok": True, "saved_to": abs_path}
                return {"ok": False, "error": "Save confirm — file nahi mili"}
            return {"ok": True, "typed_chars": len(text_to_type)}
        except Exception as e:
            return {"ok": False, "error": str(e)[:300]}

    # ==================================================================
    # APP-AGNOSTIC UIA query
    # ==================================================================

    def list_window_buttons(self, window_title: str, max_buttons: int = 50) -> dict:
        """Enumerate all buttons (clickable controls) in a window — useful
        for debugging "kya elements available hain is app mein".
        """
        try:
            pwa = _get_pwa()
            app = pwa.Application(backend="uia").connect(title_re=f".*{window_title}.*", timeout=8)
            window = app.top_window()
            buttons = []
            for el in window.descendants(control_type="Button"):
                try:
                    t = el.window_text() or ""
                    if t and len(buttons) < max_buttons:
                        buttons.append({"name": t})
                except Exception:
                    continue
            menus = []
            for el in window.descendants(control_type="MenuItem"):
                try:
                    t = el.window_text() or ""
                    if t and len(menus) < max_buttons:
                        menus.append({"name": t})
                except Exception:
                    continue
            edits = []
            for el in window.descendants(control_type="Edit"):
                try:
                    t = el.window_text() or "(empty)"
                    edits.append({"name": t})
                    if len(edits) >= 20:
                        break
                except Exception:
                    continue
            return {
                "ok": True,
                "window": window_title,
                "buttons": buttons,
                "menus": menus,
                "edits": edits,
            }
        except Exception as e:
            return {"ok": False, "error": str(e)[:300]}
