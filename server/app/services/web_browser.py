"""Web automation — JARVIS ka apna ALAG Playwright browser (user ke Chrome se
alag). Interactive sites (Foodinn jaisi React dropdown/menu, ya koi bhi site) ko
REAL DOM se chalata hai: page kholo, parho, click/fill karo, ya JS chala kar
dropdown set + menu nikalo. Vision se NAHI — reliable.

- Default HEADLESS (background, koi window nahi) — menu/prices/info ke liye perfect.
- Persistent profile (data/web_profile) — jis site pe login karo woh YAAD rehta hai.
- login(url): ek dafa HEADED (visible) khol kar user login kar le → session save →
  uske baad background mein bhi logged-in.

Sab Playwright calls EK dedicated thread pe (sync API thread-bound). General +
self-adaptive: koi site/path hardcode nahi.
"""
from __future__ import annotations

import json as _json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.core.logging import get_logger

log = get_logger(__name__)

# Apna alag profile (user ke asli Chrome profile se alag) — logins yahan persist.
_PROFILE_DIR = str(Path(__file__).resolve().parents[2] / "data" / "web_profile")
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# read() ke liye page se structured info nikalne wala JS (bounded — context na phat-e).
_READ_JS = r"""
() => {
  const clip = (s, n) => (s || '').replace(/\s+/g, ' ').trim().slice(0, n);
  const vis = (el) => { const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0; };
  const btns = [];
  document.querySelectorAll('button,[role="button"],a').forEach(el => {
    if (!vis(el)) return;
    const t = clip(el.innerText || el.getAttribute('aria-label') || '', 60);
    if (t) btns.push(t);
  });
  const inputs = [];
  document.querySelectorAll('input,select,textarea').forEach(el => {
    if (!vis(el)) return;
    inputs.push(clip(el.getAttribute('placeholder') || el.getAttribute('name')
      || el.getAttribute('aria-label') || el.type || el.tagName, 40));
  });
  const uniq = (a) => [...new Set(a.filter(Boolean))].slice(0, 60);
  return {
    title: document.title,
    url: location.href,
    text: clip(document.body ? document.body.innerText : '', 6000),
    clickables: uniq(btns),
    inputs: uniq(inputs),
  };
}
"""


class WebBrowser:
    """Ek persistent Playwright browser, single worker thread se driven."""

    _inst: "WebBrowser | None" = None

    @classmethod
    def get(cls) -> "WebBrowser":
        if cls._inst is None:
            cls._inst = cls()
        return cls._inst

    def __init__(self) -> None:
        self._ex = ThreadPoolExecutor(max_workers=1, thread_name_prefix="webpw")
        self._pw = None
        self._ctx = None          # persistent context (browser)
        self._headless = True

    def _run(self, fn, *a, **k):
        return self._ex.submit(lambda: fn(*a, **k)).result()

    # ---------------- impl (worker thread ke andar) ----------------
    def _ensure_impl(self, headless: bool):
        from playwright.sync_api import sync_playwright
        if self._pw is None:
            self._pw = sync_playwright().start()
        # Agar context khula hai par mode badal gaya → band kar ke dobara.
        if self._ctx is not None and self._headless != headless:
            try:
                self._ctx.close()
            except Exception:
                pass
            self._ctx = None
        if self._ctx is not None:
            return {"ok": True, "reused": True}
        os.makedirs(_PROFILE_DIR, exist_ok=True)
        self._headless = headless
        self._ctx = self._pw.chromium.launch_persistent_context(
            _PROFILE_DIR, headless=headless, user_agent=_UA,
            viewport={"width": 1280, "height": 820},
            args=["--no-first-run", "--no-default-browser-check",
                  "--disable-blink-features=AutomationControlled"],
        )
        return {"ok": True, "reused": False}

    def _page(self):
        pg = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        return pg

    def _goto_impl(self, url: str):
        pg = self._page()
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        pg.goto(url, wait_until="domcontentloaded", timeout=45000)
        pg.wait_for_timeout(1200)      # dynamic content thoda settle ho
        return {"ok": True, "url": pg.url, "title": pg.title()}

    def _read_impl(self):
        pg = self._page()
        data = pg.evaluate(_READ_JS)
        return {"ok": True, **data}

    def _click_impl(self, target: str):
        pg = self._page()
        # CSS selector (starts with . # [ or tag-ish) → seedha; warna VISIBLE TEXT.
        try:
            if target.strip()[:1] in (".", "#", "[") or target.strip().startswith(
                    ("button", "input", "a ", "div", "span")):
                pg.click(target, timeout=8000)
            else:
                pg.get_by_text(target, exact=False).first.click(timeout=8000)
            pg.wait_for_timeout(1000)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    def _fill_impl(self, selector: str, text: str):
        pg = self._page()
        try:
            pg.fill(selector, text, timeout=8000)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    def _eval_impl(self, js: str):
        return self._page().evaluate(js)

    def _press_impl(self, key: str, selector: str = ""):
        pg = self._page()
        try:
            if selector:
                pg.press(selector, key, timeout=8000)
            else:
                pg.keyboard.press(key)
            pg.wait_for_timeout(900)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    def _close_impl(self):
        try:
            if self._ctx:
                self._ctx.close()
        finally:
            self._ctx = None
        return {"ok": True}

    # ---------------- public (thread-hopping) ----------------
    def goto(self, url: str, headless: bool = True) -> dict:
        e = self._run(self._ensure_impl, headless)
        if not e.get("ok"):
            return e
        return self._run(self._goto_impl, url)

    def read(self) -> dict:
        if self._ctx is None:
            return {"ok": False, "error": "koi page khula nahi — pehle goto karo"}
        return self._run(self._read_impl)

    def click(self, target: str) -> dict:
        if self._ctx is None:
            return {"ok": False, "error": "koi page khula nahi"}
        return self._run(self._click_impl, target)

    def fill(self, selector: str, text: str) -> dict:
        if self._ctx is None:
            return {"ok": False, "error": "koi page khula nahi"}
        return self._run(self._fill_impl, selector, text)

    def eval_js(self, js: str) -> dict:
        if self._ctx is None:
            return {"ok": False, "error": "koi page khula nahi"}
        try:
            return {"ok": True, "result": self._run(self._eval_impl, js)}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    def press(self, key: str, selector: str = "") -> dict:
        if self._ctx is None:
            return {"ok": False, "error": "koi page khula nahi"}
        return self._run(self._press_impl, key, selector)

    def login(self, url: str) -> dict:
        """HEADED (visible) browser kholo taake user ek dafa login kar le —
        session profile mein save ho jayega, phir background mein bhi logged-in."""
        e = self._run(self._ensure_impl, False)      # headless=False
        if not e.get("ok"):
            return e
        return self._run(self._goto_impl, url)

    def close(self) -> dict:
        return self._run(self._close_impl)
