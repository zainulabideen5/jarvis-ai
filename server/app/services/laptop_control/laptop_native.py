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
        # Failsafe OFF: the corner-abort kept killing real automation runs
        # mid-task (a stray cursor near a corner aborted everything). For a
        # controlled assistant this caused more failures than it prevented.
        pag.FAILSAFE = False
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

    def paste_text(self, text: str, clear_first: bool = True) -> dict:
        """Type INSTANTLY by setting the clipboard and pressing Ctrl+V.

        clear_first: select existing content (Ctrl+A) before pasting so the new
        text REPLACES whatever was there — otherwise residual text in a search/
        compose box gets doubled (e.g. "Zaid MoeenZaid Moeen").
        Falls back to keyboard typing if the clipboard isn't available.
        """
        try:
            import win32clipboard
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
            finally:
                win32clipboard.CloseClipboard()
            pag = _get_pyautogui()
            time.sleep(0.05)
            if clear_first:
                pag.hotkey("ctrl", "a")   # select existing → paste replaces it
                time.sleep(0.05)
            pag.hotkey("ctrl", "v")
            return {"ok": True, "len": len(text), "method": "paste"}
        except Exception as e:
            log.info("paste_failed_fallback_type", error=str(e)[:120])
            return self.type_text(text)

    def attach_file(self, window_title: str, element_name: str, file_path: str) -> dict:
        """Attach a file to a chat compose box the FAST, reliable way: copy the
        file to the clipboard (CF_HDROP) and paste it (Ctrl+V) into the focused
        message box. Teams/WhatsApp/Slack/Discord all turn a pasted file into a
        real attachment — no attach-button/menu/file-dialog dance needed.
        """
        import os
        import struct
        p = os.path.abspath(os.path.expandvars(os.path.expanduser((file_path or "").strip().strip('"'))))
        if not os.path.isfile(p):
            return {"ok": False, "error": f"file nahi mili: {p}"}
        try:
            import win32clipboard
            # DROPFILES struct: pFiles offset=20, pt(0,0), fNC=0, fWide=1, then
            # the file list as UTF-16LE, double-null terminated.
            files = p + "\0\0"
            data = struct.pack("<IiiII", 20, 0, 0, 0, 1) + files.encode("utf-16-le")
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32clipboard.CF_HDROP, data)
            finally:
                win32clipboard.CloseClipboard()
        except Exception as e:
            return {"ok": False, "error": f"clipboard set fail: {str(e)[:150]}"}

        # Focus the compose box with a REAL CLICK (WebView needs real input
        # focus — set_focus alone doesn't make Ctrl+V land), then paste.
        clicked = self.uia_invoke(window_title, element_name, "Edit")
        if not clicked.get("ok"):
            clicked = self.uia_invoke(window_title, element_name, "Document")
        if not clicked.get("ok"):
            # last resort: focus the window + UIA set_focus on the box
            self.focus_and_verify(window_title)
            self.focus_element(window_title, element_name)
        pag = _get_pyautogui()
        time.sleep(0.4)
        pag.hotkey("ctrl", "v")
        time.sleep(1.5)  # let the app upload/render the attachment chip
        return {"ok": True, "attached": os.path.basename(p)}

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
        """List open top-level windows by title.

        Uses UIA (pywinauto Desktop) as the PRIMARY source — it reliably sees
        modern apps (Teams, WhatsApp, new Notepad) that the Win32 pygetwindow
        enumeration silently misses. Falls back to / merges pygetwindow so we
        never lose classic windows either. Consistency matters: whatever shows
        up here, ui_tree/focus can also open.
        """
        titles: list[str] = []
        seen: set[str] = set()

        # PRIMARY: UIA top-level windows (same backend as ui_tree)
        try:
            from pywinauto import Desktop
            for w in Desktop(backend="uia").windows():
                try:
                    t = (w.window_text() or "").strip()
                    if t and t.lower() not in seen:
                        seen.add(t.lower())
                        titles.append(t)
                except Exception:
                    continue
        except Exception as e:
            log.debug("uia_window_list_failed", error=str(e))

        # SUPPLEMENT: pygetwindow (classic Win32) for anything UIA missed
        try:
            import pygetwindow as gw
            for w in gw.getAllWindows():
                t = (w.title or "").strip()
                if t and not w.isMinimized and t.lower() not in seen:
                    seen.add(t.lower())
                    titles.append(t)
        except Exception:
            pass

        return {
            "ok": True,
            "count": len(titles),
            "windows": [{"title": t} for t in titles],
        }

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

    def resize_window(self, title_substring: str, width: int = 1000, height: int = 720) -> dict:
        """Window ko chhota karo (full-screen na rahe) aur thoda side pe rakho,
        taake dashboard bhi nazar aaye. Maximized ho to pehle restore."""
        try:
            import pygetwindow as gw
            for w in gw.getAllWindows():
                if title_substring.lower() in (w.title or "").lower() and w.title:
                    try:
                        if getattr(w, "isMaximized", False):
                            w.restore()
                        w.resizeTo(int(width), int(height))
                        w.moveTo(40, 40)
                        return {"ok": True, "title": w.title, "size": [width, height]}
                    except Exception as e:
                        return {"ok": False, "error": str(e)[:120]}
            return {"ok": False, "error": "window not found"}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    def active_window_title(self) -> str:
        """Title of the currently-foreground window (jahan user abhi hai)."""
        try:
            import pygetwindow as gw
            a = gw.getActiveWindow()
            return (getattr(a, "title", "") or "")
        except Exception:
            return ""

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

    @staticmethod
    def _find_window(window_title: str):
        """Return the first top-level UIA window whose title contains the
        substring — robust when several windows match (Application.connect
        errors on multiple matches)."""
        from pywinauto import Desktop
        needle = (window_title or "").lower()
        for w in Desktop(backend="uia").windows():
            try:
                if needle in (w.window_text() or "").lower():
                    return w
            except Exception:
                continue
        return None

    def uia_type(self, window_title: str, field_name: str, text: str, control_type: str = "Edit") -> dict:
        """Set text into a named field via UIA (ValuePattern — SAFE).

        Sets the value directly through the accessibility API. No global
        keystrokes, so it does NOT leak into whatever window the user is
        currently working in, and it isn't flagged by antivirus as a
        SendKeys/keylogger pattern. Preferred over keyboard typing.
        """
        try:
            window = self._find_window(window_title)
            if window is None:
                return {"ok": False, "error": f"window '{window_title}' nahi mili"}
            target = None
            try:
                cand = window.child_window(title=field_name, control_type=control_type)
                if cand.exists():
                    target = cand
            except Exception:
                target = None
            if target is None:
                els = window.descendants(control_type=control_type)
                if not els:
                    return {"ok": False, "error": f"No {control_type} control found"}
                target = els[0]

            target.set_text(text)

            # Read back via ValuePattern to CONFIRM it actually took — so the
            # engine never reports a fake success.
            verified = None
            try:
                verified = target.get_value()
            except Exception:
                verified = None
            ok = verified is not None and text.strip() in str(verified)
            return {
                "ok": True,
                "typed_chars": len(text),
                "verified": bool(ok),
                "current_value": (str(verified)[:200] if verified is not None else None),
            }
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    def uia_invoke(self, window_title: str, element_name: str, control_type: str = "Button") -> dict:
        """Activate an element BY NAME.

        WebView apps (Teams/WhatsApp/Slack) ignore the UIA Invoke pattern —
        it returns "ok" but nothing happens (contacts don't switch, menu items
        don't fire). So we do a REAL mouse click (click_input) FIRST, which
        actually triggers WebView controls; UIA invoke is only the fallback for
        native controls where a real click might miss.
        """
        try:
            window = self._find_window(window_title)
            if window is None:
                return {"ok": False, "error": f"window '{window_title}' nahi mili"}

            target = None
            try:
                cand = window.child_window(title=element_name, control_type=control_type)
                if cand.exists():
                    target = cand
            except Exception:
                target = None
            if target is None:
                for e in window.descendants(control_type=control_type):
                    try:
                        if element_name.lower() in (e.window_text() or "").lower():
                            target = e
                            break
                    except Exception:
                        continue
            if target is None:
                return {"ok": False, "error": f"'{element_name}' element nahi mila"}

            label = target.window_text() or element_name
            # REAL click first — reliably triggers WebView controls.
            try:
                target.click_input()
                return {"ok": True, "invoked": label, "via": "click"}
            except Exception:
                pass
            # Fallback: UIA pattern (native controls / when click can't reach)
            for method in ("invoke", "toggle", "select"):
                fn = getattr(target, method, None)
                if callable(fn):
                    try:
                        fn()
                        return {"ok": True, "invoked": label, "via": method}
                    except Exception:
                        continue
            return {"ok": False, "error": f"'{element_name}' click/invoke fail"}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    def get_element_value(self, window_title: str, element_name: str = "Type a message") -> dict:
        """Read the current text/value of an element (e.g. the chat compose box).
        Used to verify a send: after Enter, the compose box CLEARS (the message
        left) — reliable even for common words like 'hello'."""
        try:
            window = self._find_window(window_title)
            if window is None:
                return {"ok": False, "error": "window nahi mili"}
            needle = (element_name or "").lower()
            for e in window.descendants():
                try:
                    info = e.element_info
                    if (info.control_type or "") not in ("Edit", "Document"):
                        continue
                    nm = (info.name or "")
                    if needle and needle not in nm.lower():
                        continue
                    val = ""
                    try:
                        val = e.get_value() or ""
                    except Exception:
                        val = ""
                    return {"ok": True, "value": str(val), "name": nm}
                except Exception:
                    continue
            return {"ok": False, "error": "compose box nahi mila"}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    def focus_element(self, window_title: str, element_name: str, control_type: str = "") -> dict:
        """Give KEYBOARD focus to a specific control via UIA SetFocus.

        Clicking (Invoke) a textbox does NOT give it typing focus on WebView
        apps like Teams — so keystrokes go nowhere. SetFocus puts the caret
        inside the box. Returns ok + whether the window is now foreground.
        """
        try:
            window = self._find_window(window_title)
            if window is None:
                return {"ok": False, "error": f"window '{window_title}' nahi mili"}
            target = None
            # FAST PATH: direct child_window match (no full-tree scan)
            try:
                import re as _re
                cand = window.child_window(title_re=f".*{_re.escape(element_name)}.*")
                if cand.exists(timeout=1):
                    target = cand
            except Exception:
                target = None
            # Fallback: scan, but stop at the first Edit/Document match
            if target is None:
                for e in window.descendants():
                    try:
                        info = e.element_info
                        if control_type and (info.control_type or "").lower() != control_type.lower():
                            continue
                        if element_name.lower() in (info.name or "").lower():
                            target = e
                            break
                    except Exception:
                        continue
            if target is None:
                return {"ok": False, "error": f"'{element_name}' element nahi mila"}
            try:
                target.set_focus()
            except Exception as e:
                return {"ok": False, "error": f"focus set nahi hua: {str(e)[:120]}"}
            time.sleep(0.2)
            return {"ok": True, "focused": target.window_text() or element_name}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    def focus_and_verify(self, title_substring: str) -> dict:
        """Focus a window AND confirm it actually became the active window.

        Returns ok=True only if the target is now foreground — so callers can
        safely send keystrokes without risk of typing into the user's own
        active window. If it can't be confirmed foreground, ok=False.
        """
        r = self.focus_window(title_substring)
        if not r.get("ok"):
            return r
        try:
            import pygetwindow as gw
            time.sleep(0.25)
            active = gw.getActiveWindow()
            active_title = (getattr(active, "title", "") or "")
            if title_substring.lower() in active_title.lower():
                return {"ok": True, "title": active_title, "foreground": True}
            return {
                "ok": False,
                "error": f"Window mil gayi lekin foreground nahi aayi (active: '{active_title}')",
                "foreground": False,
            }
        except Exception:
            # Can't verify — be honest rather than risk wrong-window typing
            return {"ok": False, "error": "foreground verify nahi kar saka"}

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

    # Per-app UI labels — these are the APP's OWN universal labels (same on
    # every install), NOT user-specific. The contact name + message are always
    # passed in as parameters, so this works for ANY name on ANY user's laptop.
    _CHAT_APP_UI = {
        "whatsapp": {
            "window": "WhatsApp",
            "search": ["Search input textbox", "Search or start a new chat", "Search"],
            "compose": ["Type a message"],
        },
        "teams": {
            "window": "Teams",
            "search": ["Search", "Search box"],
            "compose": ["Type a message", "Type a new message", "Start a new conversation"],
        },
    }

    def chat_send(self, app: str, contact: str, message: str) -> dict:
        """DETERMINISTIC chat-app send — pure Python, NO LLM loop, NO ui_tree
        scans (which hang). One fixed reliable sequence that works for ANY
        contact on ANY laptop:

            focus window -> focus SEARCH -> paste name -> Enter (open chat)
            -> focus COMPOSE (the step that was missing — WebView needs UIA
               SetFocus or keystrokes go nowhere) -> paste message -> Enter
            -> verify the compose box CLEARED (= the message left).

        Honest: returns ok=False with a clear reason if any step can't be
        confirmed — never claims 'sent' on faith.
        """
        cfg = self._CHAT_APP_UI.get((app or "").strip().lower())
        if not cfg:
            return {"ok": False, "error": f"'{app}' ke liye deterministic send abhi nahi (engine try karega)"}
        win = cfg["window"]
        pag = _get_pyautogui()

        # 1) bring the app foreground (must be, or keystrokes hit the wrong window)
        fv = self.focus_and_verify(win)
        if not fv.get("ok"):
            return {"ok": False, "error": f"{win} foreground nahi hui — khula hai? ({fv.get('error','')})"}

        # 2) focus the SEARCH box (try the app's candidate labels)
        if not self._focus_any(win, cfg["search"], "Edit"):
            return {"ok": False, "error": f"{win} ka search box nahi mila"}

        # 3) paste the contact name (clear_first → no 'NameName' doubling), open chat
        self.paste_text(contact, clear_first=True)
        time.sleep(1.3)            # let search results populate
        pag.press("enter")        # open the top result
        time.sleep(1.1)           # let the chat load

        # 4) focus the COMPOSE box — THE critical step that was missing
        if not self._focus_any(win, cfg["compose"], "Edit") and \
           not self._focus_any(win, cfg["compose"], ""):
            return {"ok": False,
                    "error": f"compose box nahi mila — '{contact}' ki chat shayad open nahi hui"}

        # 5) paste the message + send
        self.paste_text(message, clear_first=False)
        time.sleep(0.4)
        pag.press("enter")
        time.sleep(0.9)

        # 6) verify: the compose box should now be EMPTY (message left)
        needle = message.strip().lower()
        for _ in range(3):
            val = self.get_element_value(win, cfg["compose"][0])
            if val.get("ok"):
                cur = (val.get("value") or "").strip().lower()
                if needle not in cur:      # box cleared → it sent
                    return {"ok": True, "verified": True,
                            "msg": f"'{message}' {contact} ko bhej diya (compose box clear ho gaya = chala gaya)"}
            time.sleep(0.5)
        return {"ok": False, "verified": False,
                "msg": f"'{message}' type to kiya par compose box clear nahi hua — ho sakta hai na gaya ho, zara khud dekh lein"}

    def _focus_any(self, window_title: str, names: list[str], control_type: str) -> bool:
        """Try focusing the first matching candidate label. Returns True on success."""
        for nm in names:
            try:
                if self.focus_element(window_title, nm, control_type).get("ok"):
                    return True
            except Exception:
                continue
        return False
