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
    # Input-injection via PowerShell — antivirus (rightly) flags these as
    # keylogger/SendKeys malware patterns. The engine must use the dedicated
    # UIA tools (set_text / invoke_element / type_in_window) instead.
    "sendkeys", "system.windows.forms", "add-type", "sendinput",
    "windows.input", "[microsoft.visualbasic", "setcursorpos", "mouse_event",
    "keybd_event",
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
        descendants = win.descendants()

        def _collect(ctype_filter: str, name_filter: str) -> list[dict]:
            out = []
            for el in descendants:
                try:
                    info = el.element_info
                    ctype = info.control_type or ""
                    name = (info.name or "").strip()
                    if ctype_filter and ctype.lower() != ctype_filter.lower():
                        continue
                    if name_filter and name_filter.lower() not in name.lower():
                        continue
                    if not name and ctype not in ("Edit", "Document"):
                        continue  # unnamed decoration — useless to the brain
                    entry = {"type": ctype, "name": name[:80], "enabled": bool(info.enabled)}
                    if ctype in ("Edit", "Document"):
                        try:
                            val = el.get_value()
                            if val:
                                entry["value"] = str(val)[:120]
                        except Exception:
                            pass
                    out.append(entry)
                    if len(out) >= MAX_TREE_ELEMENTS:
                        break
                except Exception:
                    continue
            return out

        elements = _collect(control_type, name_contains)

        # FORGIVING FALLBACK: if a filter matched nothing, the model probably
        # guessed the control_type/name wrong (e.g. a contact is a "TreeItem"
        # not a "ListItem"). Don't return an empty tree (that blinds the model
        # and makes it ask the user) — return the FULL window instead with a note.
        note = ""
        if not elements and (control_type or name_contains):
            elements = _collect("", "")
            note = (
                f"(filter control_type='{control_type}' name_contains='{name_contains}' "
                f"se kuch nahi mila — neeche poori window hai, isme se sahi element chuno)"
            )

        lines = []
        for i, e in enumerate(elements):
            line = f"{i}. [{e['type']}] {e['name']}"
            if e.get("value"):
                line += f"  value=\"{e['value']}\""
            if not e["enabled"]:
                line += " (disabled)"
            lines.append(line)
        return {
            "ok": True,
            "window": win.window_text(),
            "count": len(elements),
            "note": note,
            "elements": _clip("\n".join(lines)),
            "truncated": len(elements) >= MAX_TREE_ELEMENTS,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


def click_element(window_title: str, element_name: str, control_type: str = "Button") -> dict:
    """Element par click — SAFE (UIA Invoke pattern, bina mouse/foreground churaye)."""
    from app.services.laptop_control.laptop_native import LaptopNative
    if SecurityBlocker.is_app_blocked(window_title):
        return {"ok": False, "error": "blocked: sensitive app"}
    return LaptopNative.get().uia_invoke(window_title, element_name, control_type)


def set_text(window_title: str, element_name: str, text: str, control_type: str = "Edit") -> dict:
    """Kisi field mein text daalo — SAFE (UIA ValuePattern, keystrokes nahi).

    Yeh PREFERRED tareeqa hai text daalne ka: user ki active window mein leak
    nahi hota aur antivirus flag nahi karta. element_name ui_tree se lo.
    """
    from app.services.laptop_control.laptop_native import LaptopNative
    if SecurityBlocker.is_app_blocked(window_title):
        return {"ok": False, "error": "blocked: sensitive app"}
    if SecurityBlocker.has_sensitive_keyword(text):
        return {"ok": False, "error": "blocked: sensitive content (password/pin/otp)"}
    return LaptopNative.get().uia_type(window_title, element_name, text, control_type)


def type_in_window(window_title: str, text: str) -> dict:
    """Keyboard se type karo — SIRF jab set_text na chale (rich editors).

    Pehle target window ko foreground laata + VERIFY karta hai; agar foreground
    confirm na ho to type NAHI karta (taake user ki apni window mein keys leak
    na hon). window_title zaroori hai — blind typing allowed nahi.
    """
    from app.services.laptop_control.laptop_native import LaptopNative
    if not window_title:
        return {"ok": False, "error": "window_title zaroori hai — blind typing safe nahi"}
    if SecurityBlocker.is_app_blocked(window_title):
        return {"ok": False, "error": "blocked: sensitive app"}
    if SecurityBlocker.has_sensitive_keyword(text):
        return {"ok": False, "error": "blocked: sensitive content (password/pin/otp)"}
    nat = LaptopNative.get()
    fg = nat.focus_and_verify(window_title)
    if not fg.get("ok"):
        return {"ok": False, "error": f"safe type fail: {fg.get('error')}"}
    return nat.type_text(text)


def press_keys(keys: list, window_title: str = "") -> dict:
    """Hotkey/key. SAFE: agar window_title diya to use foreground laa kar verify
    karke bhejta hai (warna user ki window mein keys ja sakti hain).
    E.g. ["ctrl","l"], ["enter"], ["win","r"]."""
    from app.services.laptop_control.laptop_native import LaptopNative
    if not keys:
        return {"ok": False, "error": "keys khali hai"}
    nat = LaptopNative.get()
    # Global hotkeys (win/ctrl+esc style) may legitimately have no window;
    # but app-level keys must target a verified-foreground window.
    if window_title:
        if SecurityBlocker.is_app_blocked(window_title):
            return {"ok": False, "error": "blocked: sensitive app"}
        fg = nat.focus_and_verify(window_title)
        if not fg.get("ok"):
            return {"ok": False, "error": f"safe keys fail: {fg.get('error')}"}
    if len(keys) == 1:
        return nat.press_key(keys[0])
    return nat.hotkey(*keys)


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
    "set_text": set_text,
    "type_in_window": type_in_window,
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
- focus_window {"title": "..."} — window foreground mein lao
- ui_tree {"window_title": "...", "control_type": "?", "name_contains": "?"} — window ke elements dekho (AI ki aankhein). Buttons/Edits/MenuItems sab naam ke saath
- click_element {"window_title": "...", "element_name": "...", "control_type": "Button|MenuItem|Hyperlink|TabItem|ListItem"} — element activate (SAFE: UIA invoke, mouse churaye bina)
- set_text {"window_title": "...", "element_name": "...", "text": "...", "control_type": "Edit"} — field mein text daalo. YEH PREFERRED hai text ke liye — keyboard nahi chalata, user ki window mein leak nahi hota, antivirus flag nahi karta
- type_in_window {"window_title": "...", "text": "..."} — keyboard se type, SIRF jab set_text na chale (rich editors). Window ko verify-foreground laa kar type karta hai
- press_keys {"keys": ["ctrl","l"], "window_title": "..."} — hotkey/key (enter, tab, esc, f5). App-level keys ke liye window_title do taake sahi window mein jayein
- open_app {"name": "chrome|teams|notepad|excel|..."} — app launch
- close_app {"name": "..."} — app band
- open_url {"url": "https://..."} — user ke default browser mein URL
- powershell {"command": "..."} — files/system ka sab kaam (rename, move, list, create). Input-injection (SendKeys) BLOCKED hai — typing ke liye set_text/type_in_window use karo
- find_files {"query": "...", "location": "Desktop?"} — file dhundo
- screenshot_check {"query": "..."} — LAST RESORT vision (sirf jab ui_tree fail ho)
- wait {"seconds": 2} — load hone ka intezar
""".strip()
