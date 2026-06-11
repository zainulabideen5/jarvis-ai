"""Multi-Gmail control via user's REGULAR Chrome — NO Playwright, NO background.

User has up to 14 Gmail accounts logged into single Chrome profile.
JARVIS:
    1. Launches Chrome with mail.google.com/mail/u/N/ URL for labeled account
    2. Waits for Gmail tab to appear (window title contains "Gmail")
    3. Activates that window (foreground — user sees it)
    4. Clicks Compose via UIA OR keyboard shortcut 'c'
    5. Types To/Subject/Body via keyboard
    6. Sends via Ctrl+Enter
    7. Verifies via UIA window text scan

FOREGROUND mode — Chrome window briefly active during send. User has accepted
this tradeoff explicitly. NO Playwright background mode used.

Persistent labels: shared chrome_accounts.json (same as multi_account.py).
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from app.core.logging import get_logger

log = get_logger(__name__)

_CONFIG_PATH = Path(__file__).resolve().parents[3] / "data" / "chrome_accounts.json"


def _load_config() -> dict:
    if not _CONFIG_PATH.exists():
        return {"gmail": []}
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"gmail": []}


def _save_config(data: dict) -> None:
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CONFIG_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _get_pyautogui():
    import pyautogui as pag
    pag.FAILSAFE = True
    pag.PAUSE = 0.1
    return pag


def _get_pwa():
    import pywinauto
    return pywinauto


def _launch_chrome_url(url: str) -> None:
    """Open URL in CHROME specifically (not default browser).

    Multi-Gmail labels are tied to user's Chrome profile — opening in Edge
    or Firefox would use a different cookie store + different accounts.
    We resolve chrome.exe from common install paths + Registry.
    """
    chrome_exe = _find_chrome_exe()
    if chrome_exe:
        try:
            subprocess.Popen([chrome_exe, url], shell=False)
            return
        except Exception as e:
            log.debug("chrome_launch_direct_fail", error=str(e)[:120])
    # Fallback to default-browser launch (may open in Edge — log it).
    log.info("chrome_exe_not_found_using_default_browser")
    try:
        subprocess.Popen(["cmd", "/c", "start", "", url], shell=False)
    except Exception:
        import webbrowser
        webbrowser.open(url, new=2)


def _find_chrome_exe() -> str:
    """Resolve the user's chrome.exe path. Returns "" if not found."""
    common = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    for p in common:
        if p and os.path.exists(p):
            return p
    # Try Windows Registry
    try:
        import winreg
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe") as k:
                    val, _ = winreg.QueryValueEx(k, "")
                    if val and os.path.exists(val):
                        return val
            except OSError:
                continue
    except Exception:
        pass
    return ""


class GmailChromeNative:
    """Multi-Gmail control via user's regular Chrome (foreground)."""

    _instance: "GmailChromeNative | None" = None

    @classmethod
    def get(cls) -> "GmailChromeNative":
        if cls._instance is None:
            cls._instance = GmailChromeNative()
        return cls._instance

    # ----- Label management (shared config) -----

    def list_labeled_accounts(self) -> list[dict]:
        return _load_config().get("gmail", [])

    def set_label(self, index: int, label: str, email: str = "") -> dict:
        cfg = _load_config()
        accounts = cfg.get("gmail", [])
        found = False
        for a in accounts:
            if a.get("index") == index:
                a["label"] = label
                if email:
                    a["email"] = email
                found = True
                break
        if not found:
            accounts.append({"index": index, "label": label, "email": email or ""})
        cfg["gmail"] = accounts
        _save_config(cfg)
        return {"ok": True, "accounts": accounts}

    def remove_label(self, index: int) -> dict:
        cfg = _load_config()
        accounts = [a for a in cfg.get("gmail", []) if a.get("index") != index]
        cfg["gmail"] = accounts
        _save_config(cfg)
        return {"ok": True, "remaining": len(accounts)}

    def resolve_label(self, label: str) -> int | None:
        if not label:
            return None
        label_low = label.strip().lower()
        for a in _load_config().get("gmail", []):
            if (a.get("label") or "").lower() == label_low:
                return int(a.get("index", -1))
        return None

    # ----- Open + send -----

    def open_account_sync(self, label_or_index, timeout_sec: float = 25.0) -> dict:
        """Launch Chrome tab for the labeled account, return when ready."""
        if isinstance(label_or_index, str):
            idx = self.resolve_label(label_or_index)
            if idx is None or idx < 0:
                return {"ok": False, "error": f"Label '{label_or_index}' set nahi hai"}
        else:
            idx = int(label_or_index)
        url = f"https://mail.google.com/mail/u/{idx}/"
        _launch_chrome_url(url)
        deadline = time.monotonic() + timeout_sec
        try:
            import pygetwindow as gw
            while time.monotonic() < deadline:
                for w in gw.getAllWindows():
                    t = (w.title or "").lower()
                    if "gmail" in t and any(b in t for b in ("chrome", "edge", "brave", "chromium")):
                        try:
                            if w.isMinimized:
                                w.restore()
                            w.activate()
                        except Exception:
                            pass
                        return {"ok": True, "title": w.title, "index": idx, "url": url}
                time.sleep(0.5)
        except Exception as e:
            return {"ok": False, "error": f"Window detect fail: {e}"}
        return {"ok": False, "error": f"Gmail u/{idx} window {timeout_sec}s mein nahi mili"}

    def _send_via_index_internal(
        self,
        idx: int,
        to: str,
        subject: str = "",
        body: str = "",
        attachment_path: str = "",
        timeout_sec: float = 90.0,
    ) -> dict:
        """Internal: send directly via account index — no label required.
        Used by email_sender when no label is configured (auto fallback to u/0).
        """
        return self._do_send(idx, to, subject, body, attachment_path, timeout_sec)

    def send_email_sync(
        self,
        label: str,
        to: str,
        subject: str = "",
        body: str = "",
        attachment_path: str = "",
        timeout_sec: float = 90.0,
    ) -> dict:
        """Send email via user's regular Chrome from a labeled account."""
        idx = self.resolve_label(label)
        if idx is None or idx < 0:
            return {
                "ok": False,
                "error": f"Label '{label}' set nahi hai. Pehle 'Gmail accounts detect karo' + 'Account 0 ko Personal label do' bolo.",
            }
        return self._do_send(idx, to, subject, body, attachment_path, timeout_sec)

    def _do_send(self, idx, to, subject, body, attachment_path, timeout_sec) -> dict:
        """Real send implementation — called by both labeled + index-direct paths."""
        # Use the shared window lock so we don't race WA send on activation.
        from app.services.laptop_control.wa_chrome_native import _WINDOW_ACTIVATION_LOCK as _LOCK
        with _LOCK:
            return self._do_send_locked(idx, to, subject, body, attachment_path, timeout_sec)

    def _do_send_locked(self, idx, to, subject, body, attachment_path, timeout_sec) -> dict:
        opened = self.open_account_sync(idx, timeout_sec=25.0)
        if not opened.get("ok"):
            return opened
        log.info("gmail_chrome_native_opened", index=idx, title=opened.get("title", ""))
        time.sleep(2.5)  # Gmail SPA mount

        pag = _get_pyautogui()
        pwa = _get_pwa()

        # Click Compose (UIA → keyboard shortcut fallback)
        compose_clicked = False
        try:
            app = pwa.Application(backend="uia").connect(title_re=r".*Gmail.*", timeout=8)
            window = app.top_window()
            for el in window.descendants(control_type="Button"):
                try:
                    nm = (el.element_info.name or "").lower()
                    if "compose" in nm or "new message" in nm:
                        el.click_input()
                        compose_clicked = True
                        break
                except Exception:
                    continue
            if not compose_clicked:
                for el in window.descendants():
                    try:
                        nm = (el.element_info.name or "").lower()
                        if nm == "compose":
                            el.click_input()
                            compose_clicked = True
                            break
                    except Exception:
                        continue
        except Exception as e:
            log.debug("gmail_compose_uia_fail", error=str(e)[:120])

        if not compose_clicked:
            # Try Gmail keyboard shortcut 'c' for Compose. WORKS only if
            # user has keyboard shortcuts enabled in Gmail Settings. If
            # OFF, 'c' just types literal 'c' somewhere — bad. So we
            # verify the compose dialog actually appears within 2 sec.
            try:
                pag.press("c")
            except Exception:
                pass
            # Verify compose dialog appeared
            time.sleep(1.5)
            try:
                app = pwa.Application(backend="uia").connect(title_re=r".*Gmail.*", timeout=4)
                window = app.top_window()
                # Look for "New Message" or "Compose" label in the active dialog
                dialog_open = False
                for el in window.descendants():
                    try:
                        nm = (el.element_info.name or "").lower()
                        if "new message" in nm or "subject" == nm or "to recipients" in nm:
                            dialog_open = True
                            break
                    except Exception:
                        continue
                if dialog_open:
                    compose_clicked = True
            except Exception:
                pass
        if not compose_clicked:
            return {
                "ok": False,
                "error": (
                    "Compose dialog nahi khuli. Gmail Settings mein keyboard "
                    "shortcuts ON karo (Settings → See all settings → General → "
                    "Keyboard shortcuts: ON), phir dobara try."
                ),
            }
        time.sleep(1.5)

        # Type To
        emails = [e.strip() for e in (to or "").replace(";", ",").split(",") if e.strip()]
        if not emails:
            return {"ok": False, "error": "Recipient empty"}
        try:
            for i, em in enumerate(emails):
                pag.typewrite(em, interval=0.02)
                time.sleep(0.7)
                pag.press("escape")  # dismiss autocomplete
                time.sleep(0.2)
                if i < len(emails) - 1:
                    pag.typewrite(", ", interval=0.02)
            pag.press("tab")
            time.sleep(0.4)
        except Exception as e:
            return {"ok": False, "error": f"To field fail: {e}"}

        # Subject
        if subject:
            pag.typewrite(subject, interval=0.01)
        pag.press("tab")
        time.sleep(0.3)

        # Body
        if body:
            try:
                pag.typewrite(body, interval=0.008)
            except Exception:
                try:
                    import pyperclip
                    pyperclip.copy(body)
                    pag.hotkey("ctrl", "v")
                except Exception as e:
                    return {"ok": False, "error": f"Body fail: {e}"}
        time.sleep(0.4)

        # Send via Ctrl+Enter (works regardless of UI)
        pag.hotkey("ctrl", "enter")

        # Verify — poll for "Message sent" via UIA
        verified = False
        for _ in range(8):
            time.sleep(1.0)
            try:
                app = pwa.Application(backend="uia").connect(title_re=r".*Gmail.*", timeout=2)
                window = app.top_window()
                txt = (window.window_text() or "").lower()
                if "message sent" in txt or "conversation marked" in txt:
                    verified = True
                    break
            except Exception:
                continue

        if attachment_path:
            return {
                "ok": True,
                "tier": "chrome-native",
                "from_index": idx,
                "to": ", ".join(emails),
                "verified": verified,
                "warning": "Attachment skipped — native mode supports text-only.",
            }
        return {
            "ok": True,
            "tier": "chrome-native",
            "from_index": idx,
            "to": ", ".join(emails),
            "verified": verified,
        }
