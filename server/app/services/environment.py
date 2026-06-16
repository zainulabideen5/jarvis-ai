"""Environment / context awareness — JARVIS detects WHERE it's running (PC vs
phone, OS, device, installed apps, browsers) and REMEMBERS it, so it can pick
the right method for each platform:

    PC / laptop  → web version (background) or desktop app (foreground)
    phone        → mobile app   (when a phone client exists)

This is the "fully smart" base: instead of treating every machine the same,
JARVIS adapts to the host it's on (self-adaptive — nothing hardcoded). The
detected context is cached on disk so it persists across restarts.
"""
from __future__ import annotations

import getpass
import json
import os
import platform
import shutil
import socket
import time
from pathlib import Path

from app.core.logging import get_logger

log = get_logger(__name__)

_CACHE = Path(__file__).resolve().parents[2] / "data" / "environment.json"

# common chat apps + how to reach them on each platform
_CHAT_APPS = {
    "whatsapp": {"win_proc": "WhatsApp", "web": "web.whatsapp.com", "mobile": "WhatsApp app"},
    "teams":    {"win_proc": "Teams",    "web": "teams.microsoft.com", "mobile": "Teams app"},
    "slack":    {"win_proc": "Slack",    "web": "app.slack.com", "mobile": "Slack app"},
    "discord":  {"win_proc": "Discord",  "web": "discord.com/app", "mobile": "Discord app"},
    "telegram": {"win_proc": "Telegram", "web": "web.telegram.org", "mobile": "Telegram app"},
}


def _has_battery() -> bool:
    try:
        import psutil
        return psutil.sensors_battery() is not None
    except Exception:
        return False


def _detect_browsers() -> list[str]:
    found = []
    candidates = {
        "chrome": [r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
                   r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"],
        "edge": [r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"],
        "firefox": [r"%ProgramFiles%\Mozilla Firefox\firefox.exe"],
    }
    for name, paths in candidates.items():
        for p in paths:
            if os.path.isfile(os.path.expandvars(p)):
                found.append(name)
                break
    return found


def _detect_installed_chat_apps() -> dict:
    """Which chat apps are installed/running (Windows) — best-effort via open
    windows. (App present → we can drive the desktop app; else use web.)"""
    present = {}
    try:
        from app.services.laptop_control.laptop_native import LaptopNative
        nat = LaptopNative.get()
        titles = [w.get("title", "").lower() for w in nat.list_open_windows().get("windows", [])]
        for app, cfg in _CHAT_APPS.items():
            present[app] = any(cfg["win_proc"].lower() in t for t in titles)
    except Exception:
        present = {a: False for a in _CHAT_APPS}
    return present


def detect_environment(refresh: bool = False) -> dict:
    """Detect (and cache) the runtime environment. JARVIS calls this to know
    its platform and choose app-vs-web. self-adaptive: all from the live host."""
    if not refresh and _CACHE.exists():
        try:
            cached = json.loads(_CACHE.read_text(encoding="utf-8"))
            if time.time() - cached.get("_ts", 0) < 3600:   # 1h fresh
                return cached
        except Exception:
            pass

    osys = platform.system()                       # 'Windows'
    info = {
        "platform_kind": "pc",                     # this server runs on a PC/laptop
        "device_type": "laptop" if _has_battery() else "desktop",
        "os": osys,
        "os_version": platform.release(),
        "hostname": socket.gethostname(),
        "user": getpass.getuser(),
        "browsers": _detect_browsers() if osys == "Windows" else [],
        "chat_apps_present": _detect_installed_chat_apps() if osys == "Windows" else {},
        # how to reach apps on THIS platform
        "method": {
            "rule": "PC/laptop → web (background) ya desktop app (foreground); phone → mobile app",
            "prefer_on_pc": "web",                 # background-friendly default on PC
        },
        "_ts": time.time(),
    }
    try:
        _CACHE.parent.mkdir(parents=True, exist_ok=True)
        _CACHE.write_text(json.dumps(info, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning("environment_cache_write_failed", error=str(e)[:120])
    log.info("environment_detected", device=info["device_type"], os=osys,
             apps={k: v for k, v in info["chat_apps_present"].items() if v})
    return info


def how_to_reach(app: str) -> dict:
    """For a given app on the CURRENT platform, say how to reach it:
    PC → desktop app if installed (foreground) else web; phone → mobile app."""
    env = detect_environment()
    app = (app or "").lower()
    cfg = _CHAT_APPS.get(app, {})
    if env["platform_kind"] == "phone":
        return {"platform": "phone", "use": "app", "target": cfg.get("mobile", app)}
    installed = env.get("chat_apps_present", {}).get(app, False)
    return {
        "platform": "pc",
        "use": "desktop_app" if installed else "web",
        "target": cfg.get("win_proc", app) if installed else cfg.get("web", app),
        "installed": installed,
    }
