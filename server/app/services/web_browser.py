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
        # Crash/band hone ke baad stale context detect (e.g. window die ho gayi) → reset.
        if self._ctx is not None:
            try:
                _ = self._ctx.pages
            except Exception:
                self._ctx = None
        # AGAR context khula hai → HAMESHA reuse karo (naya launch / mode-switch
        # NAHI). Mode-switch har baar context close+reopen karta tha → WhatsApp
        # jaise logged-in sessions tut jaate the. Ek hi background browser, reuse.
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

    def _upload_impl(self, trigger: str, filepath: str):
        """GENERAL file/attachment upload — kisi bhi web app pe (WhatsApp Web, Teams,
        Gmail, koi bhi site). Browser ka native file-chooser khud handle hota hai —
        koi app hardcode nahi. trigger = attach/upload button ka TEXT ya CSS selector
        (ya seedha file-input selector). filepath = poora path."""
        if not os.path.exists(filepath):
            return {"ok": False, "error": f"file nahi mili: {filepath}"}
        pg = self._page()
        t = (trigger or "").strip()

        def _is_sel(s: str) -> bool:
            return s[:1] in (".", "#", "[") or s.startswith(
                ("button", "input", "a ", "div", "span", "label"))

        # 1) Agar trigger khud ek <input type=file> selector hai → seedha set.
        if t and _is_sel(t):
            try:
                el = pg.query_selector(t)
                if el and (el.get_attribute("type") or "").lower() == "file":
                    pg.set_input_files(t, filepath, timeout=8000)
                    pg.wait_for_timeout(1500)
                    return {"ok": True, "via": "set_input_files"}
            except Exception:
                pass

        # 2) GENERAL: trigger (attach/clip button) click → native file-chooser → set.
        try:
            with pg.expect_file_chooser(timeout=12000) as fc_info:
                if t and _is_sel(t):
                    pg.click(t, timeout=7000)
                elif t:
                    pg.get_by_text(t, exact=False).first.click(timeout=7000)
                else:
                    # trigger nahi diya → page ka pehla file-input/attach try.
                    pg.click("[data-testid*='attach'],[aria-label*='attach' i],"
                             "[title*='attach' i],input[type=file]", timeout=7000)
            fc = fc_info.value
            fc.set_files(filepath)
            pg.wait_for_timeout(1500)
            return {"ok": True, "via": "file_chooser"}
        except Exception as e:
            # 3) Fallback: page pe koi bhi input[type=file] ho to usme daal do.
            try:
                pg.set_input_files("input[type=file]", filepath, timeout=5000)
                pg.wait_for_timeout(1500)
                return {"ok": True, "via": "input_fallback"}
            except Exception as e2:
                return {"ok": False,
                        "error": f"{str(e)[:160]} | fallback: {str(e2)[:90]}"}

    def _wa_send_file_impl(self, contact: str, filepath: str):
        """WhatsApp Web pe kisi contact ko FILE bhejo — DETERMINISTIC (known
        selectors, koi brain nahi). Logged-in WA tab chahiye. General contact/file."""
        if not os.path.exists(filepath):
            return {"ok": False, "error": f"file nahi mili: {filepath}"}
        pg = self._page()
        if "web.whatsapp.com" not in (pg.url or ""):
            pg.goto("https://web.whatsapp.com", wait_until="domcontentloaded",
                    timeout=45000)
        # Search box ke MULTIPLE selectors (WhatsApp version-to-version badalta hai) —
        # general, taake update aaye tab bhi chale.
        search_sel = ("[role=textbox][aria-label*='Search'],"
                      "div[contenteditable='true'][data-tab='3'],"
                      "[aria-label='Search input textbox'],"
                      "div[contenteditable='true'][role='textbox']")
        # WA load hone do: search box / chat list (logged-in) YA QR — poll ~25s.
        state = "loading"
        for _ in range(25):
            state = pg.evaluate(
                "(sel)=>{if(document.querySelector(sel)"
                "||document.querySelector('[aria-label=\"Chat list\"]'))return 'ready';"
                "if(document.querySelector('canvas[aria-label]')||/scan the qr|link.*device|"
                "scan to log/i.test(document.body?document.body.innerText:''))return 'qr';"
                "return 'loading';}", search_sel)
            if state in ("ready", "qr"):
                break
            pg.wait_for_timeout(1000)
        if state == "qr":
            return {"ok": False,
                    "error": "WhatsApp logged-in nahi (QR aa raha) — ek baar scan karo, phir bachega"}
        if state != "ready":
            return {"ok": False, "error": "WhatsApp Web load nahi hua (timeout) — dobara try"}
        # 1) Search box pe click + keyboard se type (Lexical editor safe).
        try:
            search = pg.query_selector(search_sel)
            if not search:
                return {"ok": False, "error": "search box nahi mila"}
            search.click()
            pg.wait_for_timeout(500)
            try:
                pg.keyboard.press("Control+A")
                pg.keyboard.press("Delete")
            except Exception:
                pass
            pg.keyboard.type(contact, delay=20)
            pg.wait_for_timeout(2200)
        except Exception as e:
            return {"ok": False, "error": f"search fail: {str(e)[:120]}"}
        # 2) Result pe REAL click (JS .click() React pe nahi chalta — yeh bug tha).
        opened = False
        try:
            loc = pg.locator(f'span[title="{contact}"]').first
            loc.wait_for(state="visible", timeout=6000)
            loc.click()
            opened = True
        except Exception:
            try:
                pg.get_by_title(contact, exact=False).first.click(timeout=4000)
                opened = True
            except Exception:
                pass
        if not opened:
            return {"ok": False, "error": f"'{contact}' chat list mein nahi mila"}
        pg.wait_for_timeout(2200)
        compose_sel = ("footer [role=textbox],[aria-label*='Type a message' i],"
                       "div[contenteditable='true'][data-tab='10']")
        if not pg.query_selector(compose_sel):
            return {"ok": False, "error": f"'{contact}' ka chat khul nahi paya"}
        # 3) Attach button — REAL click. Icon span aria-hidden hota hai (Playwright
        #    click nahi karta) — isliye asli BUTTON[aria-label=Attach] click karo.
        try:
            pg.click('button[aria-label="Attach"]', timeout=6000)
        except Exception:
            try:
                pg.click('button[aria-label*="Attach" i],button[title*="Attach" i]',
                         timeout=4000)
            except Exception as e:
                return {"ok": False, "error": f"attach button nahi mila: {str(e)[:90]}"}
        pg.wait_for_timeout(1300)
        # 4) "Document" → native file-chooser → set file (REAL click).
        uploaded = False
        try:
            with pg.expect_file_chooser(timeout=12000) as fc:
                pg.get_by_role("menuitem", name="Document", exact=False).first.click(timeout=6000)
            fc.value.set_files(filepath)
            uploaded = True
        except Exception:
            try:
                with pg.expect_file_chooser(timeout=8000) as fc2:
                    pg.locator('button[role="menuitem"]:has-text("Document")').first.click(timeout=5000)
                fc2.value.set_files(filepath)
                uploaded = True
            except Exception as e:
                return {"ok": False, "error": f"Document upload fail: {str(e)[:110]}"}
        if not uploaded:
            return {"ok": False, "error": "Document upload nahi hua"}
        pg.wait_for_timeout(3500)   # preview + upload thumbnail
        # 5) Send — REAL click.
        try:
            pg.click('[data-icon="send"],[data-icon="wds-ic-send-filled"],'
                     'div[role=button][aria-label="Send"],button[aria-label="Send"]',
                     timeout=6000)
        except Exception:
            try:
                pg.keyboard.press("Enter")
            except Exception:
                pass
        # 6) HONEST verify — file ka NAAM last message mein aaya? (count lazy-load se
        #    false-pass deta tha; document bubble filename dikhata hai — wahi check.)
        token = os.path.splitext(os.path.basename(filepath))[0][:18].strip()
        ok = False
        for _ in range(15):
            pg.wait_for_timeout(1000)
            last = pg.evaluate(
                "()=>{const m=[...document.querySelectorAll('#main [data-id]')];"
                "const l=m[m.length-1];return l?(l.innerText||''):'';}")
            if token and token.lower() in (last or "").lower():
                ok = True
                break
        if not ok:
            return {"ok": False,
                    "error": f"send confirm NAHI hua — '{token}' wali file chat ke "
                             "last message mein nahi mili"}
        return {"ok": True, "verified": token}

    def _wa_open_chat(self, pg, contact):
        """WhatsApp ready-check + contact ka chat kholo (real click). (ok, err)."""
        if "web.whatsapp.com" not in (pg.url or ""):
            pg.goto("https://web.whatsapp.com", wait_until="domcontentloaded", timeout=45000)
        search_sel = ("[role=textbox][aria-label*='Search'],"
                      "div[contenteditable='true'][data-tab='3'],"
                      "[aria-label='Search input textbox'],"
                      "div[contenteditable='true'][role='textbox']")
        state = "loading"
        for _ in range(25):
            state = pg.evaluate(
                "(sel)=>{if(document.querySelector(sel)"
                "||document.querySelector('[aria-label=\"Chat list\"]'))return 'ready';"
                "if(document.querySelector('canvas[aria-label]')||/scan the qr|link.*device|"
                "scan to log/i.test(document.body?document.body.innerText:''))return 'qr';"
                "return 'loading';}", search_sel)
            if state in ("ready", "qr"):
                break
            pg.wait_for_timeout(1000)
        if state == "qr":
            return False, "WhatsApp logged-in nahi (QR aa raha) — ek baar scan karo"
        if state != "ready":
            return False, "WhatsApp Web load nahi hua (timeout)"
        try:
            sb = pg.query_selector(search_sel)
            if not sb:
                return False, "search box nahi mila"
            sb.click()
            pg.wait_for_timeout(500)
            try:
                pg.keyboard.press("Control+A")
                pg.keyboard.press("Delete")
            except Exception:
                pass
            pg.keyboard.type(contact, delay=20)
            pg.wait_for_timeout(2200)
        except Exception as e:
            return False, f"search fail: {str(e)[:100]}"
        opened = False
        try:
            loc = pg.locator(f'span[title="{contact}"]').first
            loc.wait_for(state="visible", timeout=6000)
            loc.click()
            opened = True
        except Exception:
            try:
                pg.get_by_title(contact, exact=False).first.click(timeout=4000)
                opened = True
            except Exception:
                pass
        if not opened:
            return False, f"'{contact}' chat list mein nahi mila"
        pg.wait_for_timeout(2000)
        if not pg.query_selector("footer [role=textbox],[aria-label*='Type a message' i],"
                                 "div[contenteditable='true'][data-tab='10']"):
            return False, f"'{contact}' ka chat khul nahi paya"
        return True, ""

    def _wa_send_text_impl(self, contact: str, message: str):
        """WhatsApp Web pe contact ko TEXT message bhejo (real-click, honest verify)."""
        pg = self._page()
        ok, err = self._wa_open_chat(pg, contact)
        if not ok:
            return {"ok": False, "error": err, "stage": "open"}
        comp = pg.query_selector("footer [role=textbox],[aria-label*='Type a message' i],"
                                 "div[contenteditable='true'][data-tab='10']")
        try:
            comp.click()
            pg.wait_for_timeout(300)
            # compose box clear karo (pichla bacha text na jude — "khudsalam" bug).
            try:
                pg.keyboard.press("Control+A")
                pg.keyboard.press("Delete")
            except Exception:
                pass
            pg.keyboard.type(message, delay=6)
            pg.wait_for_timeout(400)
            pg.keyboard.press("Enter")
        except Exception as e:
            return {"ok": False, "error": f"type/send fail: {str(e)[:100]}"}
        token = (message or "")[:20].strip()
        ok2 = False
        for _ in range(10):
            pg.wait_for_timeout(800)
            last = pg.evaluate(
                "()=>{const m=[...document.querySelectorAll('#main [data-id]')];"
                "const l=m[m.length-1];return l?(l.innerText||''):'';}")
            if token and token.lower() in (last or "").lower():
                ok2 = True
                break
        if not ok2:
            return {"ok": False,
                    "error": "message send confirm NAHI hua (last message mein nahi mila)"}
        return {"ok": True, "msg": f"{contact} ko WhatsApp message bhej diya"}

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

    def upload_file(self, trigger: str, filepath: str) -> dict:
        if self._ctx is None:
            return {"ok": False, "error": "koi page khula nahi — pehle web_open/web_login"}
        return self._run(self._upload_impl, trigger, filepath)

    def wa_send_file(self, contact: str, filepath: str, visible: bool = True) -> dict:
        """WhatsApp Web pe contact ko file bhejo (deterministic). Background-friendly:
        agar context pehle se khula hai to reuse (mode-switch nahi)."""
        e = self._run(self._ensure_impl, not visible)
        if not e.get("ok"):
            return e
        return self._run(self._wa_send_file_impl, contact, filepath)

    def wa_send_text(self, contact: str, message: str) -> dict:
        """WhatsApp Web pe contact ko TEXT message bhejo (background, real-click)."""
        e = self._run(self._ensure_impl, True)   # headless background (reuse if open)
        if not e.get("ok"):
            return e
        return self._run(self._wa_send_text_impl, contact, message)

    def login(self, url: str) -> dict:
        """HEADED (visible) browser kholo taake user ek dafa login kar le —
        session profile mein save ho jayega, phir background mein bhi logged-in."""
        e = self._run(self._ensure_impl, False)      # headless=False
        if not e.get("ok"):
            return e
        return self._run(self._goto_impl, url)

    def close(self) -> dict:
        return self._run(self._close_impl)
