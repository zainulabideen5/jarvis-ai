"""WhatsApp Web automation via Playwright headless Chromium.

Truly-background WhatsApp send — no Chrome window, no focus steal, no extension.
A dedicated background thread runs its own asyncio event loop and owns the
Chromium process. Sync code from anywhere (chat dispatcher, intent handlers,
HTTP routes) submits work via `run_coroutine_threadsafe`, so we never hit the
"Playwright objects bound to a different event loop" crash that happens when
you create a new loop per call.

Why a dedicated thread instead of using the FastAPI main loop directly:
    - The chat dispatcher runs in a worker thread (asyncio.to_thread). It
      can't await coroutines on the main loop without `run_coroutine_threadsafe`.
    - Reusing the main loop would still leak Playwright tasks into FastAPI's
      request lifecycle. Cleaner to isolate.

Session persistence:
    First run: open VISIBLE Chromium, scan QR on phone → cookies saved to
    `server/data/wa_session/`. From then on every send is headless + silent.

Pipeline (per send):
    1. Submit coroutine to the dedicated loop via run_coroutine_threadsafe
    2. Inside the coroutine — ensure browser is alive (lazy launch)
    3. Open WhatsApp Web (cached page, fast after first load)
    4. Open chat by name (keyboard search) OR phone (wa.me deeplink)
    5. Send text OR upload attachment + caption
    6. Verify by scanning chat tail for filename / message
"""

from __future__ import annotations

import asyncio
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from app.core.logging import get_logger

log = get_logger(__name__)

# Session lives at server/data/wa_session — same level as jarvis.db.
# wa_playwright.py is at app/services/laptop_control/, so parents:
#   [0] laptop_control, [1] services, [2] app, [3] server
_SESSION_DIR = Path(__file__).resolve().parents[3] / "data" / "wa_session"


# Stealth user agent — vanilla Chromium UA gets flagged by WA Web. Match a
# real Chrome on Windows to blend in.
_STEALTH_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


class WhatsAppPlaywright:
    """Singleton holding a persistent Chromium in a dedicated background thread."""

    _instance: "WhatsAppPlaywright | None" = None

    def __init__(self):
        # Threading primitives — safe across any caller, any event loop
        self._start_lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        # All Playwright objects live INSIDE the dedicated loop's thread.
        # Never touch them from outside this thread.
        self._playwright = None
        self._browser = None        # PersistentBrowserContext (shared)
        self._page = None           # WhatsApp Web page in that context
        self._teams_page = None     # Teams page (lazy — shared browser)
        self._gmail_page = None     # Gmail page (lazy — shared browser)
        self._trello_page = None    # Trello page (lazy — shared browser)
        # Async lock used only inside the dedicated loop to serialize sends.
        # Lazy-initialized in _ensure_loop_async because asyncio.Lock binds
        # to the running loop on creation.
        self._send_lock: asyncio.Lock | None = None

    @classmethod
    def get(cls) -> "WhatsAppPlaywright":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ==================================================================
    # Dedicated background loop / thread
    # ==================================================================

    def _start_loop_thread(self) -> None:
        """Start (once) the dedicated thread that owns the Playwright loop.

        Idempotent — safe to call multiple times; lock serializes the cold
        start. After this returns, self._loop is a running event loop.
        """
        with self._start_lock:
            if self._loop is not None and self._thread is not None and self._thread.is_alive():
                return
            self._loop = asyncio.new_event_loop()
            loop_ref = self._loop

            def _runner():
                asyncio.set_event_loop(loop_ref)
                try:
                    loop_ref.run_forever()
                except Exception as e:
                    log.warning("wa_playwright_loop_died", error=str(e)[:200])
                finally:
                    try:
                        loop_ref.close()
                    except Exception:
                        pass
                    # CRITICAL — clear instance refs so future _submit calls
                    # detect a dead loop and restart cleanly. Without this,
                    # _loop is still non-None and stale, _submit hangs.
                    if self._loop is loop_ref:
                        self._loop = None
                    if self._thread is threading.current_thread():
                        self._thread = None
                    log.info("wa_playwright_loop_thread_exited")

            self._thread = threading.Thread(
                target=_runner, name="wa-playwright-loop", daemon=True,
            )
            self._thread.start()
            # Wait briefly until the loop is running to avoid race with first call
            for _ in range(50):
                if self._loop is not None and self._loop.is_running():
                    return
                time.sleep(0.02)

    def _submit(self, coro, timeout: float = 120.0):
        """Run a coroutine on the dedicated loop and return its result.

        Safe to call from ANY thread / event loop. Blocks the caller until
        the coroutine completes or `timeout` seconds elapse. If timeout hits,
        we CANCEL the orphan task — otherwise it'd keep holding `_send_lock`
        and deadlock every subsequent call. If the loop has died (thread
        crashed), we raise immediately instead of hanging on a dead future.
        """
        import concurrent.futures
        self._start_loop_thread()
        # Sanity: loop must be running, else we'd hang forever
        if self._loop is None or self._loop.is_closed() or not self._loop.is_running():
            raise RuntimeError("Playwright loop thread not alive — restart server.")
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return fut.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            # Cancel the orphan task so it releases _send_lock and doesn't
            # block the next caller. fut.cancel() schedules the cancellation
            # on the target loop.
            try:
                fut.cancel()
            except Exception:
                pass
            raise

    # ==================================================================
    # Browser lifecycle (runs INSIDE dedicated loop)
    # ==================================================================

    async def _ensure_lock_async(self) -> None:
        if self._send_lock is None:
            self._send_lock = asyncio.Lock()

    async def _ensure_browser_async(self, headless: bool = False, *, minimize_after_launch: bool = True) -> None:
        """Make sure the persistent browser + WA page are alive.

        Default mode is HEADED (visible window) so the same Chromium can be
        used for first-time logins to Teams/Gmail/Trello tabs later. The
        window is minimized to the taskbar immediately after launch so it
        doesn't disturb the user — but it stays alive and accessible.

        Pass `minimize_after_launch=False` only when the caller explicitly
        wants the window visible (e.g. during the WhatsApp QR scan flow).
        """
        if self._browser is not None and self._page is not None:
            return

        # Shut down any half-state from a previous failed launch
        await self._shutdown_browser_async()

        from playwright.async_api import async_playwright
        self._playwright = await async_playwright().start()

        _SESSION_DIR.mkdir(parents=True, exist_ok=True)
        # Persistent context — cookies + storage saved to user_data_dir so
        # the QR scan + service logins are truly one-time per machine.
        try:
            self._browser = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(_SESSION_DIR),
                headless=headless,
                viewport={"width": 1280, "height": 800},
                user_agent=_STEALTH_UA,
                args=[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-features=IsolateOrigins,site-per-process",
                ],
            )
        except Exception as e:
            err = str(e)
            # Most common cause: `playwright install chromium` was never run.
            # Surface an actionable message instead of the raw stack.
            if "Executable doesn't exist" in err or "playwright install" in err.lower():
                raise RuntimeError(
                    "Chromium binary nahi mila. Server terminal mein chala: "
                    "cd server && venv\\Scripts\\python -m playwright install chromium"
                ) from e
            raise
        # Mask webdriver flag on each new page (WA Web checks this)
        await self._browser.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )

        pages = self._browser.pages
        self._page = pages[0] if pages else await self._browser.new_page()

        # If we launched headed and the caller wants background mode,
        # minimize the window so it lives in the taskbar without taking
        # screen space. Skipped for headless (no window exists).
        if not headless and minimize_after_launch:
            await self._minimize_window_async()

    async def _minimize_window_async(self) -> None:
        """Minimize the Chromium window TO THE TASKBAR (icon stays visible
        so the user can click to restore it). User explicitly asked:
        "chrome hide bhi nahi ho, bas minimize, mujhay nazar aana chahiye".

        Strategy: Windows API ShowWindow(hwnd, SW_SHOWMINNOACTIVE) — this
        is the ONLY method that reliably leaves the taskbar icon visible
        across all Chrome/Chromium versions. CDP Browser.setWindowBounds
        with "minimized" sometimes fully HIDES the window on certain GPU
        configurations. Win32 SW_SHOWMINNOACTIVE = 7, which minimizes
        without grabbing focus and ALWAYS keeps the taskbar entry.

        Idempotent: safe to call when already minimized or when browser
        is in an odd state — failures are logged but never raised.
        """
        # First: get the Chromium window's title so we can find its HWND.
        # Retry up to 3 times with a tiny wait, because SPA-driven pages
        # (WA Web especially) often report empty title for the first
        # 1-2 seconds while React mounts.
        title = ""
        page = self._page
        if page is None:
            return
        try:
            if page.is_closed():
                return
        except Exception:
            return
        for attempt in range(3):
            try:
                title = (await page.title()) or ""
            except Exception:
                title = ""
            if title.strip():
                break
            await asyncio.sleep(0.5)

        # Windows API minimize — falls back to generic markers (whatsapp/chromium)
        # if title is still empty, so it works even on cold launches.
        try:
            found = await asyncio.to_thread(self._win32_minimize_chromium, title)
            if found:
                log.info("playwright_window_minimized_via_win32", title=title[:80])
                return
            log.debug("win32_minimize_no_window_found", title=title[:80])
        except Exception as e:
            log.debug("win32_minimize_failed", error=str(e)[:120])

        # Fallback: CDP (older path) if win32 didn't find/minimize the window
        try:
            page = self._page
            if page is None or page.is_closed():
                return
            cdp = await page.context.new_cdp_session(page)
            try:
                info = await cdp.send("Browser.getWindowForTarget")
                window_id = info.get("windowId")
                if window_id is not None:
                    await cdp.send(
                        "Browser.setWindowBounds",
                        {"windowId": window_id, "bounds": {"windowState": "minimized"}},
                    )
                    log.info("playwright_window_minimized_via_cdp_fallback")
            finally:
                try:
                    await cdp.detach()
                except Exception:
                    pass
        except Exception as e:
            log.debug("minimize_window_failed", error=str(e)[:120])

    def _win32_minimize_chromium(self, page_title: str) -> bool:
        """Find the Chromium window via Windows API and minimize it with
        SW_SHOWMINNOACTIVE so the taskbar icon stays visible.
        Returns True if a window was found and minimized.
        """
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        SW_SHOWMINNOACTIVE = 7

        # ShowWindowAsync — minimize without grabbing focus
        ShowWindowAsync = user32.ShowWindowAsync
        ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
        ShowWindowAsync.restype = wintypes.BOOL

        IsWindowVisible = user32.IsWindowVisible
        IsWindowVisible.argtypes = [wintypes.HWND]
        IsWindowVisible.restype = wintypes.BOOL

        GetWindowTextW = user32.GetWindowTextW
        GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        GetWindowTextW.restype = ctypes.c_int

        GetWindowThreadProcessId = user32.GetWindowThreadProcessId
        GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        GetWindowThreadProcessId.restype = wintypes.DWORD

        # EnumWindows callback prototype
        EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        EnumWindows = user32.EnumWindows
        EnumWindows.argtypes = [EnumWindowsProc, wintypes.LPARAM]
        EnumWindows.restype = wintypes.BOOL

        # Search markers: prefer page title, fall back to "Chromium"/"WhatsApp"
        markers = []
        if page_title and page_title.strip():
            markers.append(page_title.strip().lower())
        markers.extend(["whatsapp", "chromium"])

        matched_hwnd = [None]

        def _cb(hwnd, lparam):
            try:
                if not IsWindowVisible(hwnd):
                    return True  # continue
                length = GetWindowTextW(hwnd, None, 0)
                if length <= 0:
                    return True
                buf = ctypes.create_unicode_buffer(length + 1)
                GetWindowTextW(hwnd, buf, length + 1)
                wt = (buf.value or "").lower()
                if not wt:
                    return True
                for m in markers:
                    if m and m in wt:
                        matched_hwnd[0] = hwnd
                        return False  # stop enum
            except Exception:
                pass
            return True

        EnumWindows(EnumWindowsProc(_cb), 0)
        if matched_hwnd[0] is None:
            return False
        ShowWindowAsync(matched_hwnd[0], SW_SHOWMINNOACTIVE)
        return True

    async def _shutdown_browser_async(self) -> None:
        """Close browser + playwright cleanly. Tolerant of any state."""
        try:
            if self._browser is not None:
                await self._browser.close()
        except Exception:
            pass
        try:
            if self._playwright is not None:
                await self._playwright.stop()
        except Exception:
            pass
        self._browser = None
        self._playwright = None
        self._page = None
        self._teams_page = None
        self._gmail_page = None
        self._trello_page = None

    async def _open_whatsapp_async(self, timeout_ms: int = 30000) -> None:
        """Navigate to WhatsApp Web. WA Web triggers its OWN client-side
        navigation/redirect on first load (loads its SPA), which destroys
        the JS execution context. We wait for that to settle before letting
        the caller proceed.
        """
        page = self._page
        cur = (page.url or "").lower()
        if "web.whatsapp.com" not in cur:
            try:
                await page.goto("https://web.whatsapp.com/", timeout=timeout_ms,
                                wait_until="domcontentloaded")
            except Exception as e:
                log.debug("wa_goto_partial", error=str(e)[:120])
        # Wait for network-idle so WA's SPA boot completes before we poll.
        # WA's initial navigation can take 5-15s on cold load.
        try:
            await page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            # networkidle can time out on slow connections — that's OK,
            # the ready-poll will keep trying until it succeeds.
            pass
        # Tiny grace period so the SPA finishes mounting before query_selector
        await asyncio.sleep(1.5)

    async def _check_logged_in_async(self) -> tuple[bool, str]:
        """Check WhatsApp Web state. Returns one of:
            (True,  "ok")          → logged in (QR gone, app shell visible)
            (False, "QR_NEEDED")   → QR canvas visible, user needs to scan
            (False, "loading")     → page still booting / mid-navigation
            (False, "window_closed") → user manually closed the Chromium

        Detection strategy: rather than matching specific selectors that WA
        rotates (data-testid → aria-label → class name churn), we look for
        the QR canvas first. If QR is GONE and the page is still alive on
        web.whatsapp.com with some app shell rendered, we treat that as a
        successful login. This is robust across WA Web UI updates.
        """
        page = self._page
        # Detect manual window close — page or context detached
        if page is None or page.is_closed():
            return False, "window_closed"
        try:
            if self._browser is not None and not self._browser.pages:
                return False, "window_closed"
        except Exception:
            return False, "window_closed"
        try:
            # 1) QR canvas present → still logged out, user must scan
            if await page.query_selector('canvas[aria-label*="QR" i], canvas[aria-label*="Scan" i]'):
                return False, "QR_NEEDED"

            # 2) "Loading your chats" or progress indicator → in transition
            #    Don't return "ok" yet; wait for it to clear so cookies are
            #    fully written before we close the browser.
            progress = await page.query_selector(
                'progress[aria-label*="Loading" i], '
                'div[aria-label*="Loading your chats" i], '
                'div[role="progressbar"]'
            )
            if progress:
                return False, "loading"

            # 3) Any of these widely-stable post-login markers → logged in
            login_markers = (
                "#pane-side",
                "#side",
                'div[aria-label*="Chat list" i]',
                'div[aria-label*="Chats list" i]',
                'header[data-testid*="chatlist" i]',
                'div[data-testid="chat-list"]',
                '[data-testid="default-user"]',
                'div[role="grid"]',
                'div[role="textbox"][contenteditable="true"][data-tab="3"]',
            )
            for sel in login_markers:
                if await page.query_selector(sel):
                    return True, "ok"

            # 4) Fallback — QR gone, no progress, on web.whatsapp.com, with
            #    page body content > some threshold → likely logged in but
            #    WA changed the markers we know about. Trust the URL +
            #    no-QR signal. This is the resilience layer that prevents
            #    "selector drift" from breaking everyone's setup.
            try:
                url = (page.url or "").lower()
                if "web.whatsapp.com" in url:
                    # Check page has substantial DOM (not blank loading screen)
                    body_text_len = await page.evaluate("() => (document.body && document.body.innerText || '').length")
                    if body_text_len and body_text_len > 200:
                        log.info("wa_login_via_fallback_marker", url=url, body_len=body_text_len)
                        return True, "ok"
            except Exception:
                pass

            return False, "loading"
        except Exception as e:
            err = str(e).lower()
            if "closed" in err or "target closed" in err or "has been closed" in err:
                return False, "window_closed"
            log.debug("check_logged_in_transient", error=str(e)[:80])
            return False, "loading"

    async def _wait_for_ready_async(self, timeout_sec: int = 35) -> tuple[bool, str]:
        deadline = time.monotonic() + timeout_sec
        last = "loading"
        while time.monotonic() < deadline:
            ok, status = await self._check_logged_in_async()
            if ok:
                return True, "ok"
            if status == "window_closed":
                return False, "window_closed"
            last = status
            await asyncio.sleep(1)
        return False, last  # "QR_NEEDED" / "loading"

    # ==================================================================
    # Self-healing helpers — automatic DOM dump + retry on failure
    # ==================================================================

    async def _auto_dump_on_failure(self, service: str, op: str, page) -> None:
        """Capture a structured DOM dump to the server log whenever a send
        operation fails. This means every failure auto-diagnoses itself —
        no need for user to manually run a diagnose endpoint. The dump
        appears in `.uv-err.log` and a fix can be written from that alone.
        """
        try:
            if page is None:
                return
            try:
                if page.is_closed():
                    log.info("auto_dump_skipped_page_closed", service=service, op=op)
                    return
            except Exception:
                return
            data = await page.evaluate(
                """() => {
                    return {
                        url: location.href,
                        title: document.title,
                        bodyLen: (document.body && document.body.innerText || '').length,
                        bodyTail: (document.body && document.body.innerText || '').slice(-2500),
                        inputs: Array.from(document.querySelectorAll('input[type="file"]')).map(el => ({
                            accept: el.getAttribute('accept') || '',
                            name: el.getAttribute('name') || '',
                        })),
                        buttons: Array.from(document.querySelectorAll('button, div[role="button"]')).slice(0, 30).map(el => ({
                            al: el.getAttribute('aria-label') || '',
                            title: el.getAttribute('title') || '',
                            dataTid: el.getAttribute('data-tid') || el.getAttribute('data-testid') || '',
                            dataIcon: el.getAttribute('data-icon') || '',
                            text: (el.innerText || '').trim().substring(0, 40),
                        })).filter(b => b.al || b.dataTid || b.dataIcon || b.text),
                        menuItems: Array.from(document.querySelectorAll('[role="menuitem"]')).map(el => ({
                            al: el.getAttribute('aria-label') || '',
                            text: (el.innerText || '').trim().substring(0, 40),
                        })),
                    };
                }"""
            )
            log.warning(
                "auto_dump_on_failure",
                service=service,
                op=op,
                url=data.get("url", "")[:100],
                title=data.get("title", "")[:80],
                body_len=data.get("bodyLen", 0),
                body_tail=str(data.get("bodyTail", ""))[:600],
                inputs=str(data.get("inputs", []))[:300],
                buttons=str(data.get("buttons", []))[:1500],
                menu_items=str(data.get("menuItems", []))[:600],
            )
        except Exception as e:
            log.debug("auto_dump_internal_fail", service=service, op=op, error=str(e)[:100])

    async def _heal_page_async(self, service: str) -> bool:
        """Auto-recover a closed/stale page WITHOUT killing the shared
        browser. Earlier we set `self._page = None` then called
        `_ensure_browser_async` — but that path's "is browser alive" check
        could fall through to `_shutdown_browser_async`, taking down the
        entire shared Chromium (Teams + Gmail + Trello would all die just
        to refresh one stale tab). Now we open a fresh page in the same
        context if the existing page ref is closed.
        """
        try:
            if self._browser is None:
                # No browser at all → cold launch is the only path
                if service == "whatsapp":
                    await self._ensure_browser_async(headless=False, minimize_after_launch=True)
                    await self._open_whatsapp_async(timeout_ms=20000)
                    ok, _ = await self._wait_for_ready_async(timeout_sec=20)
                    return ok
                elif service == "teams":
                    await self._ensure_teams_page_async()
                    ok, _ = await self._wait_teams_ready_async(timeout_sec=20)
                    return ok
                elif service == "gmail":
                    await self._ensure_gmail_page_async()
                    ok, _ = await self._wait_gmail_ready_async(timeout_sec=20)
                    return ok
                elif service == "trello":
                    await self._ensure_trello_page_async()
                    ok, _ = await self._wait_trello_ready_async(timeout_sec=20)
                    return ok
                return False

            # Browser alive — refresh just the stale tab. The _ensure_*_page_async
            # methods now (with the try/except guard added today) clear the stale
            # ref themselves when they detect is_closed(), so simply calling them
            # is enough — they will create a fresh page in the existing context.
            if service == "whatsapp":
                if self._page is not None:
                    try:
                        if self._page.is_closed():
                            self._page = None
                    except Exception:
                        self._page = None
                # Open WA freshly if needed
                if self._page is None:
                    self._page = await self._browser.new_page()
                    try:
                        await self._page.goto("https://web.whatsapp.com/", timeout=30000, wait_until="domcontentloaded")
                    except Exception:
                        pass
                    await asyncio.sleep(1.5)
                ok, _ = await self._wait_for_ready_async(timeout_sec=20)
                return ok
            elif service == "teams":
                await self._ensure_teams_page_async()
                ok, _ = await self._wait_teams_ready_async(timeout_sec=20)
                return ok
            elif service == "gmail":
                await self._ensure_gmail_page_async()
                ok, _ = await self._wait_gmail_ready_async(timeout_sec=20)
                return ok
            elif service == "trello":
                await self._ensure_trello_page_async()
                ok, _ = await self._wait_trello_ready_async(timeout_sec=20)
                return ok
        except Exception as e:
            log.warning("heal_page_failed", service=service, error=str(e)[:120])
        return False

    # ==================================================================
    # Public sync API — call from anywhere
    # ==================================================================

    def first_time_login_sync(self, qr_timeout_sec: int = 120) -> tuple[bool, str]:
        """Open a VISIBLE Chromium so the user can scan the WhatsApp QR code.

        After successful login (chat list appears within `qr_timeout_sec`),
        cookies persist on disk and all subsequent sends are headless.
        Safe to call from any thread / event loop.
        """
        return self._submit(self._first_time_login_async(qr_timeout_sec), timeout=qr_timeout_sec + 30)

    async def _first_time_login_async(self, qr_timeout_sec: int) -> tuple[bool, str]:
        # FAST PATH — already logged in? Don't disturb the running browser.
        # User clicked Connect a second time → just confirm and report back.
        # We check the cookies-on-disk first (cheap), and if alive ALSO check
        # the live browser. This prevents the common foot-gun where clicking
        # Connect again wipes out a perfectly-good logged-in session.
        if self.session_exists():
            try:
                # Browser is alive AND logged in? Nothing to do.
                if self._browser is not None and self._page is not None and not self._page.is_closed():
                    ok_now, status_now = await self._check_logged_in_async()
                    if ok_now:
                        return True, "Already connected. Background mein chal raha hai."
                # Cookies exist but no live browser — bring one up minimized
                # so future sends are fast. No QR scan needed because the
                # saved cookies auto-login.
                await self._ensure_browser_async(headless=False, minimize_after_launch=True)
                await self._open_whatsapp_async(timeout_ms=20000)
                ok_w, _ = await self._wait_for_ready_async(timeout_sec=25)
                if ok_w:
                    return True, "Already connected — background browser warm kar diya."
                # Cookies present but auto-login failed (expired?) — fall
                # through to the full QR flow below. Don't wipe; let the
                # user scan again to refresh the session.
                log.info("session_cookies_present_but_auto_login_failed")
            except Exception as e:
                log.info("session_revalidate_failed", error=str(e)[:120])
                # Fall through to QR flow

        # Close any existing browser handle, but DO NOT wipe the session dir.
        # If a previous attempt saved real cookies, keep them — wiping would
        # destroy a valid login the user already completed.
        await self._shutdown_browser_async()
        try:
            # VISIBLE during QR scan (so user can actually scan the code)
            await self._ensure_browser_async(headless=False, minimize_after_launch=False)
            await self._open_whatsapp_async(timeout_ms=20000)
            ok, status = await self._wait_for_ready_async(timeout_sec=qr_timeout_sec)
            if not ok:
                # Don't wipe the session dir on failure — the user may have
                # actually scanned and logged in even if our detector missed
                # the new markers. Just close the browser; status endpoint
                # will reflect reality based on cookies on disk.
                await self._shutdown_browser_async()
                if status == "window_closed":
                    return False, "Tumne Chromium window band kar di. Dobara button click kar."
                if status == "QR_NEEDED":
                    return False, "QR scan nahi hua time mein. Dobara button click kar."
                return False, "WhatsApp Web load nahi hua. Internet check kar phir dobara."

            # SUCCESS — give WA a few extra seconds to finish writing
            # all auth cookies + IndexedDB to user_data_dir.
            await asyncio.sleep(5)

            # DO NOT close the browser. The user explicitly asked for the
            # SAME Chromium to stay alive forever — this becomes the home
            # for WhatsApp, Teams, Gmail, Trello (one-browser-for-all).
            # We just MINIMIZE the window to background so it doesn't
            # take screen space, but the process and all logged-in tabs
            # remain available for instant automation.
            await self._minimize_window_async()
            return True, "Login saved. Browser background mein minimize ho gaya. Tabs add karte raho."
        except Exception as e:
            log.warning("first_time_login_failed", error=str(e)[:200])
            await self._shutdown_browser_async()
            # NEVER wipe on exception — could be a transient error mid-login.
            return False, f"Login flow fail: {e}"

    def send_message_sync(
        self,
        recipient: str = "",
        phone: str = "",
        message: str = "",
        attachment_path: str = "",
        timeout_sec: int = 90,
    ) -> tuple[bool, str]:
        """Send a text or file via WhatsApp Web (headless).

        Safe to call from any thread / event loop. Returns (success, message).
        """
        return self._submit(
            self._send_message_async(
                recipient=recipient, phone=phone,
                message=message, attachment_path=attachment_path,
            ),
            timeout=timeout_sec,
        )

    async def _send_message_async(
        self,
        recipient: str,
        phone: str,
        message: str,
        attachment_path: str,
    ) -> tuple[bool, str]:
        await self._ensure_lock_async()
        async with self._send_lock:
            try:
                # Use the persistent headed-minimized Chromium. If it isn't
                # alive yet (e.g. server just restarted), launch it now —
                # it will come up minimized so the user's screen isn't
                # disturbed during the send.
                await self._ensure_browser_async(headless=False, minimize_after_launch=True)
                await self._open_whatsapp_async()
                ok, status = await self._wait_for_ready_async(timeout_sec=35)
                if not ok:
                    if status == "QR_NEEDED":
                        return False, (
                            "WhatsApp Web logged out — pehle /api/whatsapp/setup call kar "
                            "to QR scan kar lo (one-time)."
                        )
                    return False, "WhatsApp Web ready nahi hua 35 sec mein."

                # ----- Open the chat -----
                opened = False
                last_err = ""
                if recipient and recipient.strip():
                    try:
                        await self._open_chat_by_name_async(recipient.strip())
                        opened = True
                    except Exception as e:
                        last_err = str(e)
                        log.info("pw_name_open_failed", error=last_err[:120])
                        # Press Escape so any leftover search state doesn't bleed
                        # into the next attempt.
                        try: await self._page.keyboard.press("Escape")
                        except Exception: pass

                if not opened and phone:
                    cleaned = re.sub(r"\D", "", str(phone))
                    if cleaned:
                        ok2, msg = await self._open_chat_by_phone_async(cleaned)
                        if ok2:
                            opened = True
                        else:
                            last_err = msg

                if not opened:
                    return False, last_err or "Chat open nahi hua."

                # ----- Send -----
                if attachment_path:
                    return await self._send_attachment_async(attachment_path, message)
                return await self._send_text_async(message)

            except Exception as e:
                log.warning("wa_playwright_send_failed", error=str(e)[:200])
                return False, f"Playwright fail: {e}"

    # ==================================================================
    # Chat-open + send primitives (inside dedicated loop)
    # ==================================================================

    async def _open_chat_by_name_async(self, name: str) -> None:
        """Universal chat opener — works for ANY name/number in user's contacts.

        Strategy: do EXACTLY what a human user does in WhatsApp Web:
          1. Open the search box.
          2. Type the name.
          3. Wait for filtered results.
          4. VERIFY at least one visible result contains the name (this is
             the ONLY safety net we need — prevents sending to the wrong
             chat when the name isn't in contacts).
          5. ArrowDown + Enter to open the top result. This is robust
             because WA itself decides which is the best match.

        We deliberately AVOID trying to read the conversation header to
        "verify" the right chat opened — WA's header DOM changes between
        releases and header-text matching has been fragile. Trusting WA's
        own search ranking is more reliable than trying to outsmart it.
        """
        page = self._page
        name_clean = (name or "").strip()
        if not name_clean:
            raise RuntimeError("Recipient name empty")

        # Capture which chat (if any) is currently active. We'll compare
        # after the search+Enter to ensure something actually changed —
        # if active chat is the same, search didn't open anything new.
        active_before = await self._current_active_chat_marker_async()

        # 1. CLOSE any existing overlay/message-search panel first —
        # multiple Escapes dismiss popups, the chat-info panel, the
        # message-search overlay, etc. WA's Ctrl+Alt+/ hotkey now opens
        # the "Search messages" (within-chat) overlay on newer builds,
        # which is the wrong search. So we explicitly target the LEFT-pane
        # "Search or start a new chat" input instead.
        for _ in range(3):
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
            await asyncio.sleep(0.15)

        # 2. Find the LEFT chat-search input ("Search or start a new chat").
        # Try multiple specific selectors in priority order. The key is to
        # AVOID the within-chat "Search messages" overlay (right side).
        handle = None
        candidate_selectors = [
            # Most specific — placeholder text match
            'div[role="textbox"][aria-placeholder="Search or start a new chat"]',
            'div[contenteditable="true"][aria-placeholder="Search or start a new chat"]',
            'div[role="textbox"][title="Search or start a new chat"]',
            # Loose placeholder match
            'div[role="textbox"][aria-placeholder*="Search or start" i]',
            'div[role="textbox"][title*="Search or start" i]',
            'div[contenteditable="true"][aria-placeholder*="Search" i]',
            # Side-pane scoped (avoid message-search overlay)
            '#side div[contenteditable="true"][role="textbox"]',
            '#side div[role="textbox"]',
            '#side input[type="search"]',
            # Legacy data-tab=3 was always the chat search
            'div[contenteditable="true"][data-tab="3"]',
        ]
        for sel in candidate_selectors:
            try:
                el = await page.query_selector(sel)
            except Exception:
                el = None
            if el:
                # Sanity check — make sure it's not the message-search overlay
                try:
                    info = await el.evaluate(
                        """(el) => {
                            const ph = (el.getAttribute('aria-placeholder') || '').toLowerCase();
                            const al = (el.getAttribute('aria-label') || '').toLowerCase();
                            const t = (el.getAttribute('title') || '').toLowerCase();
                            const blob = ph + ' ' + al + ' ' + t;
                            return {
                                isMessageSearch: blob.includes('search messages') || blob.includes('search this chat'),
                                hasSearchHint: blob.includes('search'),
                            };
                        }"""
                    )
                except Exception:
                    info = {"isMessageSearch": False, "hasSearchHint": True}
                if info.get("isMessageSearch"):
                    continue
                handle = el
                log.info("pw_search_box_found", selector=sel)
                break

        if handle is None:
            # Last resort — Playwright locator by placeholder text
            try:
                loc = page.get_by_placeholder("Search or start a new chat")
                cnt = await loc.count()
                if cnt > 0:
                    handle = await loc.first.element_handle()
                    log.info("pw_search_box_found", selector="get_by_placeholder")
            except Exception:
                pass

        if handle is None:
            raise RuntimeError("Chat-search box nahi mila (left pane).")

        try:
            await handle.scroll_into_view_if_needed(timeout=1500)
        except Exception:
            pass
        try:
            await handle.click(force=False, timeout=3000)
        except Exception:
            try:
                await handle.click(force=True, timeout=2000)
            except Exception as e:
                raise RuntimeError(f"Chat-search click fail: {e}")

        # 2. Clear + type the name
        await asyncio.sleep(0.3)
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Backspace")
        await asyncio.sleep(0.2)
        await page.keyboard.type(name_clean, delay=20)

        # 3. Wait for search results to settle
        await asyncio.sleep(1.5)

        # 4. SAFETY NET — count visible results that actually contain the name
        # as text. If zero, fail loudly instead of pressing Enter (which
        # would route to the previously-open chat).
        match_count = await page.evaluate(
            """(target) => {
                const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                const t = norm(target);
                if (!t) return 0;
                // Scan the side pane for ANY visible text matching the query.
                // We don't care about role/selector — text is what matters.
                const pane = document.querySelector('#pane-side, #side, [aria-label*="Chat list" i], div[role="grid"]');
                if (!pane) return 0;
                const txt = norm(pane.innerText || '');
                if (!txt.includes(t)) return 0;
                // Count rough "lines" in pane to estimate result density.
                const lines = (pane.innerText || '').split('\\n').filter(l => norm(l).includes(t));
                return lines.length;
            }""",
            name_clean,
        )

        log.info(
            "pw_chat_search_match_count",
            searched=name_clean,
            visible_matches=match_count,
        )

        if not match_count or match_count < 1:
            raise RuntimeError(f"'{name_clean}' WA contacts mein nahi mila.")

        # 5. ArrowDown (selects first result) + Enter (opens it). This is
        # the human-flow and works across WA Web versions.
        await page.keyboard.press("ArrowDown")
        await asyncio.sleep(0.25)
        await page.keyboard.press("Enter")
        await asyncio.sleep(1.2)

        # 6. Final verification — A chat MUST be open after search.
        # Earlier we required the marker to CHANGE, but that broke when the
        # user resends to the SAME contact (same Zaid → same Zaid). Now we
        # accept either:
        #   (a) Marker changed (we navigated to a new chat), OR
        #   (b) Marker unchanged BUT it points to a real chat (compose visible
        #       + marker doesn't look like the chat-list panel title).
        # The compose-box check below is the real safety net.
        active_after = await self._current_active_chat_marker_async()
        # "hdr:chats" / empty markers indicate WA is showing the chat list,
        # not an actual chat. Block those.
        looks_like_panel = (
            not active_after
            or active_after.startswith("hdr:chats")
            or active_after.endswith(":chats")
        )
        if looks_like_panel:
            raise RuntimeError(
                f"'{name_clean}' search ke baad koi chat open nahi hua — "
                f"contact list mein hai bhi ya nahi check kar."
            )

        if not await self._find_compose_async():
            raise RuntimeError(f"'{name_clean}' chat compose box nahi mila.")
        return

    async def _current_active_chat_marker_async(self) -> str:
        """Return a fingerprint of the currently-open chat. Used to detect
        whether a search actually OPENED a new chat or just left the
        previous one active. We avoid using the contact name (too fragile
        across UI versions) — instead we use structural attributes like
        data-id or the chat's URL hash if any.
        """
        page = self._page
        try:
            marker = await page.evaluate(
                """() => {
                    // Try a few structural markers in priority order
                    const hdr = document.querySelector('header[data-testid="conversation-header"], header');
                    if (!hdr) return '';
                    // data-id on header or its parent is the most stable ID
                    let n = hdr;
                    for (let i = 0; i < 4 && n; i++, n = n.parentElement) {
                        if (n.getAttribute) {
                            const id = n.getAttribute('data-id');
                            if (id) return 'did:' + id;
                        }
                    }
                    // Fall back to whatever the first span[title] in the header says.
                    const sp = hdr.querySelector('span[title]');
                    if (sp) {
                        const t = sp.getAttribute('title') || '';
                        if (t.trim()) return 'spt:' + t.trim().toLowerCase();
                    }
                    // Or the entire header's textContent as a last resort.
                    return 'hdr:' + (hdr.innerText || '').trim().substring(0, 80).toLowerCase();
                }"""
            )
            return marker or ""
        except Exception:
            return ""

    async def _open_chat_by_phone_async(self, phone_digits: str) -> tuple[bool, str]:
        page = self._page
        await page.goto(
            f"https://web.whatsapp.com/send?phone={phone_digits}",
            timeout=30000, wait_until="domcontentloaded",
        )
        await asyncio.sleep(2.5)
        body = await page.evaluate("document.body.innerText")
        if re.search(r"isn.?t on WhatsApp|phone number shared.*invalid", body, re.I):
            return False, f"+{phone_digits} WhatsApp pe registered nahi."
        compose = await self._find_compose_async(timeout=15)
        if compose is None:
            return False, "Chat load nahi hua deeplink ke baad."
        return True, "ok"

    async def _find_compose_async(self, timeout: float = 10.0):
        page = self._page
        sels = [
            'div[contenteditable="true"][data-tab="10"]',
            'div[role="textbox"][contenteditable="true"][aria-placeholder*="Type" i]',
            'div[role="textbox"][contenteditable="true"][data-lexical-editor="true"]',
        ]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for sel in sels:
                el = await page.query_selector(sel)
                if el:
                    return el
            await asyncio.sleep(0.3)
        return None

    async def _send_text_async(self, message: str) -> tuple[bool, str]:
        page = self._page
        compose = await self._find_compose_async()
        if compose is None:
            return False, "Compose box nahi mila."
        await compose.click()
        await asyncio.sleep(0.2)
        await page.keyboard.type(message, delay=10)
        await asyncio.sleep(0.4)
        await page.keyboard.press("Enter")
        await asyncio.sleep(1.5)
        # Honest verification — if the message we just typed isn't in the
        # chat tail, the send didn't land. Returning True with "incomplete"
        # earlier was a false-positive that the user (rightfully) called out.
        body = await page.evaluate("document.body.innerText")
        if message and message.strip() in body[-3000:]:
            return True, "Bhej diya (verified)"
        return False, "Send confirm nahi hua — message chat tail mein nahi dikha."

    async def _send_attachment_async(self, file_path: str, caption: str) -> tuple[bool, str]:
        """Upload + send a file. Wrapped in try/finally so a half-completed
        flow doesn't leave the attach menu open — that would break the NEXT
        send. On any failure, we press Escape to dismiss any open overlay.
        """
        page = self._page
        if not os.path.exists(file_path):
            return False, f"File path nahi mila: {file_path}"

        sent_ok = False
        try:
            return await self._send_attachment_inner(page, file_path, caption)
        finally:
            # If we didn't successfully send, clear any leftover attach menu /
            # preview overlay so the next call starts from a clean state.
            try:
                # Multiple Escapes — first dismisses menu, second the preview
                await page.keyboard.press("Escape")
                await asyncio.sleep(0.15)
                await page.keyboard.press("Escape")
            except Exception:
                pass

    async def _send_attachment_inner(self, page, file_path: str, caption: str) -> tuple[bool, str]:
        # ----- Step 1: click the attach button -----
        clicked = False
        for sel in (
            '[data-icon="plus-rounded"]', '[data-icon="plus"]',
            '[data-icon="attach-menu-plus"]', '[data-icon="clip"]',
            '[data-icon="attach"]', '[data-icon="paperclip"]',
            'button[title*="Attach" i]', 'button[aria-label*="Attach" i]',
            'div[role="button"][aria-label*="Attach" i]',
            'div[title*="Attach" i]',
            'span[data-icon*="attach" i]',
        ):
            btn = await page.query_selector(sel)
            if btn:
                try:
                    await btn.click()
                    clicked = True
                    break
                except Exception:
                    continue
        if not clicked:
            return False, "Attach button nahi mila."

        # ----- Step 2: wait for the menu items to appear -----
        # WA Web's new UI: menu items have role="menuitem" with aria-label
        # like "Document", "Photos & videos", "Camera", "Audio", "Sticker"
        await asyncio.sleep(1.2)

        # ----- Step 3: pick the RIGHT menu item by aria-label -----
        ext = os.path.splitext(file_path)[1].lower()
        is_media = ext in {".jpg",".jpeg",".png",".gif",".webp",".heic",".mp4",".mov",".3gp",".webm",".mkv"}
        is_audio = ext in {".mp3",".wav",".ogg",".m4a",".aac",".flac"}

        # Menu-item label preferences (in priority order)
        if is_media:
            target_labels = [r'Photos?\s*&\s*[Vv]ideos?', r'Photos?', r'Media', r'Image']
        elif is_audio:
            target_labels = [r'Audio', r'Music', r'Voice']
        else:
            # Document / PDF / DOCX / spreadsheet / anything else
            target_labels = [r'Document', r'File']

        menu_item = None
        for pat in target_labels:
            # aria-label match (most reliable)
            sel = f'[role="menuitem"][aria-label]'
            items = await page.query_selector_all(sel)
            for it in items:
                try:
                    al = (await it.get_attribute("aria-label")) or ""
                    if re.search(pat, al, re.IGNORECASE):
                        menu_item = it
                        break
                except Exception:
                    continue
            if menu_item:
                break

        # Fallback: match by visible text in menuitem
        if menu_item is None:
            items = await page.query_selector_all('[role="menuitem"]')
            for it in items:
                try:
                    txt = (await it.inner_text()) or ""
                    for pat in target_labels:
                        if re.search(pat, txt, re.IGNORECASE):
                            menu_item = it
                            break
                    if menu_item:
                        break
                except Exception:
                    continue

        if menu_item is None:
            return False, f"Menu item '{target_labels[0]}' nahi mila WA attach menu mein."

        # ----- Step 4: click menu item — Playwright intercepts the file
        # chooser dialog so we can upload the file directly, no native
        # picker ever opens (true headless / background friendly). -----
        try:
            async with page.expect_file_chooser(timeout=15000) as fc_info:
                await menu_item.click()
            file_chooser = await fc_info.value
            await file_chooser.set_files(file_path)
            log.info(
                "wa_attach_menu_clicked",
                ext=ext,
                target_label=target_labels[0],
            )
        except Exception as e:
            # Fallback — some WA versions still expose a direct input we can
            # set_input_files() on. Try that as a last resort.
            try:
                inp = await page.query_selector('input[type="file"]')
                if inp is None:
                    return False, f"File chooser intercept fail + no input fallback: {e}"
                await inp.set_input_files(file_path)
                log.info("wa_attach_fallback_input_used", ext=ext)
            except Exception as e2:
                return False, f"File upload fail: {e2}"

        await asyncio.sleep(3.0)

        # Optional caption in preview
        if caption and caption.strip():
            for sel in (
                'div[contenteditable="true"][data-tab="10"]',
                'div[role="textbox"][contenteditable="true"][aria-placeholder*="caption" i]',
                'div[role="textbox"][contenteditable="true"]',
            ):
                cap = await page.query_selector(sel)
                if cap:
                    try:
                        await cap.click()
                        await asyncio.sleep(0.2)
                        await page.keyboard.type(caption, delay=10)
                        await asyncio.sleep(0.3)
                    except Exception:
                        pass
                    break

        # Click green Send. We click the PARENT button (the role=button or
        # button element), NOT the inner icon span — clicking just the span
        # often doesn't fire the React handler. Also some WA versions have
        # the icon nested inside a clickable wrapper that needs the right
        # event target.
        send_clicked = False
        # Tier 1: explicit button elements (preferred — parent handles click)
        button_selectors = (
            'div[role="button"][aria-label="Send"]',
            'button[aria-label="Send"]',
            'div[role="button"][aria-label*="Send" i]',
            'button[aria-label*="Send" i]',
            'div[role="button"][data-tab="11"]',
            'button[data-tab="11"]',
        )
        for sel in button_selectors:
            el = await page.query_selector(sel)
            if el:
                try:
                    await el.click()
                    send_clicked = True
                    break
                except Exception:
                    continue

        # Tier 2: fallback — find the send icon, click its closest button parent
        if not send_clicked:
            for icon_sel in ('span[data-icon="send"]', 'span[data-icon="wds-ic-send-filled"]'):
                icon = await page.query_selector(icon_sel)
                if icon:
                    try:
                        # JS-resolve the clickable ancestor and click it
                        clicked = await page.evaluate(
                            """(el) => {
                                let n = el;
                                while (n && n !== document.body) {
                                    if (n.matches('div[role="button"], button')) {
                                        n.click();
                                        return true;
                                    }
                                    n = n.parentElement;
                                }
                                el.click();
                                return true;
                            }""",
                            icon,
                        )
                        if clicked:
                            send_clicked = True
                            break
                    except Exception:
                        continue

        if not send_clicked:
            return False, "Preview Send button nahi mila — file send nahi hua."

        # Wait + verify with a TIMED POLL — large files (videos, PDFs) can
        # take 5-15s to fully upload and appear in the chat tail. We poll
        # every 1.5s for up to 18s, checking both filename presence in the
        # chat list AND whether the preview overlay has closed.
        fname = os.path.basename(file_path).lower()
        fname_no_ext = os.path.splitext(fname)[0].lower()
        caption_lc = (caption or "").strip().lower()

        preview_closed_at = None
        for _ in range(12):  # 12 * 1.5 = 18 sec budget
            await asyncio.sleep(1.5)
            try:
                body = await page.evaluate("document.body.innerText")
            except Exception:
                body = ""
            tail = body[-4000:].lower() if body else ""

            # Strong signal — filename / no-ext / caption appears in chat tail
            if fname and fname in tail:
                return True, "File bhej diya (verified — filename mila)"
            if fname_no_ext and len(fname_no_ext) > 3 and fname_no_ext in tail:
                return True, "File bhej diya (verified — name mila)"
            if caption_lc and len(caption_lc) > 2 and caption_lc in tail:
                return True, "File bhej diya (verified — caption mila)"

            # Track when preview closed so we know upload is in progress
            if preview_closed_at is None:
                if (await self._find_compose_async(timeout=1)) is not None:
                    preview_closed_at = time.monotonic()

        # 18s timeout — last-resort check
        if preview_closed_at is not None:
            # Preview closed at some point — file was at least submitted.
            # Could not verify final delivery but WA accepted it.
            return True, "File send hua (preview close ho gaya, verify timeout)"
        return False, "Preview abhi bhi khuli — file send confirm nahi hua. (Send button click hua but processing nahi shuru — file unsupported ho sakti?)"

    # ==================================================================
    # Diagnostics
    # ==================================================================

    @classmethod
    def session_exists(cls) -> bool:
        """Detect whether a Playwright WhatsApp session has been set up.

        Playwright's persistent context lays down a Chrome-profile-shaped
        directory at user_data_dir. The canonical "user has logged in" signal
        is the SQLite Cookies file at `Default/Cookies`. We check that
        explicitly — older code used a recursive name match which could
        false-positive on cache files named "cookies" inside browser cache
        subdirs.
        """
        if not _SESSION_DIR.exists():
            return False
        # Newer Chromium (Playwright 1.40+) writes cookies under
        # Default/Network/Cookies. Older versions used Default/Cookies.
        # Check both to stay forward + backward compatible.
        candidates = [
            _SESSION_DIR / "Default" / "Network" / "Cookies",
            _SESSION_DIR / "Default" / "Cookies",
        ]
        for cookies_file in candidates:
            if cookies_file.exists() and cookies_file.is_file() and cookies_file.stat().st_size > 0:
                return True
        # Last-resort fallback: walk for any non-cache Cookies file
        for p in _SESSION_DIR.rglob("Cookies"):
            if p.is_file() and p.stat().st_size > 0 and "Cache" not in str(p) and "Safe Browsing" not in str(p):
                return True
        return False

    def is_background_alive(self) -> bool:
        """Is the headless background Chromium currently running?

        Used by the dashboard to show a live "Background: ON" badge so the
        user knows JARVIS is actively listening for WhatsApp commands
        without launching a new window per send.
        """
        try:
            if self._browser is None or self._page is None:
                return False
            if self._page.is_closed():
                return False
            return True
        except Exception:
            return False

    # ==================================================================
    # Teams (Microsoft Teams Web) — shares the same Chromium browser
    # ==================================================================

    async def _ensure_teams_page_async(self):
        """Open a teams.microsoft.com page in the SAME browser context as
        WhatsApp. Same cookies dir → user logs in once, all future sends
        reuse the session. Safely handles the case where the user closed
        the Teams tab manually — re-opens it instead of throwing.
        """
        # Browser must be alive first (shared with WA)
        await self._ensure_browser_async(headless=False, minimize_after_launch=True)
        # Stale-ref guard — page may have been closed by user
        try:
            if self._teams_page is not None and not self._teams_page.is_closed():
                return self._teams_page
        except Exception:
            self._teams_page = None
        # Reuse an existing teams tab if one is already open in the context
        for p in (self._browser.pages or []):
            try:
                u = (p.url or "").lower()
                if "teams.microsoft.com" in u or "teams.live.com" in u:
                    self._teams_page = p
                    return p
            except Exception:
                continue
        # Otherwise open a fresh tab
        self._teams_page = await self._browser.new_page()
        try:
            await self._teams_page.goto("https://teams.microsoft.com/", timeout=30000, wait_until="domcontentloaded")
        except Exception as e:
            log.debug("teams_goto_partial", error=str(e)[:120])
        try:
            await self._teams_page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        await asyncio.sleep(1.5)
        return self._teams_page

    async def _check_teams_logged_in_async(self) -> tuple[bool, str]:
        """Check Teams login state. Looks for the chat sidebar / app bar
        as a "logged in" signal. Login pages show O365 sign-in URLs.
        """
        page = self._teams_page
        if page is None or page.is_closed():
            return False, "no_page"
        try:
            url = (page.url or "").lower()
            if "login.microsoftonline.com" in url or "login.live.com" in url:
                return False, "login_needed"
            # Common Teams app shell markers
            markers = (
                'div[data-tid="app-bar"]',
                'div[data-tid="app-bar-container"]',
                'div[role="navigation"][aria-label*="App bar" i]',
                'button[data-tid="appBarChatButton"]',
                'div[id="leftPane"]',
                'div[data-tid*="leftRail"]',
                # Newer Teams shell
                'app-launcher',
                'div[id="main"]',
            )
            for sel in markers:
                if await page.query_selector(sel):
                    return True, "ok"
            # Fallback — page has substantial DOM and URL is teams.microsoft.com
            if "teams.microsoft.com" in url or "teams.live.com" in url:
                body_len = await page.evaluate("() => (document.body && document.body.innerText || '').length")
                if body_len and body_len > 500:
                    return True, "ok"
            return False, "loading"
        except Exception as e:
            err = str(e).lower()
            if "closed" in err or "target closed" in err:
                return False, "window_closed"
            return False, "loading"

    async def _wait_teams_ready_async(self, timeout_sec: int = 60) -> tuple[bool, str]:
        deadline = time.monotonic() + timeout_sec
        last = "loading"
        while time.monotonic() < deadline:
            ok, status = await self._check_teams_logged_in_async()
            if ok:
                return True, "ok"
            if status == "window_closed":
                return False, "window_closed"
            last = status
            await asyncio.sleep(1.5)
        return False, last

    def teams_first_time_login_sync(self, timeout_sec: int = 180) -> tuple[bool, str]:
        """Open Teams visible (in the same Chromium) so user can sign in
        with their Microsoft account. Cookies persist after login.
        """
        return self._submit(self._teams_first_time_login_async(timeout_sec), timeout=timeout_sec + 30)

    # ==================================================================
    # Gmail (mail.google.com) — shares the same Chromium browser
    # ==================================================================

    async def _ensure_gmail_page_async(self):
        """Open mail.google.com in the SAME browser context as WhatsApp.
        Safely re-opens if user closed the tab manually."""
        await self._ensure_browser_async(headless=False, minimize_after_launch=True)
        try:
            if self._gmail_page is not None and not self._gmail_page.is_closed():
                return self._gmail_page
        except Exception:
            self._gmail_page = None
        # Reuse existing Gmail tab if any
        for p in (self._browser.pages or []):
            try:
                u = (p.url or "").lower()
                if "mail.google.com" in u:
                    self._gmail_page = p
                    return p
            except Exception:
                continue
        self._gmail_page = await self._browser.new_page()
        try:
            await self._gmail_page.goto("https://mail.google.com/", timeout=30000, wait_until="domcontentloaded")
        except Exception as e:
            log.debug("gmail_goto_partial", error=str(e)[:120])
        try:
            await self._gmail_page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        await asyncio.sleep(1.5)
        return self._gmail_page

    async def _check_gmail_logged_in_async(self) -> tuple[bool, str]:
        page = self._gmail_page
        if page is None or page.is_closed():
            return False, "no_page"
        try:
            url = (page.url or "").lower()
            if "accounts.google.com" in url or "signin" in url:
                return False, "login_needed"
            markers = (
                'div[role="navigation"][aria-label*="Inbox" i]',
                'div[role="button"][aria-label*="Compose" i]',
                'div[gh="cm"]',  # Compose button (legacy gh attribute)
                'div[role="navigation"][aria-label*="Main menu" i]',
                'div[role="tabpanel"][aria-label*="Inbox" i]',
            )
            for sel in markers:
                if await page.query_selector(sel):
                    return True, "ok"
            if "mail.google.com" in url:
                body_len = await page.evaluate("() => (document.body && document.body.innerText || '').length")
                if body_len and body_len > 500:
                    return True, "ok"
            return False, "loading"
        except Exception as e:
            err = str(e).lower()
            if "closed" in err or "target closed" in err:
                return False, "window_closed"
            return False, "loading"

    async def _wait_gmail_ready_async(self, timeout_sec: int = 60) -> tuple[bool, str]:
        deadline = time.monotonic() + timeout_sec
        last = "loading"
        while time.monotonic() < deadline:
            ok, status = await self._check_gmail_logged_in_async()
            if ok:
                return True, "ok"
            if status == "window_closed":
                return False, "window_closed"
            last = status
            await asyncio.sleep(1.5)
        return False, last

    @classmethod
    def gmail_session_exists(cls) -> bool:
        """Best-effort check whether Gmail has ever logged in on this machine.
        We look for a "google.com" host cookie row marker in the persistent
        session dir. If the cookies file doesn't even exist, no session.

        Note: this returns True only after the user has actually loaded
        mail.google.com at least once in the persistent context — that's
        the only way Gmail cookies land in the file. Returning False means
        Gmail send via Playwright will need the workspace setup first.
        """
        # Cookie DB exists? (basic gate)
        if not cls.session_exists():
            return False
        # Check the SQLite cookies for a google.com row — cheap probe
        try:
            import sqlite3
            cookies_paths = (
                _SESSION_DIR / "Default" / "Network" / "Cookies",
                _SESSION_DIR / "Default" / "Cookies",
            )
            for p in cookies_paths:
                if not p.exists() or p.stat().st_size == 0:
                    continue
                try:
                    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=2)
                    cur = conn.cursor()
                    cur.execute(
                        "SELECT 1 FROM cookies WHERE host_key LIKE '%google.com' LIMIT 1"
                    )
                    has = cur.fetchone() is not None
                    conn.close()
                    if has:
                        return True
                except Exception:
                    continue
            return False
        except Exception:
            # If sqlite probe fails, fall back to "cookies dir exists" — safer
            # to attempt Playwright and let the ready-check decide than to
            # falsely deny a working session.
            return True

    def is_gmail_alive(self) -> bool:
        try:
            if self._gmail_page is None or self._gmail_page.is_closed():
                return False
            return True
        except Exception:
            return False

    def gmail_send_email_sync(
        self,
        to: str,
        subject: str = "",
        body: str = "",
        attachment_path: str = "",
        timeout_sec: int = 120,
    ) -> tuple[bool, str]:
        return self._submit(
            self._gmail_send_email_async(to=to, subject=subject, body=body, attachment_path=attachment_path),
            timeout=timeout_sec,
        )

    async def _gmail_send_email_async(self, to: str, subject: str, body: str, attachment_path: str) -> tuple[bool, str]:
        await self._ensure_lock_async()
        async with self._send_lock:
            try:
                await self._ensure_browser_async(headless=False, minimize_after_launch=True)
                await self._ensure_gmail_page_async()
                ok, status = await self._wait_gmail_ready_async(timeout_sec=25)
                if not ok:
                    if status == "login_needed":
                        return False, "Gmail logged out — /api/workspace/open call kar to login kar."
                    return False, f"Gmail ready nahi hua: {status}"

                page = self._gmail_page
                # Click Compose
                compose_sels = [
                    'div[role="button"][aria-label*="Compose" i]',
                    'div[gh="cm"]',
                    'div[role="button"][gh="cm"]',
                ]
                compose_btn = None
                for sel in compose_sels:
                    compose_btn = await page.query_selector(sel)
                    if compose_btn:
                        break
                if compose_btn is None:
                    return False, "Compose button nahi mila."
                await compose_btn.click()
                await asyncio.sleep(1.5)

                # Fill To
                to_sels = [
                    'div[role="dialog"] input[aria-label*="To recipients" i]',
                    'textarea[name="to"]',
                    'input[aria-label="To"]',
                    'div[role="dialog"] input[type="email"]',
                ]
                to_el = None
                for sel in to_sels:
                    to_el = await page.query_selector(sel)
                    if to_el:
                        break
                if to_el is None:
                    return False, "To field nahi mila compose dialog mein."
                await to_el.click()
                await asyncio.sleep(0.2)
                # Multi-recipient support — split and add each email separately
                # so Gmail parses each as a discrete chip. Pressing Escape after
                # each (instead of Tab) DISMISSES autocomplete without selecting
                # a wrong suggestion that happens to start with the same letters.
                emails = [e.strip() for e in (to or "").replace(";", ",").split(",") if e.strip()]
                if not emails:
                    return False, "No valid email in 'to' field."
                for i, em in enumerate(emails):
                    await page.keyboard.type(em, delay=10)
                    await asyncio.sleep(0.8)  # let autocomplete settle
                    # Escape dismisses dropdown without selecting; comma makes Gmail commit the chip
                    try:
                        await page.keyboard.press("Escape")
                    except Exception:
                        pass
                    await asyncio.sleep(0.2)
                    if i < len(emails) - 1:
                        # Comma + space → Gmail commits the chip and starts the next
                        await page.keyboard.type(", ", delay=10)
                        await asyncio.sleep(0.3)
                # Tab off the To field so it commits the last chip
                await page.keyboard.press("Tab")
                await asyncio.sleep(0.3)

                # Subject
                subj_sels = [
                    'div[role="dialog"] input[name="subjectbox"]',
                    'input[aria-label="Subject"]',
                ]
                subj_el = None
                for sel in subj_sels:
                    subj_el = await page.query_selector(sel)
                    if subj_el:
                        break
                if subj_el and subject:
                    await subj_el.click()
                    await asyncio.sleep(0.2)
                    await page.keyboard.type(subject, delay=10)

                # Body
                body_sels = [
                    'div[role="dialog"] div[role="textbox"][aria-label*="Message Body" i]',
                    'div[aria-label="Message Body"]',
                    'div[role="textbox"][contenteditable="true"]',
                ]
                body_el = None
                for sel in body_sels:
                    body_el = await page.query_selector(sel)
                    if body_el:
                        break
                if body_el is None:
                    return False, "Body field nahi mila."
                await body_el.click()
                await asyncio.sleep(0.2)
                if body:
                    await page.keyboard.type(body, delay=8)
                    await asyncio.sleep(0.3)

                # Attachment via file_chooser
                if attachment_path:
                    if not os.path.exists(attachment_path):
                        return False, f"Attachment file nahi mili: {attachment_path}"
                    attach_sels = [
                        'div[role="dialog"] div[aria-label*="Attach files" i]',
                        'div[command="Files"]',
                        'div[role="dialog"] div[role="button"][aria-label*="Attach" i]',
                    ]
                    attach_btn = None
                    for sel in attach_sels:
                        attach_btn = await page.query_selector(sel)
                        if attach_btn:
                            break
                    if attach_btn is None:
                        return False, "Attach button nahi mila."
                    try:
                        async with page.expect_file_chooser(timeout=15000) as fc_info:
                            await attach_btn.click()
                        fc = await fc_info.value
                        await fc.set_files(attachment_path)
                    except Exception as e:
                        return False, f"Attach fail: {e}"
                    await asyncio.sleep(4.0)  # upload wait

                # Send — find the actual Send button and click it. Earlier we
                # used Ctrl+Enter which depends on the user's Gmail keyboard
                # shortcut settings (insert newline when shortcuts disabled).
                # Button click is universal.
                send_btn = None
                for sel in (
                    'div[role="dialog"] div[role="button"][data-tooltip*="Send" i]',
                    'div[role="dialog"] div[role="button"][aria-label*="Send" i]',
                    'div[role="dialog"] div[role="button"][data-tooltip*="Ctrl-Enter" i]',
                    'div[role="dialog"] button[aria-label*="Send" i]',
                    'div[role="dialog"] [data-tooltip-delay] [aria-label*="Send" i]',
                ):
                    try:
                        send_btn = await page.query_selector(sel)
                    except Exception:
                        send_btn = None
                    if send_btn:
                        break
                if send_btn:
                    try:
                        await send_btn.click(timeout=4000)
                    except Exception:
                        try:
                            await send_btn.click(force=True, timeout=2000)
                        except Exception:
                            send_btn = None
                if not send_btn:
                    # Last resort — keyboard shortcut (works if user has it enabled)
                    try:
                        if body_el:
                            await body_el.click()
                            await asyncio.sleep(0.2)
                    except Exception:
                        pass
                    await page.keyboard.press("Control+Enter")
                await asyncio.sleep(2.5)

                # Verify — PRIMARY signal: compose dialog closure (universal,
                # locale-independent). Toast text is locale-specific ("Message
                # sent" / "संदेश भेजा गया") so it's only a corroborating signal,
                # not the primary. Poll for up to 10s.
                for _ in range(10):
                    await asyncio.sleep(1.0)
                    still_open = await page.query_selector('div[role="dialog"] input[name="subjectbox"]')
                    if still_open is None:
                        # Dialog gone — send committed (Gmail closes compose only on send success)
                        return True, "Email bhej diya (compose closed)"
                    # Belt-and-braces: also check for any "sent" / "भेज" / "envoyé" markers
                    try:
                        body_text = await page.evaluate("() => document.body.innerText")
                    except Exception:
                        body_text = ""
                    if any(marker in body_text for marker in (
                        "Message sent", "message sent",
                        "संदेश भेजा", "भेजा गया",
                        "Mensaje enviado", "envoyé",
                        "Conversation marked",
                    )):
                        return True, "Email bhej diya (toast detected)"
                return False, "Send confirm nahi hua 10s mein — compose dialog abhi bhi khuli."
            except Exception as e:
                log.warning("gmail_send_failed", error=str(e)[:200])
                return False, f"Gmail send fail: {e}"

    # ==================================================================
    # Trello (trello.com) — shares the same Chromium browser
    # ==================================================================

    async def _ensure_trello_page_async(self):
        await self._ensure_browser_async(headless=False, minimize_after_launch=True)
        try:
            if self._trello_page is not None and not self._trello_page.is_closed():
                return self._trello_page
        except Exception:
            self._trello_page = None
        for p in (self._browser.pages or []):
            try:
                u = (p.url or "").lower()
                if "trello.com" in u:
                    self._trello_page = p
                    return p
            except Exception:
                continue
        self._trello_page = await self._browser.new_page()
        try:
            await self._trello_page.goto("https://trello.com/", timeout=30000, wait_until="domcontentloaded")
        except Exception as e:
            log.debug("trello_goto_partial", error=str(e)[:120])
        try:
            await self._trello_page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        await asyncio.sleep(1.5)
        return self._trello_page

    async def _check_trello_logged_in_async(self) -> tuple[bool, str]:
        page = self._trello_page
        if page is None or page.is_closed():
            return False, "no_page"
        try:
            url = (page.url or "").lower()
            if "login" in url or "id.atlassian.com" in url:
                return False, "login_needed"
            markers = (
                'div[data-testid="header-container"]',
                'a[data-testid="header-boards-menu-button"]',
                'div[data-testid="home-sidebar"]',
                'header[role="banner"]',
                'div[data-testid="home-recent-boards-section"]',
            )
            for sel in markers:
                if await page.query_selector(sel):
                    return True, "ok"
            if "trello.com" in url and "login" not in url:
                body_len = await page.evaluate("() => (document.body && document.body.innerText || '').length")
                if body_len and body_len > 500:
                    return True, "ok"
            return False, "loading"
        except Exception as e:
            err = str(e).lower()
            if "closed" in err or "target closed" in err:
                return False, "window_closed"
            return False, "loading"

    async def _wait_trello_ready_async(self, timeout_sec: int = 60) -> tuple[bool, str]:
        deadline = time.monotonic() + timeout_sec
        last = "loading"
        while time.monotonic() < deadline:
            ok, status = await self._check_trello_logged_in_async()
            if ok:
                return True, "ok"
            if status == "window_closed":
                return False, "window_closed"
            last = status
            await asyncio.sleep(1.5)
        return False, last

    @classmethod
    def trello_session_exists(cls) -> bool:
        """Check whether trello.com / atlassian.com has cookies in the session.
        Same pattern as gmail_session_exists — probe the SQLite cookies DB
        for a trello/atlassian host row.
        """
        if not cls.session_exists():
            return False
        try:
            import sqlite3
            cookies_paths = (
                _SESSION_DIR / "Default" / "Network" / "Cookies",
                _SESSION_DIR / "Default" / "Cookies",
            )
            for p in cookies_paths:
                if not p.exists() or p.stat().st_size == 0:
                    continue
                try:
                    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=2)
                    cur = conn.cursor()
                    cur.execute(
                        "SELECT 1 FROM cookies WHERE "
                        "host_key LIKE '%trello.com' OR host_key LIKE '%atlassian.com' "
                        "LIMIT 1"
                    )
                    has = cur.fetchone() is not None
                    conn.close()
                    if has:
                        return True
                except Exception:
                    continue
            return False
        except Exception:
            return True

    @classmethod
    def teams_session_exists_strict(cls) -> bool:
        """Strict Teams login check — looks for microsoftonline / teams host
        cookies in the session DB. Used instead of the broad `teams_session_exists`
        when we need a true logged-in signal.
        """
        if not cls.session_exists():
            return False
        try:
            import sqlite3
            cookies_paths = (
                _SESSION_DIR / "Default" / "Network" / "Cookies",
                _SESSION_DIR / "Default" / "Cookies",
            )
            for p in cookies_paths:
                if not p.exists() or p.stat().st_size == 0:
                    continue
                try:
                    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=2)
                    cur = conn.cursor()
                    cur.execute(
                        "SELECT 1 FROM cookies WHERE "
                        "host_key LIKE '%teams.microsoft.com' OR "
                        "host_key LIKE '%microsoftonline.com' OR "
                        "host_key LIKE '%teams.live.com' LIMIT 1"
                    )
                    has = cur.fetchone() is not None
                    conn.close()
                    if has:
                        return True
                except Exception:
                    continue
            return False
        except Exception:
            return True

    def is_trello_alive(self) -> bool:
        try:
            if self._trello_page is None or self._trello_page.is_closed():
                return False
            return True
        except Exception:
            return False

    def _diagnose_teams_chat_sync(self, recipient: str = "", timeout: float = 60.0) -> dict:
        """Open Teams, search for recipient, dump search-result rows and the
        compose box structure. Used to fix selector drift without needing
        the user to send real messages.
        """
        return self._submit(self._diagnose_teams_chat_async(recipient), timeout=timeout)

    async def _diagnose_teams_chat_async(self, recipient: str) -> dict:
        result = {"ok": False, "search_results": [], "compose_box": None, "attach_buttons": [], "error": ""}
        try:
            await self._ensure_browser_async(headless=False, minimize_after_launch=True)
            await self._ensure_teams_page_async()
            ok, status = await self._wait_teams_ready_async(timeout_sec=20)
            if not ok:
                result["error"] = f"Teams not ready: {status}"
                return result
            page = self._teams_page
            # Search the recipient if given
            if recipient and recipient.strip():
                for sel in ('input[data-tid="topSearchInput"]', 'input[placeholder*="Search" i]', 'input[aria-label*="Search" i]'):
                    sb = await page.query_selector(sel)
                    if sb:
                        try:
                            await sb.click()
                            await asyncio.sleep(0.3)
                            await page.keyboard.press("Control+A")
                            await page.keyboard.press("Backspace")
                            await page.keyboard.type(recipient.strip(), delay=15)
                            await asyncio.sleep(2.0)
                        except Exception:
                            pass
                        result["search_selector_used"] = sel
                        break

            # Dump search results, compose box, attach buttons
            dump = await page.evaluate(
                """() => {
                    const out = {results: [], compose: null, attach: []};
                    // Search results
                    const resultSelectors = [
                        'div[data-tid="search-result"] [role="listitem"]',
                        'div[role="listbox"] [role="option"]',
                        'div[data-tid*="searchResult"]',
                        'li[role="option"]',
                    ];
                    for (const sel of resultSelectors) {
                        const items = Array.from(document.querySelectorAll(sel));
                        if (items.length > 0) {
                            for (const it of items.slice(0, 10)) {
                                out.results.push({
                                    sel,
                                    text: (it.innerText || '').trim().substring(0, 80),
                                    role: it.getAttribute('role') || '',
                                    dataTid: it.getAttribute('data-tid') || '',
                                });
                            }
                            break;
                        }
                    }
                    // Compose box
                    const composeSelectors = [
                        'div[data-tid="ckeditor"] div[contenteditable="true"]',
                        'div[role="textbox"][contenteditable="true"][aria-label*="message" i]',
                        'div[role="textbox"][contenteditable="true"][aria-label*="Type a" i]',
                        'div[contenteditable="true"][data-tid*="message"]',
                    ];
                    for (const sel of composeSelectors) {
                        const el = document.querySelector(sel);
                        if (el) {
                            out.compose = {
                                sel,
                                al: el.getAttribute('aria-label') || '',
                                dataTid: el.getAttribute('data-tid') || '',
                            };
                            break;
                        }
                    }
                    // Attach buttons (paperclip)
                    const attachSelectors = [
                        'button[data-tid="message-area-attach-file-button"]',
                        'button[aria-label*="Attach" i]',
                        'button[title*="Attach" i]',
                    ];
                    for (const sel of attachSelectors) {
                        const items = Array.from(document.querySelectorAll(sel));
                        for (const el of items.slice(0, 3)) {
                            out.attach.push({
                                sel,
                                al: el.getAttribute('aria-label') || '',
                                title: el.getAttribute('title') || '',
                                dataTid: el.getAttribute('data-tid') || '',
                            });
                        }
                    }
                    return out;
                }"""
            )
            result["search_results"] = (dump or {}).get("results", [])
            result["compose_box"] = (dump or {}).get("compose")
            result["attach_buttons"] = (dump or {}).get("attach", [])
            result["ok"] = True
            return result
        except Exception as e:
            result["error"] = str(e)[:200]
            return result

    def _diagnose_gmail_compose_sync(self, timeout: float = 60.0) -> dict:
        """Open Gmail compose dialog and dump every input/textbox in it.
        Used to debug selector drift without sending real emails.
        """
        return self._submit(self._diagnose_gmail_compose_async(), timeout=timeout)

    async def _diagnose_gmail_compose_async(self) -> dict:
        result = {"ok": False, "fields": [], "error": ""}
        try:
            await self._ensure_browser_async(headless=False, minimize_after_launch=True)
            await self._ensure_gmail_page_async()
            ok, status = await self._wait_gmail_ready_async(timeout_sec=20)
            if not ok:
                result["error"] = f"Gmail not ready: {status}"
                return result
            page = self._gmail_page
            compose_btn = None
            for sel in ('div[role="button"][aria-label*="Compose" i]', 'div[gh="cm"]'):
                compose_btn = await page.query_selector(sel)
                if compose_btn:
                    break
            if compose_btn is None:
                result["error"] = "compose button not found"
                return result
            await compose_btn.click()
            await asyncio.sleep(1.5)
            # Dump all inputs/textareas/contenteditable divs in any visible dialog
            dump = await page.evaluate(
                """() => {
                    const out = [];
                    const dialogs = Array.from(document.querySelectorAll('div[role="dialog"]'));
                    for (const dlg of dialogs) {
                        const fields = Array.from(dlg.querySelectorAll('input, textarea, div[contenteditable="true"]'));
                        for (const f of fields) {
                            out.push({
                                tag: f.tagName,
                                type: f.type || '',
                                name: f.name || '',
                                al: f.getAttribute('aria-label') || '',
                                ph: f.getAttribute('placeholder') || '',
                                role: f.getAttribute('role') || '',
                                cls: (f.className || '').toString().substring(0, 80),
                            });
                        }
                    }
                    return out;
                }"""
            )
            result["fields"] = dump or []
            result["count"] = len(dump or [])
            result["ok"] = True
            try:
                await page.keyboard.press("Escape")
                await asyncio.sleep(0.2)
                await page.keyboard.press("Escape")
            except Exception:
                pass
            return result
        except Exception as e:
            result["error"] = str(e)[:200]
            return result

    def _diagnose_trello_board_sync(self, board_name: str, timeout: float = 60.0) -> dict:
        return self._submit(self._diagnose_trello_board_async(board_name), timeout=timeout)

    async def _diagnose_trello_board_async(self, board_name: str) -> dict:
        result = {"ok": False, "lists": [], "boards_visible": [], "error": ""}
        try:
            await self._ensure_browser_async(headless=False, minimize_after_launch=True)
            await self._ensure_trello_page_async()
            ok, status = await self._wait_trello_ready_async(timeout_sec=20)
            if not ok:
                result["error"] = f"Trello not ready: {status}"
                return result
            page = self._trello_page
            if board_name:
                clicked = await page.evaluate(
                    """(target) => {
                        const t = (target || '').trim().toLowerCase();
                        const links = Array.from(document.querySelectorAll('a, button, [role="menuitem"]'));
                        for (const el of links) {
                            const txt = (el.innerText || '').trim().toLowerCase();
                            if (txt.includes(t)) { el.click(); return true; }
                        }
                        return false;
                    }""",
                    board_name,
                )
                if clicked:
                    await asyncio.sleep(2.5)
            dump = await page.evaluate(
                """() => {
                    const out = {boards: [], lists: []};
                    // Visible boards (on home or boards menu)
                    const boardLinks = Array.from(document.querySelectorAll('[data-testid*="board" i], a[href*="/b/"]'));
                    for (const b of boardLinks.slice(0, 20)) {
                        const t = (b.innerText || '').trim().substring(0, 80);
                        if (t) out.boards.push(t);
                    }
                    // Lists on currently-open board
                    const lists = Array.from(document.querySelectorAll('[data-testid="list"], li[data-list-id]'));
                    for (const l of lists.slice(0, 20)) {
                        const hdr = l.querySelector('[data-testid="list-header"], h2');
                        const name = hdr ? (hdr.innerText || '').trim() : (l.innerText || '').trim().substring(0, 50);
                        const addBtn = !!l.querySelector('[data-testid="list-add-card-button"]');
                        out.lists.push({name, hasAddButton: addBtn});
                    }
                    return out;
                }"""
            )
            result["boards_visible"] = (dump or {}).get("boards", [])
            result["lists"] = (dump or {}).get("lists", [])
            result["ok"] = True
            return result
        except Exception as e:
            result["error"] = str(e)[:200]
            return result

    def trello_add_card_sync(
        self,
        board_name: str,
        list_name: str,
        card_title: str,
        timeout_sec: int = 90,
    ) -> tuple[bool, str]:
        return self._submit(
            self._trello_add_card_async(board_name, list_name, card_title),
            timeout=timeout_sec,
        )

    async def _trello_add_card_async(self, board_name: str, list_name: str, card_title: str) -> tuple[bool, str]:
        await self._ensure_lock_async()
        async with self._send_lock:
            try:
                await self._ensure_browser_async(headless=False, minimize_after_launch=True)
                await self._ensure_trello_page_async()
                ok, status = await self._wait_trello_ready_async(timeout_sec=25)
                if not ok:
                    if status == "login_needed":
                        return False, "Trello logged out — /api/workspace/open call kar to login kar."
                    return False, f"Trello ready nahi hua: {status}"

                page = self._trello_page
                # Find the board by name — open boards menu, click matching board
                # Trello has a global boards switcher in the header
                boards_btn_sels = [
                    'a[data-testid="header-boards-menu-button"]',
                    'button[data-testid="header-boards-menu-button"]',
                    'a[aria-label*="Boards" i]',
                ]
                boards_btn = None
                for sel in boards_btn_sels:
                    boards_btn = await page.query_selector(sel)
                    if boards_btn:
                        break
                if boards_btn:
                    await boards_btn.click()
                    await asyncio.sleep(1.0)

                # Click board by visible name
                clicked = await page.evaluate(
                    """(target) => {
                        const t = (target || '').trim().toLowerCase();
                        const links = Array.from(document.querySelectorAll('a, button, [role="menuitem"], [role="link"]'));
                        for (const el of links) {
                            const txt = (el.innerText || '').trim().toLowerCase();
                            if (txt && (txt === t || txt.startsWith(t) || txt.includes(t))) {
                                el.click();
                                return true;
                            }
                        }
                        return false;
                    }""",
                    board_name,
                )
                if not clicked:
                    return False, f"Board '{board_name}' nahi mila."
                await asyncio.sleep(3.0)

                # Find the list and click "+ Add a card" inside it
                added = await page.evaluate(
                    """({listName, cardTitle}) => {
                        const ln = (listName || '').trim().toLowerCase();
                        const lists = Array.from(document.querySelectorAll('[data-testid="list"], li[data-list-id]'));
                        for (const list of lists) {
                            const header = list.querySelector('[data-testid="list-header"], h2, .list-header-name');
                            const text = (header ? header.innerText : list.innerText || '').trim().toLowerCase();
                            if (text.includes(ln)) {
                                const addBtn = list.querySelector('[data-testid="list-add-card-button"], button[aria-label*="Add" i]');
                                if (addBtn) {
                                    addBtn.click();
                                    return {ok: true, listFound: true};
                                }
                                return {ok: false, listFound: true};
                            }
                        }
                        return {ok: false, listFound: false};
                    }""",
                    {"listName": list_name, "cardTitle": card_title},
                )
                if not added.get("listFound"):
                    return False, f"List '{list_name}' board mein nahi mili."
                if not added.get("ok"):
                    return False, "Add Card button nahi mila is list mein."

                await asyncio.sleep(0.8)
                # Type the card title
                await page.keyboard.type(card_title, delay=15)
                await asyncio.sleep(0.5)
                # Submit — prefer the "Add card" button (Trello sometimes
                # treats Enter as newline). Press Enter only as fallback.
                add_clicked = False
                for sel in (
                    'button[data-testid="list-card-composer-add-card-button"]',
                    'button[data-testid="card-composer-add-card-button"]',
                    'button[type="submit"][aria-label*="Add card" i]',
                    'button:has-text("Add card")',
                ):
                    try:
                        btn = await page.query_selector(sel)
                    except Exception:
                        btn = None
                    if btn:
                        try:
                            await btn.click(timeout=2500)
                            add_clicked = True
                            break
                        except Exception:
                            continue
                if not add_clicked:
                    await page.keyboard.press("Enter")
                await asyncio.sleep(1.8)
                try:
                    await page.keyboard.press("Escape")
                except Exception:
                    pass
                body = await page.evaluate("() => document.body.innerText")
                if card_title.lower() in (body or "").lower():
                    return True, "Trello card bana diya (verified)"
                return False, "Card create confirm nahi hua."
            except Exception as e:
                log.warning("trello_add_card_failed", error=str(e)[:200])
                return False, f"Trello card fail: {e}"

    # ==================================================================
    # Unified workspace — open WA + Teams (+future Gmail/Trello) in ONE
    # Chromium so the user does all logins in a single window.
    # ==================================================================

    def open_workspace_sync(self, timeout_sec: int = 180) -> dict:
        """Open the shared Chromium with WhatsApp + Teams tabs side by side.
        User scans WA QR + signs into Microsoft account, all in one browser.
        Returns a status dict per service.
        """
        return self._submit(self._open_workspace_async(timeout_sec), timeout=timeout_sec + 30)

    async def _open_workspace_async(self, timeout_sec: int) -> dict:
        result = {
            "ok": False,
            "whatsapp": {"ready": False, "needs_action": False, "message": ""},
            "teams":    {"ready": False, "needs_action": False, "message": ""},
            "gmail":    {"ready": False, "needs_action": False, "message": ""},
            "trello":   {"ready": False, "needs_action": False, "message": ""},
            "message": "",
        }
        try:
            # Browser VISIBLE (non-minimized) so user can do logins in real time
            await self._ensure_browser_async(headless=False, minimize_after_launch=False)

            # Open all 4 service tabs in the SAME persistent Chromium
            await self._open_whatsapp_async(timeout_ms=25000)
            await self._ensure_teams_page_async()
            await self._ensure_gmail_page_async()
            await self._ensure_trello_page_async()

            # Round-robin poll for each service's login state
            deadline = time.monotonic() + timeout_sec
            wa_ready = teams_ready = gmail_ready = trello_ready = False
            wa_status = teams_status = gmail_status = trello_status = "loading"
            last_log_t = 0.0
            while time.monotonic() < deadline:
                if not wa_ready:
                    ok, st = await self._check_logged_in_async()
                    wa_ready, wa_status = ok, st
                if not teams_ready:
                    ok, st = await self._check_teams_logged_in_async()
                    teams_ready, teams_status = ok, st
                if not gmail_ready:
                    ok, st = await self._check_gmail_logged_in_async()
                    gmail_ready, gmail_status = ok, st
                if not trello_ready:
                    ok, st = await self._check_trello_logged_in_async()
                    trello_ready, trello_status = ok, st
                if wa_ready and teams_ready and gmail_ready and trello_ready:
                    break
                now = time.monotonic()
                if now - last_log_t > 10:
                    log.info(
                        "workspace_login_progress",
                        wa=wa_status, teams=teams_status, gmail=gmail_status, trello=trello_status,
                    )
                    last_log_t = now
                # Window closed on any tab → bail
                if "window_closed" in (wa_status, teams_status, gmail_status, trello_status):
                    result["message"] = "Tumne Chromium window band kar di. Dobara button click kar."
                    return result
                await asyncio.sleep(1.5)

            # Populate per-service result
            def _entry(ready, status, action_status):
                return {
                    "ready": ready,
                    "needs_action": (status == action_status),
                    "message": "Connected" if ready else (
                        "Sign-in pending" if status == action_status else "Loading"
                    ),
                }
            result["whatsapp"] = _entry(wa_ready, wa_status, "QR_NEEDED")
            result["teams"]    = _entry(teams_ready, teams_status, "login_needed")
            result["gmail"]    = _entry(gmail_ready, gmail_status, "login_needed")
            result["trello"]   = _entry(trello_ready, trello_status, "login_needed")

            anything = wa_ready or teams_ready or gmail_ready or trello_ready
            if anything:
                await asyncio.sleep(5)
                await self._minimize_window_async()
                result["ok"] = True
                ready_names = [n for n, r in [("WhatsApp", wa_ready), ("Teams", teams_ready),
                                              ("Gmail", gmail_ready), ("Trello", trello_ready)] if r]
                pending_names = [n for n, r in [("WhatsApp", wa_ready), ("Teams", teams_ready),
                                                ("Gmail", gmail_ready), ("Trello", trello_ready)] if not r]
                msg = "Connected: " + ", ".join(ready_names) if ready_names else ""
                if pending_names:
                    msg += " · Pending: " + ", ".join(pending_names)
                result["message"] = msg or "Background mein chal raha"
                return result

            result["message"] = "Koi service login nahi hui time mein. Dobara button click kar."
            return result
        except Exception as e:
            log.warning("workspace_open_failed", error=str(e)[:200])
            result["message"] = f"Workspace open fail: {e}"
            return result

    async def _teams_first_time_login_async(self, timeout_sec: int) -> tuple[bool, str]:
        try:
            # Make sure the browser is visible (minimize off) for login
            await self._ensure_browser_async(headless=False, minimize_after_launch=False)
            await self._ensure_teams_page_async()
            # Bring the Teams tab to front so user sees the login screen
            try:
                await self._teams_page.bring_to_front()
            except Exception:
                pass
            ok, status = await self._wait_teams_ready_async(timeout_sec=timeout_sec)
            if not ok:
                if status == "window_closed":
                    return False, "Tumne Chromium window band kar di. Dobara button click kar."
                if status == "login_needed":
                    return False, "Login complete nahi hua time mein. Dobara button click kar."
                return False, "Teams ready nahi hua time mein."
            # Login good — minimize the window again
            await asyncio.sleep(3)
            await self._minimize_window_async()
            return True, "Teams login saved. Background mein silent chalna shuru."
        except Exception as e:
            log.warning("teams_first_time_login_failed", error=str(e)[:200])
            return False, f"Teams login fail: {e}"

    def is_teams_alive(self) -> bool:
        """Is the Teams page open and not closed?"""
        try:
            if self._teams_page is None or self._teams_page.is_closed():
                return False
            return True
        except Exception:
            return False

    @classmethod
    def teams_session_exists(cls) -> bool:
        """Strict Teams login check — looks for microsoftonline/teams host
        cookies in the persistent session DB. Returning the WA-style "any
        cookie file exists" was too generous and made the dashboard show
        Teams as connected before user had logged in.
        """
        return cls.teams_session_exists_strict() if hasattr(cls, 'teams_session_exists_strict') else cls.session_exists()

    def teams_send_message_sync(
        self,
        recipient: str = "",
        message: str = "",
        attachment_path: str = "",
        timeout_sec: int = 120,
    ) -> tuple[bool, str]:
        """Send a Teams 1:1 chat message. Same patterns as WA — open chat
        by recipient name, type the message, click send. Caller can be on
        any thread.
        """
        if not (message and message.strip()) and not attachment_path:
            return False, "Message text ya attachment dena hoga"
        return self._submit(
            self._teams_send_message_async(
                recipient=recipient,
                message=message,
                attachment_path=attachment_path,
            ),
            timeout=timeout_sec,
        )

    async def _teams_send_message_async(
        self,
        recipient: str,
        message: str,
        attachment_path: str,
    ) -> tuple[bool, str]:
        await self._ensure_lock_async()
        async with self._send_lock:
            try:
                await self._ensure_browser_async(headless=False, minimize_after_launch=True)
                await self._ensure_teams_page_async()
                ok, status = await self._wait_teams_ready_async(timeout_sec=30)
                if not ok:
                    if status == "login_needed":
                        return False, "Teams logged out — /api/teams/setup call kar to login kar (one-time)."
                    return False, f"Teams ready nahi hua: {status}"

                # Open chat by recipient name via Teams search
                try:
                    await self._teams_open_chat_by_name_async(recipient.strip())
                except Exception as e:
                    return False, f"Chat open nahi hua: {e}"

                # Send
                if attachment_path:
                    return await self._teams_send_attachment_async(attachment_path, message)
                return await self._teams_send_text_async(message)
            except Exception as e:
                log.warning("teams_send_failed", error=str(e)[:200])
                return False, f"Teams send fail: {e}"

    async def _teams_open_chat_by_name_async(self, name: str) -> None:
        """Open Teams 1:1 chat with `name`. Uses Teams' top search bar.
        Pattern: search input → type name → click first chat result.
        """
        page = self._teams_page
        name_clean = (name or "").strip()
        if not name_clean:
            raise RuntimeError("Recipient name empty")

        # Teams' top search/command bar selectors (multiple to handle versions)
        search_selectors = [
            'input[data-tid="topSearchInput"]',
            'input[placeholder*="Search" i]',
            'input[aria-label*="Search" i]',
            'div[role="search"] input',
            'div[data-tid="search-box"] input',
        ]
        search_el = None
        for sel in search_selectors:
            search_el = await page.query_selector(sel)
            if search_el:
                break
        if search_el is None:
            raise RuntimeError("Teams search bar nahi mila.")

        try:
            await search_el.click()
        except Exception:
            pass
        await asyncio.sleep(0.4)
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Backspace")
        await asyncio.sleep(0.2)
        await page.keyboard.type(name_clean, delay=20)
        await asyncio.sleep(2.0)

        # Locate matching result row — pure DATA QUERY (no click). Then
        # we click via Playwright's element handle so React event handlers
        # fire correctly. In-page .click() is a known bad pattern for
        # React SPAs (we hit this on WhatsApp earlier).
        match = await page.evaluate(
            """(target) => {
                const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                const t = norm(target);
                const sels = [
                    'div[data-tid="search-result"] [role="listitem"]',
                    'div[role="listbox"] [role="option"]',
                    'div[data-tid="search-tab-result"]',
                    '[data-tid*="searchResult"]',
                    'li[role="option"]',
                    'div[role="grid"] [role="row"]',
                ];
                for (const sel of sels) {
                    const items = Array.from(document.querySelectorAll(sel));
                    for (let i = 0; i < items.length; i++) {
                        const txt = norm(items[i].innerText || '');
                        if (txt && (txt === t || txt.startsWith(t) || txt.includes(t))) {
                            return {sel, idx: i, text: items[i].innerText.substring(0, 60)};
                        }
                    }
                }
                return null;
            }""",
            name_clean,
        )

        if match:
            log.info("teams_search_match", searched=name_clean, matched=match.get("text", "")[:60], sel=match.get("sel", ""))
            rows = await page.query_selector_all(match["sel"])
            if match["idx"] < len(rows):
                target_row = rows[match["idx"]]
                try:
                    await target_row.scroll_into_view_if_needed(timeout=2000)
                except Exception:
                    pass
                try:
                    await target_row.click(timeout=4000)
                except Exception:
                    try:
                        await target_row.click(force=True, timeout=2000)
                    except Exception as e:
                        raise RuntimeError(f"Teams result row click fail: {e}")
        else:
            # No fallback Enter-press — in Teams, Enter in search navigates
            # to a global search-results page, NOT opening a 1:1 chat.
            # Better to fail loudly than send to the wrong target.
            raise RuntimeError(f"'{name_clean}' Teams contacts mein nahi mila.")

        await asyncio.sleep(2.5)
        # Handle possible "Start new chat with X?" confirmation dialog
        for confirm_sel in (
            'button[data-tid="new-chat-dialog-create-button"]',
            'button[aria-label*="Start chat" i]',
            'button:has-text("Create")',
            'button:has-text("Start")',
        ):
            try:
                btn = await page.query_selector(confirm_sel)
                if btn:
                    log.info("teams_confirm_dialog_clicked", selector=confirm_sel)
                    await btn.click(timeout=2000)
                    await asyncio.sleep(1.2)
                    break
            except Exception:
                continue

        # Verify by looking for the message compose box
        compose = await self._teams_find_compose_async(timeout=12)
        if compose is None:
            raise RuntimeError(f"'{name_clean}' chat compose box nahi mila.")

    async def _teams_find_compose_async(self, timeout: float = 10.0):
        page = self._teams_page
        sels = [
            'div[data-tid="ckeditor"] div[contenteditable="true"]',
            'div[role="textbox"][contenteditable="true"][aria-label*="message" i]',
            'div[role="textbox"][contenteditable="true"][aria-label*="Type a" i]',
            'div[contenteditable="true"][data-tid*="message"]',
            'div[contenteditable="true"][aria-label*="Type a new message" i]',
            'div[contenteditable="true"][role="textbox"]',
        ]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for sel in sels:
                el = await page.query_selector(sel)
                if el:
                    return el
            await asyncio.sleep(0.3)
        return None

    async def _teams_send_text_async(self, message: str) -> tuple[bool, str]:
        page = self._teams_page
        compose = await self._teams_find_compose_async()
        if compose is None:
            return False, "Compose box nahi mila."
        await compose.click()
        await asyncio.sleep(0.3)
        await page.keyboard.type(message, delay=10)
        await asyncio.sleep(0.4)
        # Click the explicit Send button — Enter inserts NEWLINE when the
        # user has "Press Enter to send" disabled (common default in Teams).
        # We try several selectors and only fall back to Enter as a last
        # resort.
        send_clicked = False
        for sel in (
            'button[data-tid="newMessageCommands-send"]',
            'button[aria-label*="Send" i]',
            'button[title*="Send" i]',
            'div[role="button"][aria-label*="Send" i]',
        ):
            try:
                btn = await page.query_selector(sel)
            except Exception:
                btn = None
            if btn:
                try:
                    await btn.click(timeout=3000)
                    send_clicked = True
                    break
                except Exception:
                    try:
                        await btn.click(force=True, timeout=2000)
                        send_clicked = True
                        break
                    except Exception:
                        continue
        if not send_clicked:
            # Last-resort fallback. Many users have Enter=send enabled.
            await page.keyboard.press("Enter")
        # Poll for verification — Teams renders messages asynchronously,
        # single 1.5s wait was false-failing legitimate sends. 12 poll
        # rounds of 1.5s = 18s budget (same as WA attachment verify).
        msg_strip = (message or "").strip()
        if not msg_strip:
            return False, "Empty message — kuch nahi bheja."
        for _ in range(12):
            await asyncio.sleep(1.5)
            try:
                body = await page.evaluate("() => document.body.innerText")
            except Exception:
                body = ""
            if msg_strip in (body or "")[-5000:]:
                return True, "Bhej diya (verified)"
        return False, "Send confirm nahi hua — message chat tail mein 18s mein nahi dikha."

    async def _teams_send_attachment_async(self, file_path: str, caption: str) -> tuple[bool, str]:
        """Upload + send a file in Teams. Teams uses an attach button
        (paperclip) that triggers a file picker — same expect_file_chooser
        intercept pattern as WA.
        """
        page = self._teams_page
        if not os.path.exists(file_path):
            return False, f"File path nahi mila: {file_path}"

        # Find and click the attach button
        attach_sels = [
            'button[data-tid="message-area-attach-file-button"]',
            'button[aria-label*="Attach" i]',
            'button[title*="Attach" i]',
            'div[role="button"][aria-label*="Attach" i]',
        ]
        attach_btn = None
        for sel in attach_sels:
            attach_btn = await page.query_selector(sel)
            if attach_btn:
                break
        if attach_btn is None:
            return False, "Teams attach button nahi mila."

        # Intercept file chooser
        try:
            async with page.expect_file_chooser(timeout=15000) as fc_info:
                await attach_btn.click()
                await asyncio.sleep(0.5)
                # Some Teams variants show a sub-menu — click "Upload from this device" / "Upload from computer"
                upload_sels = [
                    'button[data-tid*="UploadComputer" i]',
                    'div[role="menuitem"]:has-text("Upload from this device")',
                    'div[role="menuitem"]:has-text("Upload from computer")',
                ]
                for sel in upload_sels:
                    try:
                        sub = await page.query_selector(sel)
                        if sub:
                            await sub.click()
                            break
                    except Exception:
                        continue
            fc = await fc_info.value
            await fc.set_files(file_path)
        except Exception as e:
            return False, f"Teams attach fail: {e}"

        await asyncio.sleep(4.0)  # upload time

        # Add caption if given
        if caption and caption.strip():
            compose = await self._teams_find_compose_async()
            if compose:
                try:
                    await compose.click()
                    await asyncio.sleep(0.2)
                    await page.keyboard.type(caption, delay=10)
                    await asyncio.sleep(0.3)
                except Exception:
                    pass

        # Click send
        send_sels = [
            'button[data-tid="newMessageCommands-send"]',
            'button[aria-label*="Send" i]',
            'button[title*="Send" i]',
        ]
        send_clicked = False
        for sel in send_sels:
            btn = await page.query_selector(sel)
            if btn:
                try:
                    await btn.click()
                    send_clicked = True
                    break
                except Exception:
                    continue
        if not send_clicked:
            return False, "Teams send button nahi mila."

        await asyncio.sleep(3.0)
        # Verify — filename in chat tail
        try:
            body = await page.evaluate("() => document.body.innerText")
        except Exception:
            body = ""
        fname = os.path.basename(file_path).lower()
        tail = (body or "")[-5000:].lower()
        if fname and fname in tail:
            return True, "File bhej diya (verified)"
        if caption and caption.strip() and caption.strip().lower() in tail:
            return True, "File bhej diya (verified — caption mila)"
        return True, "File send hua (preview close ho gaya, verify timeout)"

    def diagnose_attach_inputs_sync(self, recipient: str = "", timeout: float = 60.0) -> dict:
        """One-shot diagnostic: open a chat, click the attach button, dump
        ALL `input[type=file]` elements with their full ancestry, then dismiss
        the menu without sending anything.

        Used to debug why image PNGs route to sticker / why PDFs don't go to
        document. Returns the raw DOM data so we can fix the routing without
        guessing.
        """
        return self._submit(self._diagnose_attach_inputs_async(recipient), timeout=timeout)

    async def _diagnose_attach_inputs_async(self, recipient: str) -> dict:
        result = {"ok": False, "inputs": [], "error": ""}
        try:
            await self._ensure_browser_async(headless=False, minimize_after_launch=True)
            await self._open_whatsapp_async()
            ok, status = await self._wait_for_ready_async(timeout_sec=20)
            if not ok:
                result["error"] = f"WA not ready: {status}"
                return result

            # Open a chat — needed because attach button only exists when
            # a chat is active. If no recipient given, use the last opened.
            if recipient and recipient.strip():
                try:
                    await self._open_chat_by_name_async(recipient.strip())
                except Exception as e:
                    result["error"] = f"chat open failed: {e}"
                    return result

            page = self._page
            # Click attach
            clicked = False
            for sel in (
                '[data-icon="plus-rounded"]', '[data-icon="plus"]',
                '[data-icon="attach-menu-plus"]', '[data-icon="clip"]',
                '[data-icon="attach"]', '[data-icon="paperclip"]',
                'button[title*="Attach" i]', 'button[aria-label*="Attach" i]',
                'div[role="button"][aria-label*="Attach" i]',
            ):
                btn = await page.query_selector(sel)
                if btn:
                    try:
                        await btn.click()
                        clicked = True
                        result["attach_selector_used"] = sel
                        break
                    except Exception:
                        continue
            if not clicked:
                result["error"] = "attach button not found"
                return result

            # Wait for menu to settle — longer for new WA Web (sometimes lazy)
            await asyncio.sleep(2.5)

            # Dump: (a) all inputs, (b) any menu/listbox/popup with their items
            dump = await page.evaluate(
                """() => {
                    const out = {inputs: [], menu_items: [], popups: []};
                    // ---- INPUTS ----
                    const inputs = Array.from(document.querySelectorAll('input[type="file"]'));
                    out.inputs = inputs.map((el, idx) => {
                        const o = {
                            idx,
                            accept: el.getAttribute('accept') || '',
                            name: el.getAttribute('name') || '',
                            multiple: el.multiple || false,
                        };
                        let n = el.parentElement;
                        const trail = [];
                        for (let i = 0; i < 8 && n && n !== document.body; i++, n = n.parentElement) {
                            const piece = {};
                            if (n.tagName) piece.tag = n.tagName;
                            if (n.getAttribute) {
                                const al = n.getAttribute('aria-label'); if (al) piece.al = al;
                                const di = n.getAttribute('data-icon'); if (di) piece.di = di;
                                const dtt = n.getAttribute('data-testid'); if (dtt) piece.dtt = dtt;
                            }
                            const innerIcon = n.querySelector && n.querySelector('[data-icon]');
                            if (innerIcon) {
                                const di = innerIcon.getAttribute('data-icon');
                                if (di && !piece.di) piece.iconChild = di;
                            }
                            const txt = (n.innerText || '').trim();
                            if (txt && txt.length < 60) piece.text = txt;
                            trail.push(piece);
                        }
                        o.trail = trail;
                        return o;
                    });

                    // ---- MENU / POPUP ITEMS — search the whole document for
                    // anything resembling an attach menu item ----
                    const candidates = Array.from(document.querySelectorAll(
                        '[role="menuitem"], [role="button"], li[role="option"], div[role="menu"] > *, div[role="listbox"] > *, span[data-icon]'
                    ));
                    const seen = new Set();
                    for (const el of candidates) {
                        if (seen.has(el)) continue;
                        seen.add(el);
                        const di = el.getAttribute && el.getAttribute('data-icon');
                        const al = el.getAttribute && el.getAttribute('aria-label');
                        const role = el.getAttribute && el.getAttribute('role');
                        const txt = (el.innerText || '').trim().substring(0, 50);
                        if (!di && !al && !txt) continue;
                        // Look for input inside
                        const innerInput = el.querySelector && el.querySelector('input[type="file"]');
                        const innerAccept = innerInput ? innerInput.getAttribute('accept') : null;
                        out.menu_items.push({
                            di: di || '',
                            al: al || '',
                            role: role || '',
                            text: txt,
                            hasInput: !!innerInput,
                            innerAccept: innerAccept || '',
                        });
                    }
                    // Limit menu_items to ones with relevant signals (icon/aria/text)
                    out.menu_items = out.menu_items.filter(m =>
                        (m.di && /attach|document|image|photo|sticker|camera|audio|poll|contact/i.test(m.di)) ||
                        (m.al && /photo|document|sticker|camera|audio|poll|contact|image|video/i.test(m.al)) ||
                        (m.text && /photo|document|sticker|camera|audio|poll|contact|image|video/i.test(m.text)) ||
                        m.hasInput
                    );
                    return out;
                }"""
            )
            result["dump"] = dump
            result["count"] = len((dump or {}).get("inputs", []))
            result["menu_count"] = len((dump or {}).get("menu_items", []))
            result["ok"] = True

            # Dismiss the menu — Escape
            try:
                await page.keyboard.press("Escape")
                await asyncio.sleep(0.2)
                await page.keyboard.press("Escape")
            except Exception:
                pass
            return result
        except Exception as e:
            result["error"] = str(e)[:200]
            try:
                if self._page is not None and not self._page.is_closed():
                    await self._page.keyboard.press("Escape")
            except Exception:
                pass
            return result
