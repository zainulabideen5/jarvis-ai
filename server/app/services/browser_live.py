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

    # ---------------- public (thread-hopping) ----------------
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
