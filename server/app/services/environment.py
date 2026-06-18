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


_LOC_CACHE = Path(__file__).resolve().parents[2] / "data" / "location.json"


def _read_loc() -> dict:
    try:
        return json.loads(_LOC_CACHE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_loc(d: dict) -> None:
    try:
        _LOC_CACHE.parent.mkdir(parents=True, exist_ok=True)
        _LOC_CACHE.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        log.warning("location_cache_write_failed", error=str(e)[:120])


def detect_location_by_ip() -> dict | None:
    """Approximate location from public IP (free, no key, no permission) —
    CITY-level only (e.g. 'Karachi'), not the exact street."""
    import urllib.request
    try:
        with urllib.request.urlopen("http://ip-api.com/json/", timeout=5) as r:
            d = json.loads(r.read().decode())
        if d.get("status") == "success":
            return {"city": d.get("city"), "region": d.get("regionName"),
                    "country": d.get("country"), "lat": d.get("lat"),
                    "lon": d.get("lon"), "source": "ip", "_ts": time.time()}
    except Exception as e:
        log.warning("ip_geolocation_failed", error=str(e)[:120])
    return None


def set_user_location(text: str) -> dict:
    """User sets their precise area (e.g. 'Defence Phase 2, Karachi') — most
    reliable; stored + preferred over IP for picking nearby outlets."""
    loc = _read_loc()
    loc["user_set"] = (text or "").strip()
    loc["_ts"] = time.time()
    _write_loc(loc)
    log.info("user_location_set", area=loc["user_set"][:60])
    return loc


def _reverse_geocode(lat: float, lon: float) -> dict | None:
    """lat/long ko area+city mein badlo — OpenStreetMap Nominatim (free, no key)."""
    import urllib.request
    try:
        url = (f"https://nominatim.openstreetmap.org/reverse?format=json&lat={lat}"
               f"&lon={lon}&zoom=16&addressdetails=1&accept-language=en")
        req = urllib.request.Request(url, headers={"User-Agent": "JARVIS-Assistant/1.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            d = json.loads(r.read().decode())
        a = d.get("address", {}) or {}
        area = (a.get("suburb") or a.get("neighbourhood") or a.get("residential")
                or a.get("quarter") or a.get("road") or "")
        city = (a.get("city") or a.get("town") or a.get("state_district")
                or a.get("county") or a.get("state") or "")
        display = ", ".join([p for p in (area, city, a.get("country")) if p]) \
            or d.get("display_name", "")
        return {"area": area, "city": city, "display": display}
    except Exception as e:
        log.warning("reverse_geocode_failed", error=str(e)[:120])
        return None


def set_gps_location(lat: float, lon: float, accuracy: float | None = None) -> dict:
    """Dashboard browser ne PRECISE GPS bheja → reverse-geocode + store. Yeh
    sabse pakka location (city-level IP se behtar). Self-adaptive: har user ka apna.

    accuracy = browser ka bataya hua radius (meters). Laptop pe yeh aksar bada
    hota hai (wifi-based) — isay store karte hain taaki jawab mein honestly bata
    saken ke fix kitna pakka hai."""
    loc = _read_loc()
    geo = _reverse_geocode(lat, lon) or {}
    loc["gps"] = {
        "lat": lat, "lon": lon,
        "area": geo.get("area"), "city": geo.get("city"),
        "display": geo.get("display"), "_ts": time.time(),
        "accuracy_m": (float(accuracy) if accuracy is not None else None),
    }
    _write_loc(loc)
    log.info("gps_location_set", area=(geo.get("display") or "")[:70])
    return get_location()


def get_location(refresh_ip: bool = False) -> dict:
    """Best-known location. Preference: GPS (precise, browser) > user_set > IP city."""
    loc = _read_loc()
    if refresh_ip or "ip" not in loc or (time.time() - loc.get("ip", {}).get("_ts", 0) > 1800):
        ip = detect_location_by_ip()
        if ip:
            loc["ip"] = ip
            _write_loc(loc)
    gps = loc.get("gps") or {}
    gps_best = gps.get("display") or gps.get("area") or gps.get("city")
    return {
        "gps": loc.get("gps"),                           # precise (browser GPS)
        "user_set": loc.get("user_set"),                 # precise, user-given
        "ip": loc.get("ip"),                             # approx city (auto)
        "best": gps_best or loc.get("user_set") or (loc.get("ip", {}) or {}).get("city"),
        "accuracy_m": gps.get("accuracy_m"),             # browser GPS radius (meters); bada = kam pakka
        "note": "GPS sabse pakka; phir user_set; IP sirf city-level. Nearby ke liye 'best' use karo.",
    }


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
