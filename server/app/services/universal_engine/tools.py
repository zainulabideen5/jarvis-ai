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


def type_in_window(window_title: str, text: str, element_name: str = "", submit: bool = False) -> dict:
    """Keyboard se type karo — chat apps (Teams/WhatsApp) ke message box ke liye.

    element_name DO (jaise "Type a message") — woh box ko UIA se FOCUS karega
    phir type karega (WebView apps mein click se focus nahi milta).
    submit=True DO to type ke FAURAN BAAD Enter bhi dabata hai — usi focus pe,
    bina dobara window focus kiye (warna message box se caret hat jaata aur
    Enter kaam nahi karta). Chat message bhejne ke liye yehi ek call kaafi hai.
    """
    from app.services.laptop_control.laptop_native import LaptopNative
    if not window_title:
        return {"ok": False, "error": "window_title zaroori hai — blind typing safe nahi"}
    if SecurityBlocker.is_app_blocked(window_title):
        return {"ok": False, "error": "blocked: sensitive app"}
    if SecurityBlocker.has_sensitive_keyword(text):
        return {"ok": False, "error": "blocked: sensitive content (password/pin/otp)"}
    nat = LaptopNative.get()
    if element_name:
        # Focus the specific control (gives the caret to it — reliable on WebView)
        fe = nat.focus_element(window_title, element_name)
        if not fe.get("ok"):
            # fall back to window focus
            fg = nat.focus_and_verify(window_title)
            if not fg.get("ok"):
                return {"ok": False, "error": f"focus fail: {fe.get('error')}"}
    else:
        fg = nat.focus_and_verify(window_title)
        if not fg.get("ok"):
            return {"ok": False, "error": f"safe type fail: {fg.get('error')}"}
    r = nat.paste_text(text)   # instant clipboard paste (not char-by-char)
    if r.get("ok") and submit:
        # Enter on the SAME focus — no re-focus (which would lose the caret).
        import time as _t
        _t.sleep(0.2)
        enter = nat.press_key("enter")
        r["submitted"] = bool(enter.get("ok"))
    return r


def attach_file(window_title: str, element_name: str, file_path: str) -> dict:
    """File ko chat compose box mein PASTE karo (jaise insaan Ctrl+V karta hai).
    File clipboard pe aati hai, message box pe real click se focus, phir Ctrl+V.
    Har chat app pe same (Teams/WhatsApp/Slack). element_name = message box ka
    naam (jaise "Type a message"). Iske baad press_keys enter se bhejo.
    """
    from app.services.laptop_control.laptop_native import LaptopNative
    if SecurityBlocker.is_app_blocked(window_title):
        return {"ok": False, "error": "blocked: sensitive app"}
    return LaptopNative.get().attach_file(window_title, element_name, file_path)


def pick_file_in_dialog(file_path: str) -> dict:
    """Windows "Open" file dialog mein file chuno — agar paste na chale to:
    attach button dabane ke BAAD jo OS dialog khulta hai usme path daal kar Open.
    """
    import os
    from app.services.laptop_control.laptop_native import LaptopNative
    p = os.path.abspath(os.path.expandvars(os.path.expanduser((file_path or "").strip().strip('"'))))
    if not os.path.isfile(p):
        return {"ok": False, "error": f"file nahi mili: {p}"}
    nat = LaptopNative.get()
    # The OS file dialog title is usually "Open" (sometimes localized).
    for dlg in ("Open", "Choose File to Upload", "Select"):
        win = nat._find_window(dlg)
        if win is not None:
            # Set the path into the "File name" edit (ValuePattern — native, safe)
            r = nat.uia_type(dlg, "File name", p, control_type="Edit")
            if not r.get("ok"):
                # fallback: first Edit in the dialog
                r = nat.uia_type(dlg, "", p, control_type="Edit")
            # Click "Open"
            nat.uia_invoke(dlg, "Open", "Button")
            import time as _t
            _t.sleep(0.6)
            return {"ok": True, "picked": os.path.basename(p), "dialog": dlg}
    return {"ok": False, "error": "file dialog (Open) nahi mila — pehle attach button dabao"}


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
    """App kholo (naam ya exe path). Khulne ke baad window ko chhoti karta hai
    (full-screen na rahe) taake dashboard bhi nazar aaye."""
    from app.services.laptop_control.apps import AppController
    from app.services.laptop_control.laptop_native import LaptopNative
    if SecurityBlocker.is_app_blocked(name):
        return {"ok": False, "error": "blocked: sensitive app"}
    ok, msg = AppController.open_app(name)
    if ok:
        import time as _t
        _t.sleep(1.5)
        try:
            LaptopNative.get().resize_window(name)
        except Exception:
            pass
    return {"ok": ok, "message": msg}


def resize_window(title: str, width: int = 1000, height: int = 720) -> dict:
    """Kisi window ko chhota karo (full-screen na rahe)."""
    from app.services.laptop_control.laptop_native import LaptopNative
    return LaptopNative.get().resize_window(title, width, height)


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


# ----------------------------------------------------------------------
# Reversible (UNDOABLE) file/folder tools — file ops ke liye YEH use karo
# (powershell se NAHI), taake user "undo karo"/"redo karo" kar sake. General.
# ----------------------------------------------------------------------
def create_file(path: str, content: str = "") -> dict:
    """Nayi file banao (UNDOABLE)."""
    import os
    from app.services.undo_history import UndoHistory
    try:
        path = os.path.abspath(os.path.expandvars(os.path.expanduser(path)))
        if os.path.exists(path):
            return {"ok": False, "error": "file pehle se maujood hai"}
        if os.path.dirname(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content or "")
        UndoHistory.record({"kind": "create", "path": path,
                            "desc": f"file banai: {os.path.basename(path)}"})
        return {"ok": True, "path": path}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


def create_excel(path: str, data=None, sheet_name: str = "Sheet1") -> dict:
    """Excel (.xlsx) file SEEDHE disk pe banao — Excel khole BAGHAIR (BACKGROUND,
    koi window nahi). General: koi bhi data, kisi bhi user ke laptop pe (openpyxl).

    data: rows ki list. Har row ya to list/tuple (cells) ho, ya dict (keys =
    column headers — pehli row headers ban jaati hai). UNDOABLE.
    """
    import os
    from app.services.undo_history import UndoHistory
    try:
        from openpyxl import Workbook
    except ImportError:
        return {"ok": False, "error": "openpyxl install nahi (pip install openpyxl)"}
    try:
        path = os.path.abspath(os.path.expandvars(os.path.expanduser(path)))
        if not path.lower().endswith(".xlsx"):
            path += ".xlsx"
        if os.path.exists(path):
            return {"ok": False, "error": "file pehle se maujood hai"}
        if os.path.dirname(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
        wb = Workbook()
        ws = wb.active
        ws.title = (str(sheet_name) or "Sheet1")[:31]
        rows = data or []
        if rows and isinstance(rows[0], dict):
            headers = list(rows[0].keys())
            ws.append(headers)
            for d in rows:
                ws.append([d.get(h, "") for h in headers])
        else:
            for r in rows:
                ws.append(list(r) if isinstance(r, (list, tuple)) else [r])
        wb.save(path)
        UndoHistory.record({"kind": "create", "path": path,
                            "desc": f"excel banai: {os.path.basename(path)}"})
        return {"ok": True, "path": path, "rows": len(rows)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


def create_folder(path: str) -> dict:
    """Naya folder banao (UNDOABLE)."""
    import os
    from app.services.undo_history import UndoHistory
    try:
        path = os.path.abspath(os.path.expandvars(os.path.expanduser(path)))
        if os.path.exists(path):
            return {"ok": False, "error": "folder pehle se maujood hai"}
        os.makedirs(path)
        UndoHistory.record({"kind": "create", "path": path,
                            "desc": f"folder banaya: {os.path.basename(path)}"})
        return {"ok": True, "path": path}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


def move_path(src: str, dst: str) -> dict:
    """File/folder ko move ya rename karo (UNDOABLE)."""
    import os
    import shutil
    from app.services.undo_history import UndoHistory
    try:
        src = os.path.abspath(os.path.expandvars(os.path.expanduser(src)))
        dst = os.path.abspath(os.path.expandvars(os.path.expanduser(dst)))
        if not os.path.exists(src):
            return {"ok": False, "error": "source nahi mila"}
        if os.path.isdir(dst):
            dst = os.path.join(dst, os.path.basename(src))
        if os.path.dirname(dst):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(src, dst)
        UndoHistory.record({"kind": "move", "src": src, "dst": dst,
                            "desc": f"move/rename: {os.path.basename(src)}"})
        return {"ok": True, "from": src, "to": dst}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


def delete_path(path: str) -> dict:
    """File/folder ko RECOVERABLE trash mein bhejo (permanent delete NAHI) — UNDOABLE."""
    import os
    import shutil
    from app.services.undo_history import UndoHistory, trash_path_for
    try:
        path = os.path.abspath(os.path.expandvars(os.path.expanduser(path)))
        if not os.path.exists(path):
            return {"ok": False, "error": "path nahi mila"}
        trash = trash_path_for(path)
        if os.path.dirname(trash):
            os.makedirs(os.path.dirname(trash), exist_ok=True)
        shutil.move(path, trash)
        UndoHistory.record({"kind": "delete", "orig": path, "trash": trash,
                            "desc": f"delete (recoverable): {os.path.basename(path)}"})
        return {"ok": True, "deleted": path, "recoverable_at": trash}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


# ======================================================================
# Registry — what the brain sees
# ======================================================================

TOOLS = {
    "create_file": create_file,
    "create_excel": create_excel,
    "create_folder": create_folder,
    "move_path": move_path,
    "delete_path": delete_path,
    "list_windows": list_windows,
    "focus_window": focus_window,
    "ui_tree": ui_tree,
    "click_element": click_element,
    "set_text": set_text,
    "type_in_window": type_in_window,
    "attach_file": attach_file,
    "pick_file_in_dialog": pick_file_in_dialog,
    "press_keys": press_keys,
    "open_app": open_app,
    "resize_window": resize_window,
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
- type_in_window {"window_title": "...", "element_name": "Type a message", "text": "...", "submit": true} — chat box mein type karo. element_name DO (box ka naam) taake focus mile. submit:true DO to type ke baad Enter bhi dab jaata hai (message bhej deta hai) — chat ke liye yeh EK call kaafi hai, alag press_keys ki zaroorat nahi
- attach_file {"window_title": "...", "element_name": "Type a message", "file_path": "..."} — file ko compose box mein PASTE karo (clipboard + real-click focus + Ctrl+V). File bhejne ka SABSE SEEDHA tareeqa. Iske baad press_keys enter se bhejo
- pick_file_in_dialog {"file_path": "..."} — AGAR paste na chale: attach button dabane ke baad jo "Open" dialog khulta hai usme path daal kar Open
- press_keys {"keys": ["ctrl","l"], "window_title": "..."} — hotkey/key (enter, tab, esc, f5). App-level keys ke liye window_title do taake sahi window mein jayein
- open_app {"name": "chrome|teams|whatsapp|notepad|..."} — app launch (khulne ke baad window chhoti ho jaati hai, full-screen nahi)
- resize_window {"title": "...", "width": 1000, "height": 720} — window ko chhota karo agar full-screen ho
- close_app {"name": "..."} — app band
- open_url {"url": "https://..."} — user ke default browser mein URL
- create_file {"path":"...","content":"..."} — nayi text file banao (UNDOABLE)
- create_excel {"path":"...","data":[["Name","Age"],["Zain","25"]],"sheet_name":"Sheet1"} — Excel (.xlsx) SEEDHE banao, Excel khole BAGHAIR (BACKGROUND). data = rows ki list (list-of-lists, ya list-of-dicts jisme keys=columns). Spreadsheet/table/data ke liye YEH use karo — Excel GUI nahi (UNDOABLE)
- create_folder {"path":"..."} — naya folder banao (UNDOABLE)
- move_path {"src":"...","dst":"..."} — file/folder move ya rename (UNDOABLE)
- delete_path {"path":"..."} — file/folder ko RECOVERABLE trash mein bhejo (permanent NAHI) (UNDOABLE)
  ★ FILE/FOLDER create/move/rename/delete ke liye HAMESHA yeh 4 tools use karo — powershell se NAHI — taake user baad mein "undo karo" kar sake. (powershell sirf reading/listing/info ke liye.)
- powershell {"command": "..."} — files/system ki READING/info/listing (process, settings query). File MODIFY (create/move/delete) yahan se MAT karo — upar wale undoable tools use karo. Input-injection (SendKeys) BLOCKED hai
- find_files {"query": "...", "location": "Desktop?"} — file dhundo
- screenshot_check {"query": "..."} — LAST RESORT vision (sirf jab ui_tree fail ho)
- wait {"seconds": 2} — load hone ka intezar
""".strip()
