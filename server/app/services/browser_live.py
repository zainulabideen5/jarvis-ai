"""Live browser control — JARVIS tumhare APNE Chrome ko chalata hai (same login,
same session), DevTools Protocol (CDP) ke zariye. Yeh vision se NAHI — real DOM,
fast. Har step chat se trigger hota hai.

Kaise: Chrome ko ek dafa `--remote-debugging-port` ke saath kholna padta hai. Agar
woh port khula hai to seedha connect; warna user ke REAL profile pe Chrome ko
debug-mode mein (purane tabs `--restore-last-session` se wapas) relaunch karte hain.
Sab Playwright calls EK hi dedicated thread pe chalti hain (sync API thread-bound
hota hai) — isliye ek single-worker executor.

self-adaptive: chrome.exe + profile host se khud detect hote hain, kuch hardcode nahi.
"""
from __future__ import annotations

import os
import socket
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

from app.core.logging import get_logger

log = get_logger(__name__)

_DEFAULT_PORT = 9222


def _chrome_exe() -> str | None:
    cands = [
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                     r"Google\Chrome\Application\chrome.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                     r"Google\Chrome\Application\chrome.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""),
                     r"Google\Chrome\Application\chrome.exe"),
    ]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


def _default_profile() -> str:
    return os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "User Data")


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) == 0


class LiveBrowser:
    """One connected Chrome, driven from a single worker thread."""

    _inst: "LiveBrowser | None" = None

    @classmethod
    def get(cls) -> "LiveBrowser":
        if cls._inst is None:
            cls._inst = cls()
        return cls._inst

    def __init__(self, user_data_dir: str | None = None, port: int = _DEFAULT_PORT,
                 kill_existing: bool = True):
        self._port = port
        self._profile = user_data_dir or _default_profile()
        self._kill_existing = kill_existing
        self._ex = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pw")
        self._pw = None
        self._browser = None

    # ---- run any callable on the dedicated PW thread ----
    def _run(self, fn, *a, **k):
        return self._ex.submit(lambda: fn(*a, **k)).result()

    # ---------------- impl (run INSIDE the worker thread) ----------------
    def _ensure_impl(self, restore_url: str | None):
        from playwright.sync_api import sync_playwright
        if self._pw is None:
            self._pw = sync_playwright().start()
        if self._browser is not None:
            try:
                if self._browser.is_connected():
                    return {"ok": True, "reused": True}
            except Exception:
                self._browser = None

        if not _port_open(self._port):
            self._launch_chrome(restore_url)
            for _ in range(48):                       # up to ~12s for the port
                if _port_open(self._port):
                    break
                time.sleep(0.25)
            else:
                return {"ok": False, "reason": "debug_port_never_opened"}
        self._browser = self._pw.chromium.connect_over_cdp(
            f"http://127.0.0.1:{self._port}")
        return {"ok": bool(self._browser.is_connected()), "reused": False}

    def _launch_chrome(self, restore_url: str | None):
        exe = _chrome_exe()
        if not exe:
            raise RuntimeError("chrome.exe not found")
        # A running Chrome on the SAME profile ignores new --flags (singleton);
        # to enable the debug port we must fully close it first, then relaunch
        # with the same profile so logins/cookies stay.
        if self._kill_existing and self._profile == _default_profile():
            try:
                subprocess.run(["taskkill", "/F", "/IM", "chrome.exe"],
                               capture_output=True)
                time.sleep(1.5)
            except Exception as e:
                log.warning("chrome_kill_failed", err=str(e)[:120])
        args = [exe, f"--remote-debugging-port={self._port}",
                f"--user-data-dir={self._profile}", "--restore-last-session",
                "--no-first-run", "--no-default-browser-check"]
        if restore_url:
            args.append(restore_url)
        log.info("launching_debug_chrome", port=self._port)
        subprocess.Popen(args, close_fds=True)

    def _context(self):
        ctxs = self._browser.contexts
        return ctxs[0] if ctxs else self._browser.new_context()

    def _open_url_impl(self, url: str):
        ctx = self._context()
        pg = ctx.pages[0] if ctx.pages else ctx.new_page()
        pg.bring_to_front()
        pg.goto(url, wait_until="domcontentloaded", timeout=45000)
        return {"ok": True, "url": pg.url, "title": pg.title()}

    def _eval_impl(self, url: str | None, js: str):
        ctx = self._context()
        pg = ctx.pages[0] if ctx.pages else ctx.new_page()
        if url:
            pg.goto(url, wait_until="domcontentloaded", timeout=45000)
        return pg.evaluate(js)

    def _wa_send_file_impl(self, contact: str, filepath: str):
        """User ke APNE Chrome (CDP) ki khuli WhatsApp tab pe contact ko file bhejo.
        Deterministic (koi brain nahi). WhatsApp user ki session se logged-in hona chahiye."""
        if not os.path.isfile(filepath):
            return {"ok": False, "error": f"file nahi mili: {filepath}"}
        ctx = self._context()
        pg = None
        for p in ctx.pages:
            try:
                if "web.whatsapp.com" in (p.url or ""):
                    pg = p
                    break
            except Exception:
                pass
        if pg is None:
            pg = ctx.pages[0] if ctx.pages else ctx.new_page()
            pg.goto("https://web.whatsapp.com", wait_until="domcontentloaded",
                    timeout=45000)
        try:
            pg.bring_to_front()
        except Exception:
            pass
        state = "loading"
        for _ in range(25):
            state = pg.evaluate(
                "()=>{if(document.querySelector('div[contenteditable=true][data-tab=\"3\"]')"
                "||document.querySelector('[aria-label=\"Search input textbox\"]'))return 'ready';"
                "if(document.querySelector('canvas[aria-label]')||/scan the qr|link.*device|"
                "scan to log/i.test(document.body?document.body.innerText:''))return 'qr';"
                "return 'loading';}")
            if state in ("ready", "qr"):
                break
            pg.wait_for_timeout(1000)
        if state == "qr":
            return {"ok": False, "error": "is Chrome mein WhatsApp logged-in nahi (QR aa raha)"}
        if state != "ready":
            return {"ok": False, "error": "WhatsApp Web load nahi hua (timeout)"}
        try:
            search = (pg.query_selector('div[contenteditable="true"][data-tab="3"]')
                      or pg.query_selector('[aria-label="Search input textbox"]'))
            if not search:
                return {"ok": False, "error": "search box nahi mila"}
            search.click()
            pg.wait_for_timeout(400)
            pg.evaluate(
                "(t)=>{const s=document.querySelector("
                "'div[contenteditable=true][data-tab=\"3\"]')||document.querySelector("
                "'[aria-label=\"Search input textbox\"]');s.focus();"
                "document.execCommand('selectAll',false,null);"
                "document.execCommand('insertText',false,t);}", contact)
            pg.wait_for_timeout(2000)
        except Exception as e:
            return {"ok": False, "error": f"search fail: {str(e)[:120]}"}
        opened = pg.evaluate(
            "(name)=>{const n=name.toLowerCase();"
            "let e=[...document.querySelectorAll('span[title]')].find(x=>"
            "(x.getAttribute('title')||'').toLowerCase()===n);"
            "if(!e)e=[...document.querySelectorAll('span[title]')].find(x=>"
            "(x.getAttribute('title')||'').toLowerCase().includes(n));"
            "if(e){(e.closest('div[role=listitem]')||e).click();return true;}return false;}",
            contact)
        if not opened:
            return {"ok": False, "error": f"'{contact}' chat list mein nahi mila"}
        pg.wait_for_timeout(2000)
        pg.evaluate(
            "()=>{const a=document.querySelector('span[data-icon=\"plus-rounded\"]')"
            "||document.querySelector('span[data-icon=\"attach-menu-plus\"]')"
            "||document.querySelector('div[title=\"Attach\"]')"
            "||document.querySelector('span[data-icon=\"clip\"]')"
            "||document.querySelector('button[aria-label=\"Attach\"]');"
            "if(a){(a.closest('button')||a.closest('div[role=button]')||a).click();}}")
        pg.wait_for_timeout(1200)
        uploaded = False
        try:
            with pg.expect_file_chooser(timeout=8000) as fc:
                pg.evaluate(
                    "()=>{const it=[...document.querySelectorAll("
                    "'li,div[role=button],button,span,div')].find(e=>"
                    "/^document/i.test((e.innerText||'').trim()));if(it)it.click();}")
            fc.value.set_files(filepath)
            uploaded = True
        except Exception:
            try:
                inputs = pg.query_selector_all('input[type="file"]')
                target = None
                for inp in inputs:
                    acc = (inp.get_attribute("accept") or "")
                    if acc == "" or "*" in acc or "application" in acc:
                        target = inp
                        break
                target = target or (inputs[-1] if inputs else None)
                if target:
                    target.set_input_files(filepath)
                    uploaded = True
            except Exception as e:
                return {"ok": False, "error": f"upload fail: {str(e)[:120]}"}
        if not uploaded:
            return {"ok": False, "error": "file attach nahi ho paya"}
        pg.wait_for_timeout(3500)
        sent = pg.evaluate(
            "()=>{const s=document.querySelector('span[data-icon=\"send\"]')"
            "||document.querySelector('span[data-icon=\"wds-ic-send-filled\"]')"
            "||document.querySelector('div[role=button][aria-label=\"Send\"]');"
            "if(s){(s.closest('div[role=button]')||s.closest('button')||s).click();return true;}"
            "return false;}")
        if not sent:
            try:
                pg.keyboard.press("Enter")
            except Exception:
                pass
        pg.wait_for_timeout(2500)
        out = pg.evaluate("()=>document.querySelectorAll('#main .message-out').length")
        return {"ok": True, "out_messages": out, "send_clicked": bool(sent)}

    # ---------------- public (thread-hopping) ----------------
    def wa_send_file(self, contact: str, filepath: str) -> dict:
        """CDP se user ke Chrome ki WhatsApp pe file bhejo (logged-in session)."""
        e = self._run(self._ensure_impl, "https://web.whatsapp.com")
        if not e.get("ok"):
            return e
        return self._run(self._wa_send_file_impl, contact, filepath)

    def ensure(self, restore_url: str | None = None) -> dict:
        return self._run(self._ensure_impl, restore_url)

    def open_url(self, url: str, restore_url: str | None = None) -> dict:
        e = self._run(self._ensure_impl, restore_url)
        if not e.get("ok"):
            return e
        return self._run(self._open_url_impl, url)

    def eval_js(self, js: str, url: str | None = None) -> dict:
        e = self._run(self._ensure_impl, None)
        if not e.get("ok"):
            return e
        return {"ok": True, "result": self._run(self._eval_impl, url, js)}
