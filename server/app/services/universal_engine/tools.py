"""Universal tools — the engine's hands. No per-app code.

Every tool returns a dict with at least {"ok": bool}. Results are fed back
to the brain as observations, so keep them text-friendly and bounded.
"""

from __future__ import annotations

import subprocess

from app.core.logging import get_logger
from app.services.laptop_control.security import SecurityBlocker

log = get_logger(__name__)

MAX_RESULT_CHARS = 4000
MAX_TREE_ELEMENTS = 120

# PowerShell patterns that are never OK regardless of consent.
_PS_HARD_BLOCK = (
    "format-volume", "format c:", "diskpart", "stop-computer",
    "restart-computer", "remove-item c:\\windows", "remove-item c:/windows",
    "reg delete hklm", "bcdedit", "cipher /w",
)


def _clip(s: str, limit: int = MAX_RESULT_CHARS) -> str:
    s = s or ""
    return s if len(s) <= limit else s[:limit] + f"\n...[{len(s) - limit} chars truncated]"


# ======================================================================
# Tool implementations (sync — engine runs them in a thread)
# ======================================================================

def list_windows() -> dict:
    """Saari khuli windows (titles)."""
    from app.services.laptop_control.laptop_native import LaptopNative
    return LaptopNative.get().list_open_windows()


def focus_window(title: str) -> dict:
    """Window ko foreground mein lao (title ka hissa kaafi hai)."""
    from app.services.laptop_control.laptop_native import LaptopNative
    if SecurityBlocker.is_app_blocked(title):
        return {"ok": False, "error": "blocked: sensitive app"}
    return LaptopNative.get().focus_window(title)


def ui_tree(window_title: str, control_type: str = "", name_contains: str = "") -> dict:
    """AI ki aankhein — window ke saare UI elements text mein.

    Returns elements as: index | control_type | name | enabled.
    Filter with control_type (e.g. 'Button', 'Edit', 'MenuItem', 'Hyperlink',
    'TabItem', 'ListItem', 'Document') and/or name_contains.
    """
    try:
        from pywinauto import Desktop

        needle = (window_title or "").lower()
        matches = [
            w for w in Desktop(backend="uia").windows()
            if needle in (w.window_text() or "").lower()
        ]
        if not matches:
            return {"ok": False, "error": f"window '{window_title}' nahi mili"}
        # Multiple matches: take the first (foreground-most) one
        win = matches[0]

        elements = []
        for el in win.descendants():
            try:
                info = el.element_info
                ctype = info.control_type or ""
                name = (info.name or "").strip()
                if control_type and ctype.lower() != control_type.lower():
                    continue
                if name_contains and name_contains.lower() not in name.lower():
                    continue
                if not name and ctype not in ("Edit", "Document"):
                    continue  # unnamed decoration — useless to the brain
                elements.append({
                    "type": ctype,
                    "name": name[:80],
                    "enabled": bool(info.enabled),
                })
                if len(elements) >= MAX_TREE_ELEMENTS:
                    break
            except Exception:
                continue

        lines = [
            f"{i}. [{e['type']}] {e['name']}" + ("" if e["enabled"] else " (disabled)")
            for i, e in enumerate(elements)
        ]
        return {
            "ok": True,
            "window": window_title,
            "count": len(elements),
            "elements": _clip("\n".join(lines)),
            "truncated": len(elements) >= MAX_TREE_ELEMENTS,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


def click_element(window_title: str, element_name: str, control_type: str = "Button") -> dict:
    """Window ke element par click (naam se — ui_tree se naam lo)."""
    from app.services.laptop_control.laptop_native import LaptopNative
    if SecurityBlocker.is_app_blocked(window_title):
        return {"ok": False, "error": "blocked: sensitive app"}
    return LaptopNative.get().uia_click(window_title, element_name, control_type)


def type_text(text: str) -> dict:
    """Foreground window mein type karo (pehle focus_window zaroor)."""
    from app.services.laptop_control.laptop_native import LaptopNative
    if SecurityBlocker.has_sensitive_keyword(text):
        return {"ok": False, "error": "blocked: sensitive content (password/pin/otp)"}
    return LaptopNative.get().type_text(text)


def press_keys(keys: list) -> dict:
    """Hotkey ya single key. E.g. ["ctrl","l"], ["enter"], ["win","r"]."""
    from app.services.laptop_control.laptop_native import LaptopNative
    if not keys:
        return {"ok": False, "error": "keys khali hai"}
    if len(keys) == 1:
        return LaptopNative.get().press_key(keys[0])
    return LaptopNative.get().hotkey(*keys)


def open_app(name: str) -> dict:
    """App kholo (naam ya exe path)."""
    from app.services.laptop_control.apps import AppController
    if SecurityBlocker.is_app_blocked(name):
        return {"ok": False, "error": "blocked: sensitive app"}
    ok, msg = AppController.open_app(name)
    return {"ok": ok, "message": msg}


def close_app(name: str) -> dict:
    """App band karo."""
    from app.services.laptop_control.apps import AppController
    ok, msg = AppController.close_app(name)
    return {"ok": ok, "message": msg}


def open_url(url: str) -> dict:
    """Default browser (user ka apna Chrome) mein URL kholo."""
    from app.services.laptop_control.apps import AppController
    ok, msg = AppController.open_url(url)
    return {"ok": ok, "message": msg}


def powershell(command: str) -> dict:
    """PowerShell command chalao — files/system ka workhorse.

    Use for: file rename/move/copy/list, folder ops, process info,
    settings queries. Output capped at 4000 chars.
    """
    low = (command or "").lower()
    for pat in _PS_HARD_BLOCK:
        if pat in low:
            return {"ok": False, "error": f"blocked: dangerous command ({pat})"}
    if SecurityBlocker.has_sensitive_keyword(low):
        return {"ok": False, "error": "blocked: sensitive keyword"}
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        return {
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "stdout": _clip(out),
            "stderr": _clip(err, 1000),
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "powershell timeout (60s)"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


def find_files(query: str, location: str = "") -> dict:
    """File/folder dhundo (Desktop, Documents, Downloads waghaira mein)."""
    from app.services.laptop_control.files import FileOperations
    try:
        results = FileOperations.find_files(query, location=location or None)
        if not results:
            return {"ok": True, "count": 0, "results": "kuch nahi mila"}
        lines = [
            f"{i+1}. {'[DIR] ' if r.get('is_folder') else ''}{r['name']} — {r['path']}"
            for i, r in enumerate(results[:15])
        ]
        return {"ok": True, "count": len(results), "results": _clip("\n".join(lines))}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


def screenshot_check(query: str) -> dict:
    """LAST RESORT — screen ka screenshot le kar vision LLM se sawal poocho.

    Sirf tab jab ui_tree kaam na kare (games, custom-render apps).
    """
    from app.core.config import ServerConfig
    from app.services.laptop_control.vision import VisionDriver
    try:
        ok, msg = VisionDriver(ServerConfig()).analyze(query)
        return {"ok": ok, "answer": _clip(msg, 2000)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


def wait(seconds: float) -> dict:
    """Thoda ruk jao (app/page load ke liye). Max 8s."""
    import time
    s = max(0.2, min(float(seconds or 1), 8.0))
    time.sleep(s)
    return {"ok": True, "waited": s}


def _re_escape(s: str) -> str:
    import re
    return re.escape(s or "")


# ======================================================================
# Registry — what the brain sees
# ======================================================================

TOOLS = {
    "list_windows": list_windows,
    "focus_window": focus_window,
    "ui_tree": ui_tree,
    "click_element": click_element,
    "type_text": type_text,
    "press_keys": press_keys,
    "open_app": open_app,
    "close_app": close_app,
    "open_url": open_url,
    "powershell": powershell,
    "find_files": find_files,
    "screenshot_check": screenshot_check,
    "wait": wait,
}

TOOLS_DOC = """
- list_windows {} — saari khuli windows ke titles
- focus_window {"title": "..."} — window foreground mein lao (typing/click se PEHLE)
- ui_tree {"window_title": "...", "control_type": "?", "name_contains": "?"} — window ke elements dekho (AI ki aankhein). Buttons/Edits/MenuItems sab naam ke saath
- click_element {"window_title": "...", "element_name": "...", "control_type": "Button|MenuItem|Edit|Hyperlink|TabItem|ListItem"} — element par click
- type_text {"text": "..."} — foreground mein type karo
- press_keys {"keys": ["ctrl","l"]} — hotkey/single key (enter, tab, win, esc, f5...)
- open_app {"name": "chrome|notepad|excel|..."} — app launch
- close_app {"name": "..."} — app band
- open_url {"url": "https://..."} — user ke default browser mein URL
- powershell {"command": "..."} — files/system ka sab kaam (rename, move, list, create...)
- find_files {"query": "...", "location": "Desktop?"} — file dhundo
- screenshot_check {"query": "..."} — LAST RESORT vision (sirf jab ui_tree fail ho)
- wait {"seconds": 2} — load hone ka intezar
""".strip()
