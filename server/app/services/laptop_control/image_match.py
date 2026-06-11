"""Image template matching — find buttons by their VISUAL appearance.

For apps where UIA fails (Photoshop, Premiere, games, custom-rendered UIs),
user saves button images as PNG templates. JARVIS captures screen, uses
OpenCV template matching to find the button, then clicks at the matched
coordinates.

100% LOCAL — no LLM, no cloud, no screenshots leave laptop.

Template storage:
    server/data/app_templates/<app_name>/<element_name>.png

API usage:
    1. User opens Photoshop, sees a button they want to automate
    2. Calls /api/imgmatch/capture-region to save a screenshot region as template
    3. Names it e.g. "photoshop/new_layer.png"
    4. Later: /api/imgmatch/click-template {template: "photoshop/new_layer"}
    5. JARVIS finds the button on screen + clicks

Templates can be reused across sessions — once captured, forever usable.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from app.core.logging import get_logger

log = get_logger(__name__)

_TEMPLATES_DIR = Path(__file__).resolve().parents[3] / "data" / "app_templates"


def _ensure_dir():
    _TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)


class ImageMatcher:
    """Singleton — OpenCV-based template matching."""

    _instance: "ImageMatcher | None" = None

    @classmethod
    def get(cls) -> "ImageMatcher":
        if cls._instance is None:
            cls._instance = ImageMatcher()
        return cls._instance

    # ==================================================================
    # Template management
    # ==================================================================

    def list_templates(self) -> dict:
        """List all saved templates organized by app folder."""
        _ensure_dir()
        out = {}
        for p in _TEMPLATES_DIR.rglob("*.png"):
            try:
                rel = p.relative_to(_TEMPLATES_DIR)
                parts = rel.parts
                if len(parts) >= 2:
                    app = parts[0]
                    name = "/".join(parts[1:]).rsplit(".png", 1)[0]
                else:
                    app = "_root"
                    name = rel.stem
                out.setdefault(app, []).append({
                    "name": name,
                    "path": str(p),
                    "size_kb": round(p.stat().st_size / 1024, 1),
                })
            except Exception:
                continue
        return {"ok": True, "templates": out, "count": sum(len(v) for v in out.values())}

    def capture_region(self, save_as: str, x: int, y: int, width: int, height: int) -> dict:
        """Capture a region of the current screen as a template.

        Args:
            save_as: relative path like "photoshop/new_layer" (no .png extension)
            x, y:    top-left corner of region
            width, height: region size
        """
        if not save_as:
            return {"ok": False, "error": "save_as required (e.g. 'photoshop/new_layer')"}
        if width < 5 or height < 5:
            return {"ok": False, "error": "Region too small (min 5x5)"}
        try:
            import pyautogui as pag
            _ensure_dir()
            # Validate region is fully on-screen — pyautogui silently
            # crops if off-screen, which yields a wrong-sized template
            # that NEVER matches the original button.
            sw, sh = pag.size()
            if x < 0 or y < 0:
                return {"ok": False, "error": "x and y must be >= 0"}
            if x + width > sw or y + height > sh:
                return {
                    "ok": False,
                    "error": f"Region extends off-screen (screen is {sw}x{sh})",
                }
            img = pag.screenshot(region=(x, y, width, height))
            save_path = _TEMPLATES_DIR / f"{save_as}.png"
            save_path.parent.mkdir(parents=True, exist_ok=True)
            img.save(save_path)
            return {
                "ok": True,
                "saved_to": str(save_path),
                "size": {"width": img.width, "height": img.height},
                "key": save_as,
            }
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    def delete_template(self, key: str) -> dict:
        """Delete a saved template by key (e.g. 'photoshop/new_layer')."""
        p = _TEMPLATES_DIR / f"{key}.png"
        if not p.exists():
            return {"ok": False, "error": f"Template '{key}' nahi mila"}
        try:
            p.unlink()
            return {"ok": True, "deleted": key}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    # ==================================================================
    # Template matching
    # ==================================================================

    def find_template(self, key: str, confidence: float = 0.85) -> dict:
        """Find a template's location on the CURRENT screen.

        Returns: {ok, found, x, y, width, height, confidence_actual}
        """
        template_path = _TEMPLATES_DIR / f"{key}.png"
        if not template_path.exists():
            return {"ok": False, "error": f"Template '{key}' nahi mila"}
        try:
            import cv2
            import numpy as np
            import pyautogui as pag

            # Take screen
            screen_pil = pag.screenshot()
            screen_np = np.array(screen_pil)
            screen_bgr = cv2.cvtColor(screen_np, cv2.COLOR_RGB2BGR)

            # Load template
            # Load with IMREAD_UNCHANGED so we keep alpha if present, then
            # downconvert to BGR. IMREAD_COLOR alone silently discards alpha
            # → channel-mismatched match returns garbage.
            template_raw = cv2.imread(str(template_path), cv2.IMREAD_UNCHANGED)
            if template_raw is None:
                return {"ok": False, "error": "Template image load fail"}
            if template_raw.ndim == 2:
                template_bgr = cv2.cvtColor(template_raw, cv2.COLOR_GRAY2BGR)
            elif template_raw.shape[2] == 4:
                template_bgr = cv2.cvtColor(template_raw, cv2.COLOR_BGRA2BGR)
            else:
                template_bgr = template_raw

            th, tw = template_bgr.shape[:2]
            sh, sw = screen_bgr.shape[:2]
            if th > sh or tw > sw:
                return {"ok": False, "error": "Template larger than screen"}

            # Match
            res = cv2.matchTemplate(screen_bgr, template_bgr, cv2.TM_CCOEFF_NORMED)
            min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(res)

            log.info("imgmatch_attempt", key=key, confidence_actual=round(max_val, 3))

            if max_val < confidence:
                return {
                    "ok": True,
                    "found": False,
                    "confidence_actual": round(float(max_val), 3),
                    "confidence_required": confidence,
                }

            x, y = max_loc
            return {
                "ok": True,
                "found": True,
                "x": int(x),
                "y": int(y),
                "width": int(tw),
                "height": int(th),
                "center_x": int(x + tw // 2),
                "center_y": int(y + th // 2),
                "confidence_actual": round(float(max_val), 3),
            }
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    def click_template(self, key: str, confidence: float = 0.85, button: str = "left", clicks: int = 1) -> dict:
        """Find a template on screen + click its center.

        Returns: {ok, clicked, x, y, confidence}
        """
        loc = self.find_template(key, confidence)
        if not loc.get("ok"):
            return loc
        if not loc.get("found"):
            return {
                "ok": False,
                "error": f"Template '{key}' screen pe nahi mila",
                "confidence_actual": loc.get("confidence_actual", 0),
            }
        try:
            import pyautogui as pag
            cx, cy = loc["center_x"], loc["center_y"]
            pag.click(x=cx, y=cy, button=button, clicks=clicks, interval=0.1)
            log.info("imgmatch_clicked", key=key, x=cx, y=cy, confidence=loc.get("confidence_actual"))
            return {
                "ok": True,
                "clicked": True,
                "x": cx,
                "y": cy,
                "confidence": loc.get("confidence_actual"),
            }
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    def wait_for_template(self, key: str, timeout_sec: float = 10.0, confidence: float = 0.85, poll_interval: float = 0.5) -> dict:
        """Poll the screen for a template until found or timeout."""
        deadline = time.monotonic() + timeout_sec
        last_conf = 0.0
        while time.monotonic() < deadline:
            r = self.find_template(key, confidence)
            if r.get("ok") and r.get("found"):
                return r
            if r.get("ok"):
                last_conf = max(last_conf, r.get("confidence_actual", 0))
            time.sleep(poll_interval)
        return {
            "ok": False,
            "error": f"Template '{key}' {timeout_sec}s mein nahi appeared",
            "best_confidence_seen": last_conf,
        }

    def find_all_templates(self, key: str, confidence: float = 0.85, max_results: int = 10) -> dict:
        """Find ALL occurrences of a template on screen (e.g. multiple
        similar buttons in a row). Returns list of locations sorted by
        descending confidence.
        """
        template_path = _TEMPLATES_DIR / f"{key}.png"
        if not template_path.exists():
            return {"ok": False, "error": f"Template '{key}' nahi mila"}
        try:
            import cv2
            import numpy as np
            import pyautogui as pag

            screen_np = np.array(pag.screenshot())
            screen_bgr = cv2.cvtColor(screen_np, cv2.COLOR_RGB2BGR)
            template_bgr = cv2.imread(str(template_path), cv2.IMREAD_COLOR)
            if template_bgr is None:
                return {"ok": False, "error": "Template load fail"}

            th, tw = template_bgr.shape[:2]
            res = cv2.matchTemplate(screen_bgr, template_bgr, cv2.TM_CCOEFF_NORMED)
            loc = np.where(res >= confidence)
            points = list(zip(*loc[::-1]))  # (x, y) pairs

            # Non-max suppression — dedupe overlapping matches
            results = []
            for (x, y) in points:
                # Skip if too close to an existing result
                too_close = False
                for r in results:
                    if abs(r["x"] - x) < tw // 2 and abs(r["y"] - y) < th // 2:
                        too_close = True
                        break
                if too_close:
                    continue
                results.append({
                    "x": int(x),
                    "y": int(y),
                    "center_x": int(x + tw // 2),
                    "center_y": int(y + th // 2),
                    "confidence": round(float(res[y, x]), 3),
                })
                if len(results) >= max_results:
                    break
            results.sort(key=lambda r: r["confidence"], reverse=True)
            return {"ok": True, "count": len(results), "matches": results}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}
