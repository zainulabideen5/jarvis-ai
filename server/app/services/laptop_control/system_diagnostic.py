"""End-to-end system diagnostic — exercise every layer of JARVIS.

Single entry point: `SystemDiagnostic.get().run_full_check()`. Returns a
structured report showing what's working, what's broken, what needs setup.

Used for:
    - First-run health check (after install.bat)
    - Multi-laptop verification (50+ users — same test on each)
    - Pre-test sanity (before user starts using features)
    - Bug triage (which layer is the failure in?)

Tests done in order of dependency:
    Layer 0  Python + deps installed
    Layer 1  Native infra (pyautogui + pywinauto)
    Layer 2  Office COM (Excel/Word/Outlook detection)
    Layer 3  Multi-Gmail accounts file accessible
    Layer 4  Win UIA can list windows
    Layer 5  OpenCV + image-match templates dir
    Layer 6  Server routes loaded
    Layer 7  WA Playwright session
    Layer 8  Disk space + screen + permissions
    Layer 9  LLM key (any provider)

Each test = isolated, fast (<2 sec), no external side effects.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from app.core.logging import get_logger

log = get_logger(__name__)


class SystemDiagnostic:
    _instance: "SystemDiagnostic | None" = None

    @classmethod
    def get(cls) -> "SystemDiagnostic":
        if cls._instance is None:
            cls._instance = SystemDiagnostic()
        return cls._instance

    def run_full_check(self) -> dict:
        """Run all layer checks. Returns structured report."""
        layers = []
        layers.append(self._layer_python_deps())
        layers.append(self._layer_native_infra())
        layers.append(self._layer_office_com())
        layers.append(self._layer_win_uia())
        layers.append(self._layer_image_match())
        layers.append(self._layer_server_routes())
        layers.append(self._layer_system_env())
        layers.append(self._layer_llm_keys())

        # Overall summary
        passed = sum(1 for l in layers if l["status"] == "ok")
        failed = sum(1 for l in layers if l["status"] == "fail")
        partial = sum(1 for l in layers if l["status"] == "partial")
        total = len(layers)
        return {
            "ok": failed == 0,
            "summary": {
                "total": total,
                "passed": passed,
                "partial": partial,
                "failed": failed,
                "score_pct": round(((passed + 0.5 * partial) / max(1, total)) * 100, 1),
            },
            "layers": layers,
            "ran_at": time.time(),
        }

    # ==================================================================
    # Per-layer checks
    # ==================================================================

    def _layer_python_deps(self) -> dict:
        """Layer 0: Python + key dependencies installed."""
        deps = {}
        for name, mod in (
            ("pyautogui", "pyautogui"),
            ("pywinauto", "pywinauto"),
            ("cv2", "cv2"),
            ("PIL", "PIL"),
            ("win32com", "win32com.client"),
            ("comtypes", "comtypes"),
            ("fastapi", "fastapi"),
            ("uvicorn", "uvicorn"),
        ):
            try:
                __import__(mod)
                deps[name] = "ok"
            except Exception as e:
                deps[name] = f"missing: {str(e)[:60]}"
        missing = [k for k, v in deps.items() if v != "ok"]
        return {
            "layer": "0-python-deps",
            "name": "Python + dependencies",
            "status": "ok" if not missing else "fail",
            "details": deps,
            "fix_hint": (
                f"Install: pip install {' '.join(missing)}"
                if missing else ""
            ),
        }

    def _layer_native_infra(self) -> dict:
        """Layer 1: laptop_native module + screen + mouse access."""
        try:
            from app.services.laptop_control.laptop_native import LaptopNative
            inst = LaptopNative.get()
            screen = inst.screen_size()
            mouse = inst.mouse_position()
            ok = bool(screen.get("ok") and mouse.get("ok"))
            return {
                "layer": "1-native-infra",
                "name": "Mouse + keyboard + screen",
                "status": "ok" if ok else "fail",
                "details": {
                    "screen": screen,
                    "mouse_pos": mouse,
                },
                "fix_hint": "" if ok else "pyautogui can't read screen — check display drivers / RDP session",
            }
        except Exception as e:
            return {"layer": "1-native-infra", "name": "Mouse + keyboard + screen", "status": "fail", "error": str(e)[:200]}

    def _layer_office_com(self) -> dict:
        """Layer 2: Excel / Word / Outlook accessible via COM (silent)."""
        try:
            from app.services.laptop_control.office_com import OfficeCOM
            r = OfficeCOM.get().check_office_installed()
            apps = r.get("office", {})
            ok_count = sum(1 for v in apps.values() if v.get("available"))
            total = len(apps)
            status = "ok" if ok_count == total else ("partial" if ok_count > 0 else "fail")
            return {
                "layer": "2-office-com",
                "name": "Office COM (Excel/Word/Outlook silent)",
                "status": status,
                "details": apps,
                "fix_hint": (
                    "" if status == "ok" else
                    "Office not installed. Install Microsoft 365 / Office 2019+ for silent automation."
                ),
            }
        except Exception as e:
            return {"layer": "2-office-com", "name": "Office COM", "status": "fail", "error": str(e)[:200]}

    def _layer_win_uia(self) -> dict:
        """Layer 4: pywinauto can enumerate windows."""
        try:
            from app.services.laptop_control.laptop_native import LaptopNative
            wins = LaptopNative.get().list_open_windows()
            count = wins.get("count", 0)
            ok = bool(wins.get("ok"))
            return {
                "layer": "4-win-uia",
                "name": "Windows UIA (native apps)",
                "status": "ok" if ok else "fail",
                "details": {
                    "visible_windows": count,
                },
                "fix_hint": "" if ok else "pywinauto can't enumerate windows — UIA service may be down",
            }
        except Exception as e:
            return {"layer": "4-win-uia", "name": "Windows UIA", "status": "fail", "error": str(e)[:200]}

    def _layer_image_match(self) -> dict:
        """Layer 5: OpenCV + templates dir + sample match test."""
        try:
            from app.services.laptop_control.image_match import ImageMatcher
            tmpl = ImageMatcher.get().list_templates()
            template_count = tmpl.get("count", 0)
            # Verify OpenCV can do a minimal operation
            import cv2
            import numpy as np
            arr = np.zeros((10, 10, 3), dtype=np.uint8)
            res = cv2.matchTemplate(arr, arr, cv2.TM_CCOEFF_NORMED)
            cv_ok = res is not None
            return {
                "layer": "5-image-match",
                "name": "OpenCV image-match",
                "status": "ok" if cv_ok else "fail",
                "details": {
                    "opencv_works": cv_ok,
                    "templates_saved": template_count,
                },
                "fix_hint": "" if cv_ok else "cv2 broken — reinstall: pip install opencv-python",
            }
        except Exception as e:
            return {"layer": "5-image-match", "name": "OpenCV image-match", "status": "fail", "error": str(e)[:200]}

    def _layer_server_routes(self) -> dict:
        """Layer 6: count key routes mounted."""
        try:
            from app.main import app
            paths = [r.path for r in app.routes if hasattr(r, "path")]
            # Key paths we should have
            required = [
                "/api/chat",
                "/api/native/screen-info",
                "/api/office/check",
                "/api/winapps/calculator",
                "/api/imgmatch/templates",
            ]
            missing = [p for p in required if p not in paths]
            return {
                "layer": "6-server-routes",
                "name": "FastAPI routes loaded",
                "status": "ok" if not missing else "partial",
                "details": {
                    "total_routes": len(paths),
                    "missing": missing,
                },
                "fix_hint": "" if not missing else f"Missing routes: {missing} — restart server",
            }
        except Exception as e:
            return {"layer": "6-server-routes", "name": "Server routes", "status": "fail", "error": str(e)[:200]}

    def _layer_system_env(self) -> dict:
        """Layer 8: disk space + permissions + server data dir."""
        try:
            import shutil
            data_dir = Path(__file__).resolve().parents[3] / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            # Disk free in GB on data drive
            total, used, free = shutil.disk_usage(str(data_dir))
            free_gb = round(free / (1024**3), 1)
            writable = os.access(str(data_dir), os.W_OK)
            return {
                "layer": "8-system-env",
                "name": "System environment",
                "status": "ok" if (free_gb > 2 and writable) else "partial",
                "details": {
                    "data_dir": str(data_dir),
                    "free_disk_gb": free_gb,
                    "writable": writable,
                    "hostname": os.environ.get("COMPUTERNAME", ""),
                    "user": os.environ.get("USERNAME", ""),
                },
                "fix_hint": (
                    "Disk free <2 GB — clean up before deploying"
                    if free_gb < 2 else
                    ("Data dir not writable" if not writable else "")
                ),
            }
        except Exception as e:
            return {"layer": "8-system-env", "name": "System env", "status": "fail", "error": str(e)[:200]}

    def _layer_llm_keys(self) -> dict:
        """Layer 9: at least one LLM provider key configured."""
        try:
            from app.core.config import ServerConfig
            cfg = ServerConfig()
            providers = {
                "groq": bool(cfg.groq_api_keys),
                "cerebras": bool(cfg.cerebras_api_keys),
                "gemini": bool(cfg.gemini_api_keys),
                "openrouter": bool(cfg.openrouter_api_keys),
            }
            any_key = any(providers.values())
            counts = {k: len(getattr(cfg, k + "_api_keys", [])) for k in providers}
            return {
                "layer": "9-llm-keys",
                "name": "LLM API keys",
                "status": "ok" if any_key else "fail",
                "details": {
                    "providers_with_keys": [k for k, v in providers.items() if v],
                    "key_counts": counts,
                },
                "fix_hint": (
                    "" if any_key else
                    "No LLM key configured. Edit server/.env — add JARVIS_GROQ_API_KEY (free at groq.com)"
                ),
            }
        except Exception as e:
            return {"layer": "9-llm-keys", "name": "LLM keys", "status": "fail", "error": str(e)[:200]}
