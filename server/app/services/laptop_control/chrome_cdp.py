"""Chrome DevTools Protocol — connect to user's existing Chrome in debug mode.

Allows BACKGROUND control of Chrome tabs (WhatsApp Web, etc.) without
focusing windows or disturbing user.

Requires Chrome to be launched with: --remote-debugging-port=9222
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import requests

from app.core.logging import get_logger

log = get_logger(__name__)

DEBUG_PORT = 9222
CDP_URL = f"http://localhost:{DEBUG_PORT}"


def is_debug_running() -> bool:
    """Check if Chrome is running with debug port enabled."""
    try:
        r = requests.get(f"{CDP_URL}/json/version", timeout=2)
        return r.status_code == 200
    except Exception:
        return False


def list_tabs() -> list[dict]:
    """List all open Chrome tabs."""
    try:
        r = requests.get(f"{CDP_URL}/json", timeout=3)
        if r.status_code == 200:
            return [t for t in r.json() if t.get("type") == "page"]
    except Exception as e:
        log.debug("cdp_list_tabs_failed", error=str(e))
    return []


def find_tab_by_url(url_substring: str) -> dict | None:
    """Find first tab matching URL substring."""
    for tab in list_tabs():
        if url_substring.lower() in tab.get("url", "").lower():
            return tab
    return None


_BROWSER_CANDIDATES: list[tuple[str, list[str]]] = [
    # (label, candidate paths) — Chromium-derivatives all support --remote-debugging-port
    ("Google Chrome", [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expanduser(r"~\AppData\Local\Google\Chrome\Application\chrome.exe"),
    ]),
    ("Microsoft Edge", [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]),
    ("Brave", [
        r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
        r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe",
        os.path.expanduser(r"~\AppData\Local\BraveSoftware\Brave-Browser\Application\brave.exe"),
    ]),
    ("Vivaldi", [
        r"C:\Program Files\Vivaldi\Application\vivaldi.exe",
        os.path.expanduser(r"~\AppData\Local\Vivaldi\Application\vivaldi.exe"),
    ]),
    ("Chromium", [
        r"C:\Program Files\Chromium\Application\chrome.exe",
        os.path.expanduser(r"~\AppData\Local\Chromium\Application\chrome.exe"),
    ]),
]


def _registry_browser_exe() -> str | None:
    """Look up the system-registered chrome.exe via Windows App Paths registry."""
    if os.name != "nt":
        return None
    try:
        import winreg
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for app in ("chrome.exe", "msedge.exe", "brave.exe"):
                try:
                    with winreg.OpenKey(hive, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{app}") as k:
                        path, _ = winreg.QueryValueEx(k, None)
                        if path and os.path.exists(path):
                            return path
                except OSError:
                    continue
    except Exception:
        return None
    return None


def find_browser_exe() -> tuple[str | None, str | None]:
    """Locate a Chromium-based browser on Windows. Returns (exe_path, browser_label).

    Searches in order: registry App Paths → Chrome → Edge → Brave → Vivaldi → Chromium.
    Anything Chromium-derived supports CDP, so the first hit wins.
    """
    reg = _registry_browser_exe()
    if reg:
        # Best-effort label from the path
        low = reg.lower()
        for label, _ in _BROWSER_CANDIDATES:
            if label.split()[0].lower() in low:
                return reg, label
        return reg, "Browser"

    for label, paths in _BROWSER_CANDIDATES:
        for path in paths:
            if os.path.exists(path):
                return path, label
    return None, None


def find_chrome_exe() -> str | None:
    """Backwards-compat shim — returns first installed Chromium-based browser path."""
    exe, _ = find_browser_exe()
    return exe


def get_jarvis_profile_dir() -> str:
    """Dedicated profile dir for JARVIS background sending.

    Chrome 136+ silently ignores --remote-debugging-port when launched with the
    default user-data-dir (cookie-theft prevention). A dedicated directory
    sidesteps that restriction. Path is browser-agnostic and lives under the
    detected browser's vendor folder; user signs into Gmail/Teams/WhatsApp web
    here once.
    """
    exe, label = find_browser_exe()
    if exe and label:
        # Place profile next to the regular profile in the vendor folder
        # e.g. C:\Users\X\AppData\Local\Google\Chrome\Jarvis Profile
        # or  C:\Users\X\AppData\Local\Microsoft\Edge\Jarvis Profile
        if "edge" in label.lower():
            base = Path.home() / "AppData" / "Local" / "Microsoft" / "Edge"
        elif "brave" in label.lower():
            base = Path.home() / "AppData" / "Local" / "BraveSoftware" / "Brave-Browser"
        elif "vivaldi" in label.lower():
            base = Path.home() / "AppData" / "Local" / "Vivaldi"
        else:
            base = Path.home() / "AppData" / "Local" / "Google" / "Chrome"
        p = base / "Jarvis Profile"
    else:
        # Fallback — place under generic LOCALAPPDATA
        p = Path.home() / "AppData" / "Local" / "Jarvis" / "Browser Profile"
    p.mkdir(parents=True, exist_ok=True)
    return str(p)


def restart_chrome_with_debug(restore_session: bool = False) -> tuple[bool, str]:
    """Kill the user's Chromium-based browser and re-launch it with --remote-debugging-port using the JARVIS profile."""
    browser_exe, _ = find_browser_exe()
    if not browser_exe:
        return False, "Koi Chromium browser nahi mila (Chrome / Edge / Brave / Vivaldi) — install karo pehle."

    user_data_dir = get_jarvis_profile_dir()
    image_name = os.path.basename(browser_exe)  # e.g. chrome.exe, msedge.exe, brave.exe

    try:
        # Kill running instances of THIS browser only — leave other browsers alone
        subprocess.run(
            ["taskkill", "/F", "/IM", image_name],
            capture_output=True, timeout=10,
        )
        time.sleep(2)

        args = [
            browser_exe,
            f"--remote-debugging-port={DEBUG_PORT}",
            f"--user-data-dir={user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        if restore_session:
            args.append("--restore-last-session")

        subprocess.Popen(
            args,
            creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS)
            if os.name == "nt" else 0,
        )

        # Wait until debug port is up
        for _ in range(20):
            time.sleep(0.5)
            if is_debug_running():
                return True, "Chrome debug mode mein chalu — background sending ready"
        return False, "Chrome restart ho gaya but debug port detect nahi hua"
    except Exception as e:
        log.warning("chrome_restart_failed", error=str(e))
        return False, f"Chrome restart failed: {e}"


def connect_playwright_cdp():
    """Connect Playwright to running Chrome via CDP. Returns (playwright, browser, context)."""
    from playwright.sync_api import sync_playwright

    p = sync_playwright().start()
    try:
        browser = p.chromium.connect_over_cdp(CDP_URL, timeout=10000)
    except Exception as e:
        p.stop()
        raise RuntimeError(f"Chrome CDP connect failed: {e}")

    contexts = browser.contexts
    if not contexts:
        p.stop()
        raise RuntimeError("Chrome mein koi context nahi mili")

    return p, browser, contexts[0]


def get_status() -> dict:
    """Return CDP status for UI."""
    if not is_debug_running():
        return {
            "available": False,
            "tabs": 0,
            "whatsapp_open": False,
            "message": "Chrome debug mode mein nahi chal raha. Background mode disabled.",
        }
    tabs = list_tabs()
    wa_tab = any("web.whatsapp.com" in t.get("url", "").lower() for t in tabs)
    return {
        "available": True,
        "tabs": len(tabs),
        "whatsapp_open": wa_tab,
        "message": "Chrome CDP active — background mode ready",
    }
