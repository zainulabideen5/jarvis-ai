"""Live system info — RAM, disk space, CPU, battery, OS. Fully general &
self-adaptive: jis bhi machine pe chale, uski ASLI stats padhta hai (kuch
hardcoded nahi). psutil + stdlib se. Chat se seedha jawab dene ke liye.
"""

from __future__ import annotations

import platform
import shutil
import time

from app.core.logging import get_logger

log = get_logger(__name__)


def _fmt_bytes(n: float) -> str:
    """Bytes ko human-readable (GB/TB) mein."""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(n) < 1024.0:
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} EB"


def get_system_info() -> dict:
    """Pura system snapshot — RAM, har disk, CPU, battery, OS. General."""
    info: dict = {"ok": True}

    # ---- OS / machine ----
    try:
        info["os"] = f"{platform.system()} {platform.release()}"
        info["machine"] = platform.machine()
        info["hostname"] = platform.node()
    except Exception:
        pass

    try:
        import psutil

        # ---- RAM ----
        vm = psutil.virtual_memory()
        info["ram"] = {
            "total": vm.total, "available": vm.available, "used": vm.used,
            "percent": vm.percent,
            "total_h": _fmt_bytes(vm.total), "available_h": _fmt_bytes(vm.available),
            "used_h": _fmt_bytes(vm.used),
        }

        # ---- Disks (har partition) ----
        disks = []
        for part in psutil.disk_partitions(all=False):
            try:
                u = psutil.disk_usage(part.mountpoint)
            except (PermissionError, OSError):
                continue  # CD/empty drive etc.
            disks.append({
                "device": part.device, "mount": part.mountpoint,
                "total": u.total, "used": u.used, "free": u.free, "percent": u.percent,
                "total_h": _fmt_bytes(u.total), "used_h": _fmt_bytes(u.used),
                "free_h": _fmt_bytes(u.free),
            })
        info["disks"] = disks

        # ---- CPU ----
        try:
            freq = psutil.cpu_freq()
            ghz = f"{freq.current / 1000:.2f} GHz" if freq and freq.current else None
        except Exception:
            ghz = None
        info["cpu"] = {
            "cores_physical": psutil.cpu_count(logical=False),
            "cores_logical": psutil.cpu_count(logical=True),
            "percent": psutil.cpu_percent(interval=0.3),
            "ghz": ghz,
            "name": platform.processor() or None,
        }

        # ---- Battery (laptop ho to) ----
        try:
            bat = psutil.sensors_battery()
            if bat is not None:
                info["battery"] = {
                    "percent": round(bat.percent),
                    "plugged": bool(bat.power_plugged),
                }
        except Exception:
            pass

        # ---- Uptime ----
        try:
            info["uptime_sec"] = int(time.time() - psutil.boot_time())
        except Exception:
            pass

    except Exception as e:
        # psutil na ho to disk shutil se de do (kam se kam space).
        log.warning("system_info_psutil_failed", error=str(e)[:160])
        try:
            u = shutil.disk_usage("C:\\" if platform.system() == "Windows" else "/")
            info["disks"] = [{
                "device": "C:", "mount": "C:\\",
                "total": u.total, "used": u.used, "free": u.free,
                "percent": round(u.used / u.total * 100) if u.total else 0,
                "total_h": _fmt_bytes(u.total), "used_h": _fmt_bytes(u.used),
                "free_h": _fmt_bytes(u.free),
            }]
        except Exception:
            info["ok"] = False
            info["error"] = str(e)[:160]

    return info


def _uptime_h(sec: int) -> str:
    d, rem = divmod(int(sec), 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    out = []
    if d:
        out.append(f"{d}d")
    if h:
        out.append(f"{h}h")
    if m and not d:
        out.append(f"{m}m")
    return " ".join(out) or "abhi-abhi"


def format_system_info(info: dict, want: set[str] | None = None) -> str:
    """Snapshot ko chat-friendly text mein. want = kaunse parts chahiye
    ({'ram','disk','cpu','battery'}); None = sab."""
    if not info.get("ok"):
        return f"⚠️ System info nahi le paya: {info.get('error', 'unknown')}"
    want = want or {"ram", "disk", "cpu", "battery"}
    lines: list[str] = []

    if "ram" in want and info.get("ram"):
        r = info["ram"]
        lines.append(
            f"🧠 **RAM:** {r['used_h']} / {r['total_h']} use ho rahi "
            f"({r['percent']:.0f}%) — **{r['available_h']} free**"
        )

    if "disk" in want and info.get("disks"):
        lines.append("💾 **Disk space:**")
        for d in info["disks"]:
            lines.append(
                f"  • **{d['device'].rstrip(chr(92))}** — {d['free_h']} free / "
                f"{d['total_h']} total ({d['percent']:.0f}% bhari)"
            )

    if "cpu" in want and info.get("cpu"):
        c = info["cpu"]
        cores = c.get("cores_logical") or c.get("cores_physical")
        bits = []
        if cores:
            bits.append(f"{cores} cores")
        if c.get("ghz"):
            bits.append(c["ghz"])
        if c.get("percent") is not None:
            bits.append(f"{c['percent']:.0f}% load")
        if bits:
            lines.append(f"⚙️ **CPU:** {', '.join(bits)}")

    if "battery" in want and info.get("battery"):
        b = info["battery"]
        plug = "charging 🔌" if b["plugged"] else "battery 🔋"
        lines.append(f"{('🔋')} **Battery:** {b['percent']}% ({plug})")

    if info.get("uptime_sec") is not None and want == {"ram", "disk", "cpu", "battery"}:
        lines.append(f"⏱️ **Uptime:** {_uptime_h(info['uptime_sec'])}")

    if info.get("os"):
        lines.append(f"🖥️ {info['os']}")

    return "\n".join(lines) if lines else "System info khali aaya."
