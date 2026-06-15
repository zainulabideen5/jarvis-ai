"""Vision-based computer control — JARVIS 'sees' the screen and acts like a
human (real mouse + keyboard). Works on ANY app or web page because it relies
on what's VISIBLE, not on app-specific labels.

Approach = Set-of-Marks (the reliable way):
    1. screenshot the foreground window
    2. UIA gives the PRECISE position of every on-screen control (works even
       when the control has no readable label — WebView apps hide labels but
       still expose boxes)
    3. overlay a NUMBER on each control
    4. a vision model LOOKS at the numbered screenshot and says "click 5,
       type X, press Enter" — it only has to pick numbers, not pixel-perfect
       coordinates, so it's reliable
    5. we click the exact UIA position like a human (pyautogui)
    6. screenshot again → verify → repeat

Vision model: Claude CLI (Opus) ONLY — per Zain, EVERYTHING goes through the
CLI (no Groq/third-party). Privacy: the screenshot is sent only to his own
authenticated Claude (Anthropic) via the CLI, and we capture ONLY the target
app's window (not the whole screen) and delete the temp files after each step.
"""
from __future__ import annotations

import json
import os
import tempfile
import time

from app.core.logging import get_logger

log = get_logger(__name__)

# interactive control types worth marking (clickable / typable)
_INTERACTIVE = {
    "Edit", "Document", "Button", "ListItem", "TreeItem", "MenuItem",
    "CheckBox", "RadioButton", "ComboBox", "Hyperlink", "TabItem", "Text",
    "SplitButton", "MenuItemControl",
}
_MAX_MARKS = 45            # cap clutter / keep the prompt small

_VISION_SYSTEM = """Tu JARVIS ka VISION controller hai. Tujhe ek screenshot diya jata hai jisme
har clickable/typable element pe ek NUMBER (mark) laga hai. Tera kaam: user ka
task pura karne ke liye AGLA EK action batao — bilkul jaise insaan screen dekh
ke karta hai.

SIRF ek JSON object do, aur kuch nahi:
  {"action":"click","mark":N,"why":"short"}             -- numbered mark N pe click
  {"action":"click_type","mark":N,"text":"...","why":""} -- mark N pe click PHIR likho
  {"action":"click_xy","x":X,"y":Y,"why":""}            -- screenshot ke (X,Y) PIXEL pe click
  {"action":"click_type_xy","x":X,"y":Y,"text":"...","why":""} -- (X,Y) pe click PHIR likho
  {"action":"type","text":"...","why":"short"}          -- abhi-focused box me likho
  {"action":"key","key":"enter","why":"short"}          -- key dabao (enter/tab/esc/ctrl+a)
  {"action":"done","reply":"user ko jawab"}             -- task complete
  {"action":"fail","reply":"kyun nahi hua"}             -- nahi ho saka (honest)

MARKS vs PIXEL:
- Agar element pe ek NUMBER (red mark) dikh raha hai → "click"/"click_type" with mark.
- Agar kisi cheez pe number NAHI hai (jaise web/chat apps ke andar ke box) →
  screenshot pe halki CYAN grid hai: upar X ke numbers, left me Y ke numbers.
  Un se us cheez ka pixel (X,Y) estimate karo aur "click_xy"/"click_type_xy" do.

RULES:
- Search/contact box me likhne se pehle uspe click karo (click_type ya click_type_xy).
- Message bhejne se pehle CONFIRM karo sahi chat/jagah khuli hai.
- Jab kaam ho jaye to "done" do — bina kaam ke "done" mat do.
- Agar sahi jagah nazar na aaye to "fail" do, jhooth mat bolo."""


class VisionController:
    _instance: "VisionController | None" = None

    @classmethod
    def get(cls) -> "VisionController":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ---------------- screen capture ----------------

    def _foreground_window(self):
        """Return a pywinauto wrapper for the current foreground window (or None)."""
        try:
            import win32gui
            from pywinauto import Desktop
            hwnd = win32gui.GetForegroundWindow()
            if not hwnd:
                return None
            return Desktop(backend="uia").window(handle=hwnd)
        except Exception as e:
            log.warning("vision_fg_window_failed", err=str(e)[:120])
            return None

    def _capture(self, window=None) -> tuple[str, tuple[int, int]]:
        """Grab the screen and crop to the target WINDOW only (privacy: the
        rest of the screen is never captured/sent). Returns (png_path, offset)
        where offset is the window's top-left in screen coords. UIA rects and
        pyautogui clicks stay in ABSOLUTE screen coords; only the saved image
        and its drawn marks are translated by the offset."""
        import mss
        from PIL import Image
        with mss.MSS() as sct:
            mon = sct.monitors[1]
            shot = sct.grab(mon)
            img = Image.frombytes("RGB", shot.size, shot.rgb)
        off = (0, 0)
        if window is not None:
            try:
                r = window.rectangle()
                left, top = max(0, r.left), max(0, r.top)
                right, bottom = min(img.width, r.right), min(img.height, r.bottom)
                if right - left > 50 and bottom - top > 50:
                    img = img.crop((left, top, right, bottom))
                    off = (left, top)
            except Exception:
                pass
        path = os.path.join(tempfile.gettempdir(), "jarvis_vision.png")
        img.save(path)
        return path, off

    # ---------------- element marks ----------------

    def _enumerate(self, window) -> list[dict]:
        """List interactive elements of `window` with screen-space centers.
        Bounded so a huge WebView tree can't hang us."""
        marks: list[dict] = []
        if window is None:
            return marks
        deadline = time.monotonic() + 5.0
        try:
            for e in window.descendants():
                if time.monotonic() > deadline or len(marks) >= _MAX_MARKS:
                    break
                try:
                    info = e.element_info
                    ctype = info.control_type or ""
                    if ctype not in _INTERACTIVE:
                        continue
                    r = e.rectangle()
                    w, h = r.width(), r.height()
                    if w <= 2 or h <= 2 or w > 3000 or h > 2000:
                        continue
                    marks.append({
                        "idx": len(marks) + 1,
                        "role": ctype,
                        "name": (info.name or "")[:50],
                        "cx": (r.left + r.right) // 2,
                        "cy": (r.top + r.bottom) // 2,
                        "rect": (r.left, r.top, r.right, r.bottom),
                    })
                except Exception:
                    continue
        except Exception as e:
            log.warning("vision_enumerate_failed", err=str(e)[:120])
        return marks

    def _annotate(self, img_path: str, marks: list[dict], offset: tuple[int, int]) -> str:
        """Draw each mark's number on the screenshot (rects translated from
        absolute screen coords into the cropped window image by `offset`)."""
        from PIL import Image, ImageDraw
        ox, oy = offset
        img = Image.open(img_path).convert("RGB")
        d = ImageDraw.Draw(img)
        # coordinate grid (image-space pixels) so the model can give click_xy
        # for elements UIA doesn't expose (WebView/web app innards).
        W, H = img.size
        for x in range(0, W, 100):
            d.line((x, 0, x, H), fill=(0, 200, 255), width=1)
            d.text((x + 2, 2), str(x), fill=(0, 130, 180))
        for y in range(0, H, 100):
            d.line((0, y, W, y), fill=(0, 200, 255), width=1)
            d.text((2, y + 2), str(y), fill=(0, 130, 180))
        for m in marks:
            l, t, r, b = m["rect"]
            box = (l - ox, t - oy, r - ox, b - oy)
            tag = str(m["idx"])
            d.rectangle(box, outline=(255, 0, 0), width=2)
            tw, th = 9 * len(tag) + 6, 16
            d.rectangle((box[0], box[1], box[0] + tw, box[1] + th), fill=(255, 0, 0))
            d.text((box[0] + 3, box[1] + 1), tag, fill=(255, 255, 255))
        out = os.path.join(tempfile.gettempdir(), "jarvis_vision_marked.png")
        img.save(out)
        return out

    # ---------------- vision model ----------------

    def _marks_text(self, marks: list[dict]) -> str:
        return "\n".join(
            f"  [{m['idx']}] {m['role']}" + (f" '{m['name']}'" if m["name"] else "")
            for m in marks
        )

    def _ask_vision(self, task: str, marked_img: str, marks: list[dict], history: list[str]) -> dict:
        """Ask the vision model (Claude CLI ONLY — per Zain, no third-party)
        for the next action."""
        hist = ("\n".join(history[-6:])) or "(abhi kuch nahi kiya)"
        user_text = (
            f"TASK: {task}\n\n"
            f"Ab tak ke steps:\n{hist}\n\n"
            f"Screen pe ye numbered elements hain:\n{self._marks_text(marks)}\n\n"
            f"Agla EK action JSON me do."
        )
        out = self._cli_vision(user_text, marked_img)
        return self._parse(out)

    def _cli_vision(self, user_text: str, img_path: str) -> str | None:
        import shutil
        import subprocess
        exe = shutil.which("claude.cmd") or shutil.which("claude")
        if not exe:
            return None
        prompt = (
            _VISION_SYSTEM + "\n\n"
            f"Image yahan hai (Read tool se kholo): {img_path}\n\n" + user_text
        )
        try:
            proc = subprocess.run(
                [exe, "-p", "--model", "opus", "--output-format", "json",
                 "--max-turns", "4", "--allowedTools", "Read",
                 "--strict-mcp-config", "--disallowedTools", "mcp__*"],
                input=prompt, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=60, shell=False,
            )
            if proc.returncode != 0:
                return None
            payload = json.loads((proc.stdout or "").strip())
            return payload.get("result", "")
        except Exception as e:
            log.warning("cli_vision_failed", err=str(e)[:120])
            return None

    @staticmethod
    def _parse(raw: str | None) -> dict:
        if not raw:
            return {"action": "fail", "reply": "vision model ne jawab nahi diya"}
        s = raw.strip()
        # pull the first {...} block
        i, j = s.find("{"), s.rfind("}")
        if i != -1 and j != -1 and j > i:
            s = s[i:j + 1]
        try:
            return json.loads(s)
        except Exception:
            return {"action": "fail", "reply": f"vision output parse nahi hua: {raw[:80]}"}

    # ---------------- execute ----------------

    def _execute(self, action: dict, marks: list[dict], offset: tuple[int, int]) -> str:
        from app.services.laptop_control.laptop_native import LaptopNative, _get_pyautogui
        nat = LaptopNative.get()
        pag = _get_pyautogui()
        kind = (action.get("action") or "").lower()
        ox, oy = offset

        def _click_mark(n):
            m = next((x for x in marks if x["idx"] == int(n)), None)
            if not m:
                return False
            pag.click(m["cx"], m["cy"])   # marks are absolute screen coords
            time.sleep(0.4)
            return True

        def _click_xy(action):
            # model gives IMAGE (cropped-window) coords → +offset = screen coords
            try:
                x = int(action.get("x")) + ox
                y = int(action.get("y")) + oy
            except (TypeError, ValueError):
                return False
            pag.click(x, y)
            time.sleep(0.4)
            return True

        if kind == "click":
            return "clicked" if _click_mark(action.get("mark")) else "mark nahi mila"
        if kind == "click_xy":
            return "clicked_xy" if _click_xy(action) else "xy galat"
        if kind == "type":
            nat.paste_text(action.get("text", ""), clear_first=False)
            return f"typed: {action.get('text','')[:40]}"
        if kind == "click_type":
            if not _click_mark(action.get("mark")):
                return "mark nahi mila"
            nat.paste_text(action.get("text", ""), clear_first=True)
            return f"clicked+typed: {action.get('text','')[:40]}"
        if kind == "click_type_xy":
            if not _click_xy(action):
                return "xy galat"
            nat.paste_text(action.get("text", ""), clear_first=True)
            return f"clicked_xy+typed: {action.get('text','')[:40]}"
        if kind == "key":
            key = (action.get("key") or "enter").lower()
            if "+" in key:
                pag.hotkey(*[p.strip() for p in key.split("+")])
            else:
                pag.press(key)
            return f"key: {key}"
        return "unknown action"

    # ---------------- main loop ----------------

    def run(self, task: str, max_steps: int = 9) -> dict:
        """See→act loop. Returns {ok, reply, steps}."""
        from app.services.task_control import clear_stop, is_stopped
        clear_stop()   # fresh task → drop any stale stop flag
        history: list[str] = []
        for step in range(1, max_steps + 1):
            if is_stopped():
                log.info("vision_stopped_by_user", step=step)
                return {"ok": False, "reply": "🛑 Boss, rok diya — aapne stop bola.", "steps": step}
            window = self._foreground_window()
            shot, offset = self._capture(window)
            marks = self._enumerate(window)
            if not marks:
                history.append(f"{step}. (koi element nahi mila screen pe)")
            marked = self._annotate(shot, marks, offset) if marks else shot
            try:
                action = self._ask_vision(task, marked, marks, history)
            finally:
                # privacy: delete the screenshots right after the model sees them
                for f in {shot, marked}:
                    try:
                        os.remove(f)
                    except OSError:
                        pass
            kind = (action.get("action") or "").lower()
            log.info("vision_step", step=step, action=kind, why=action.get("why", "")[:60])

            if kind == "done":
                return {"ok": True, "reply": action.get("reply", "Ho gaya."), "steps": step}
            if kind == "fail":
                return {"ok": False, "reply": action.get("reply", "Nahi ho saka."), "steps": step}

            result = self._execute(action, marks, offset)
            history.append(f"{step}. {kind} -> {result}")
            time.sleep(0.6)   # let the UI settle before the next screenshot

        return {"ok": False, "reply": f"{max_steps} steps me complete nahi hua — zara khud dekh lein.",
                "steps": max_steps}
