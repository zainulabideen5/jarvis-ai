"""Universal browser controller — Hybrid Option D.

WhatsApp / Teams / Gmail / Trello ke alava ANY website pe automation.
3-tier strategy: DOM heuristics → ARIA tree → learned macros (recorded by user).
NO LLM, NO per-service code, NO ongoing maintenance per site.

Shares the SAME persistent Chromium as WhatsAppPlaywright — opens new tabs
in the existing browser context. WhatsApp/Teams/Gmail/Trello code is NOT
touched; this is a parallel additive feature.

Architecture:
    UniversalBrowser.get()           singleton
        .open_url(url)               open URL in shared browser
        .send_message_sync(...)      universal send (Tier 1 → 2 → 3 fallback)
        .teach_start(url)            begin recording mode on a tab
        .teach_stop(site_key)        save captured macro
        .list_learned_sites()        diagnostic: what sites are known

Learned macros persist at `server/data/learned_sites/<host>.json`.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.core.logging import get_logger

log = get_logger(__name__)

_MACROS_DIR = Path(__file__).resolve().parents[3] / "data" / "learned_sites"


def _site_key_for(url: str) -> str:
    """Normalize a URL to a site key — eTLD+host, lowercase, file-safe."""
    try:
        host = urlparse(url).netloc or url
    except Exception:
        host = url
    host = (host or "").lower().split("/")[0]
    # Strip www. for grouping but keep subdomain (slack.com vs my.slack.com differ)
    if host.startswith("www."):
        host = host[4:]
    # File-safe
    return re.sub(r"[^a-z0-9._-]", "_", host) or "unknown"


def _macro_path(site_key: str) -> Path:
    return _MACROS_DIR / f"{site_key}.json"


class UniversalBrowser:
    """Singleton — opens arbitrary URLs in the SHARED Chromium owned by
    WhatsAppPlaywright. All Playwright work runs on that singleton's
    dedicated background thread (same _submit mechanism).
    """

    _instance: "UniversalBrowser | None" = None

    def __init__(self):
        # Per-tab refs by site_key (so we can revisit existing tabs)
        self._tabs: dict[str, Any] = {}
        # In-memory cache of learned macros (loaded from disk lazily)
        self._macros: dict[str, dict] = {}
        # Recording state — per-site key, populated during teach_start
        self._recording_active: dict[str, dict] = {}

    @classmethod
    def get(cls) -> "UniversalBrowser":
        if cls._instance is None:
            cls._instance = UniversalBrowser()
        return cls._instance

    # ----- Underlying browser access (shared with WhatsAppPlaywright) -----

    def _wa(self):
        """Get the WhatsAppPlaywright singleton. Lazy import so this module
        can be loaded without forcing the WA module to initialize.
        """
        from app.services.laptop_control.wa_playwright import WhatsAppPlaywright
        return WhatsAppPlaywright.get()

    def _submit(self, coro, timeout: float = 90.0):
        """Submit a coroutine to the shared background loop."""
        return self._wa()._submit(coro, timeout=timeout)

    # ==================================================================
    # Sync API — call from anywhere
    # ==================================================================

    def open_url_sync(self, url: str, timeout_sec: float = 30.0) -> dict:
        """Open the given URL in the shared Chromium. Returns site_key + page state.
        Visible window (so the user can interact if needed).
        """
        return self._submit(self._open_url_async(url), timeout=timeout_sec + 10)

    def send_message_sync(
        self,
        url: str,
        recipient: str = "",
        message: str = "",
        attachment_path: str = "",
        timeout_sec: float = 90.0,
    ) -> dict:
        """Universal "send a message" on any site.

        Strategy (in order):
          1. Cached macro for this site (if available)
          2. DOM heuristics
          3. ARIA accessibility tree
          4. (Fail) — return error + suggest teach mode
        """
        return self._submit(
            self._send_message_async(url, recipient, message, attachment_path),
            timeout=timeout_sec,
        )

    def teach_start_sync(self, url: str, timeout_sec: float = 30.0) -> dict:
        """Begin recording user actions on the given URL. Returns the site_key
        and a recording_id the caller uses to stop + save.
        """
        return self._submit(self._teach_start_async(url), timeout=timeout_sec)

    def teach_stop_sync(self, site_key: str, label: str = "send", timeout_sec: float = 15.0) -> dict:
        """Stop recording, save captured macro as `<site_key>.json`."""
        return self._submit(self._teach_stop_async(site_key, label), timeout=timeout_sec)

    def list_learned_sites_sync(self) -> list[dict]:
        """List all learned sites + their macros (for the dashboard)."""
        _MACROS_DIR.mkdir(parents=True, exist_ok=True)
        out = []
        for f in sorted(_MACROS_DIR.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                out.append({
                    "site_key": f.stem,
                    "labels": list((data or {}).get("macros", {}).keys()),
                    "learned_at": data.get("learned_at", ""),
                })
            except Exception:
                continue
        return out

    # ==================================================================
    # Async internals
    # ==================================================================

    async def _ensure_tab_async(self, url: str):
        """Get or create a tab for the URL. Tab refs stored by site_key.
        If existing tab exists but page is closed, re-open."""
        wa = self._wa()
        await wa._ensure_browser_async(headless=False, minimize_after_launch=True)
        sk = _site_key_for(url)
        # Check existing
        existing = self._tabs.get(sk)
        try:
            if existing is not None and not existing.is_closed():
                return existing, sk
        except Exception:
            pass
        # Look for an open tab with matching host
        for p in (wa._browser.pages or []):
            try:
                u = (p.url or "").lower()
                if _site_key_for(u) == sk:
                    self._tabs[sk] = p
                    return p, sk
            except Exception:
                continue
        # Create new tab
        page = await wa._browser.new_page()
        try:
            await page.goto(url, timeout=30000, wait_until="domcontentloaded")
        except Exception as e:
            log.debug("universal_goto_partial", url=url[:120], error=str(e)[:120])
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        await asyncio.sleep(1.0)
        self._tabs[sk] = page
        return page, sk

    async def _open_url_async(self, url: str) -> dict:
        try:
            page, sk = await self._ensure_tab_async(url)
            return {
                "ok": True,
                "site_key": sk,
                "url": page.url,
                "title": (await page.title()) or "",
            }
        except Exception as e:
            log.warning("universal_open_url_failed", url=url[:120], error=str(e)[:200])
            return {"ok": False, "error": str(e)[:200]}

    def _load_macro(self, site_key: str) -> dict | None:
        if site_key in self._macros:
            return self._macros[site_key]
        p = _macro_path(site_key)
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            self._macros[site_key] = data
            return data
        except Exception as e:
            log.debug("macro_load_failed", site_key=site_key, error=str(e)[:80])
            return None

    def _save_macro(self, site_key: str, data: dict) -> None:
        _MACROS_DIR.mkdir(parents=True, exist_ok=True)
        p = _macro_path(site_key)
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        self._macros[site_key] = data

    async def _send_message_async(
        self,
        url: str,
        recipient: str,
        message: str,
        attachment_path: str,
    ) -> dict:
        """Universal send — Tier 1 (macro) → Tier 2 (heuristics) → Tier 3 (ARIA) → fail.

        Returns dict: {ok, tier, message, error}
        """
        try:
            page, sk = await self._ensure_tab_async(url)
        except Exception as e:
            return {"ok": False, "tier": "open", "error": f"Tab open fail: {e}"}

        # Try to bring the tab to front so user sees something happening
        try:
            await page.bring_to_front()
        except Exception:
            pass

        # ----- Tier 1: cached macro -----
        macro = self._load_macro(sk)
        if macro:
            try:
                ok = await self._replay_macro_async(page, macro, recipient, message, attachment_path)
                if ok:
                    return {"ok": True, "tier": "macro", "message": "Bhej diya (cached macro)"}
                log.info("universal_macro_failed_falling_through", site_key=sk)
            except Exception as e:
                log.debug("macro_replay_error", site_key=sk, error=str(e)[:120])

        # ----- Tier 2: DOM heuristics -----
        try:
            ok, info = await self._heuristics_send_async(page, recipient, message, attachment_path)
            if ok:
                return {"ok": True, "tier": "heuristics", "message": "Bhej diya (heuristics)", "info": info}
        except Exception as e:
            log.debug("heuristics_error", site_key=sk, error=str(e)[:120])

        # ----- Tier 3: ARIA tree -----
        try:
            ok, info = await self._aria_send_async(page, recipient, message, attachment_path)
            if ok:
                return {"ok": True, "tier": "aria", "message": "Bhej diya (ARIA)", "info": info}
        except Exception as e:
            log.debug("aria_error", site_key=sk, error=str(e)[:120])

        return {
            "ok": False,
            "tier": "exhausted",
            "error": (
                f"'{sk}' site pe automatic send fail hua. "
                f"Dashboard mein 'Teach New Site' use kar — tu manually demonstrate kar dega, "
                f"JARVIS forever yaad rakhega."
            ),
            "suggestion": "teach_mode",
            "site_key": sk,
        }

    # ----- Tier 2: heuristics — generic compose+send detection -----

    async def _heuristics_send_async(self, page, recipient: str, message: str, attachment_path: str) -> tuple[bool, dict]:
        """Generic compose+send. Scans the visible DOM for the most likely
        compose box, types the message, finds the most likely send button.
        Recipient is typed first if provided (assume single recipient field).
        """
        # 1. If recipient provided, find the most likely recipient input
        #    (search/to/recipient). Skip if recipient is empty.
        info = {}
        if recipient and recipient.strip():
            rec_typed = await page.evaluate(
                """(name) => {
                    const candidates = Array.from(document.querySelectorAll('input, [contenteditable="true"][role="textbox"], [contenteditable="true"]'));
                    // Score by placeholder/aria-label containing "search", "to", "recipient"
                    let best = null, bestScore = -1;
                    for (const el of candidates) {
                        const ph = (el.getAttribute && el.getAttribute('placeholder') || '').toLowerCase();
                        const al = (el.getAttribute && el.getAttribute('aria-label') || '').toLowerCase();
                        const blob = ph + ' ' + al;
                        let score = 0;
                        if (/search|recipient|to:|find a |jump to|message someone|new message/i.test(blob)) score += 100;
                        if (el.offsetParent === null) score -= 50;  // hidden
                        if (score > bestScore) { bestScore = score; best = el; }
                    }
                    if (best && bestScore > 0) {
                        best.focus();
                        // Type via a real input event for React
                        return true;
                    }
                    return false;
                }""",
                recipient,
            )
            if rec_typed:
                await asyncio.sleep(0.3)
                await page.keyboard.type(recipient.strip(), delay=20)
                await asyncio.sleep(1.5)
                # Try Enter to select the top result
                try:
                    await page.keyboard.press("Enter")
                except Exception:
                    pass
                await asyncio.sleep(1.5)
                info["recipient_typed"] = True

        # 2. Find the compose box for the message body
        compose_info = await page.evaluate(
            """() => {
                // Top candidates: contenteditable textboxes, textareas, large inputs
                const candidates = [];
                for (const el of document.querySelectorAll('div[contenteditable="true"][role="textbox"], div[contenteditable="true"], textarea, input[type="text"]')) {
                    if (!el || el.offsetParent === null) continue;
                    const ph = (el.getAttribute('placeholder') || '').toLowerCase();
                    const al = (el.getAttribute('aria-label') || '').toLowerCase();
                    const blob = ph + ' ' + al;
                    let score = 0;
                    if (el.tagName === 'TEXTAREA') score += 30;
                    if (el.matches('[contenteditable="true"][role="textbox"]')) score += 50;
                    if (el.matches('[contenteditable="true"]')) score += 30;
                    if (/message|compose|post|reply|write|chat|comment|tweet|note/i.test(blob)) score += 80;
                    if (/search|find|filter/i.test(blob)) score -= 100;  // not a compose
                    // Larger boxes more likely to be compose
                    try {
                        const rect = el.getBoundingClientRect();
                        if (rect.width > 200) score += 20;
                        if (rect.height > 30) score += 20;
                    } catch (e) {}
                    candidates.push({el, score, ph, al});
                }
                candidates.sort((a, b) => b.score - a.score);
                if (!candidates.length || candidates[0].score < 30) return null;
                const winner = candidates[0];
                // Mark it for Playwright to find
                winner.el.setAttribute('data-jarvis-compose', '1');
                return {score: winner.score, placeholder: winner.ph, ariaLabel: winner.al};
            }"""
        )
        if not compose_info:
            return False, {"reason": "compose_not_found"}
        info["compose"] = compose_info

        # Type message into compose
        compose_el = await page.query_selector('[data-jarvis-compose="1"]')
        if compose_el is None:
            return False, {"reason": "compose_query_fail"}
        try:
            await compose_el.click()
            await asyncio.sleep(0.2)
            if message:
                await page.keyboard.type(message, delay=10)
                await asyncio.sleep(0.3)
        except Exception as e:
            return False, {"reason": f"compose_type_fail: {e}"}

        # 3. Attachment via expect_file_chooser if requested
        if attachment_path:
            if not os.path.exists(attachment_path):
                return False, {"reason": f"file_not_found: {attachment_path}"}
            # Heuristic: find an "attach" or "upload" button
            try:
                async with page.expect_file_chooser(timeout=15000) as fc_info:
                    attached = await page.evaluate(
                        """() => {
                            const btns = Array.from(document.querySelectorAll('button, div[role="button"], a[role="button"], label[for]'));
                            for (const b of btns) {
                                const t = (b.innerText || '').toLowerCase();
                                const al = (b.getAttribute && b.getAttribute('aria-label') || '').toLowerCase();
                                const blob = t + ' ' + al;
                                if (/attach|upload|file|paperclip|add file|insert file/i.test(blob)) {
                                    b.click();
                                    return true;
                                }
                            }
                            return false;
                        }"""
                    )
                    if not attached:
                        return False, {"reason": "attach_button_not_found"}
                fc = await fc_info.value
                await fc.set_files(attachment_path)
                await asyncio.sleep(3.0)
            except Exception as e:
                return False, {"reason": f"attach_fail: {e}"}

        # 4. Click Send. Heuristic: button with text "Send"/"Post"/"Submit"
        send_clicked = await page.evaluate(
            """() => {
                const btns = Array.from(document.querySelectorAll('button, div[role="button"], a[role="button"]'));
                let best = null, bestScore = -1;
                for (const b of btns) {
                    if (!b || b.offsetParent === null) continue;
                    if (b.disabled || b.getAttribute('aria-disabled') === 'true') continue;
                    const t = (b.innerText || '').trim().toLowerCase();
                    const al = (b.getAttribute('aria-label') || '').toLowerCase();
                    const title = (b.getAttribute('title') || '').toLowerCase();
                    const blob = t + ' ' + al + ' ' + title;
                    let score = 0;
                    if (/^send$|^post$|^submit$|^tweet$|^reply$/i.test(t)) score += 100;
                    if (/send|post message|reply|publish/i.test(al)) score += 80;
                    if (/send|post message|reply/i.test(title)) score += 60;
                    if (/^cancel|close|delete|remove|sign in|log in/i.test(t)) score -= 200;
                    if (score > bestScore) { bestScore = score; best = b; }
                }
                if (best && bestScore > 50) {
                    best.click();
                    return true;
                }
                return false;
            }"""
        )
        if not send_clicked:
            return False, {"reason": "send_button_not_found"}

        # Light verification — page.body contains the message in tail
        await asyncio.sleep(2.0)
        if message:
            try:
                body = await page.evaluate("() => document.body.innerText")
            except Exception:
                body = ""
            if message.strip() in (body or "")[-3000:]:
                return True, info
        return True, {**info, "verified": False}

    # ----- Tier 3: ARIA tree fallback -----

    async def _aria_send_async(self, page, recipient: str, message: str, attachment_path: str) -> tuple[bool, dict]:
        """ARIA-driven send. Uses role=textbox + role=button[name=Send] pattern.
        More strict than heuristics — only works on sites that respect ARIA.
        """
        # 1. Recipient via search/textbox with ARIA name containing relevant keywords
        info = {}
        if recipient and recipient.strip():
            r_clicked = await page.evaluate(
                """(name) => {
                    const tbs = Array.from(document.querySelectorAll('[role="textbox"], input[role="searchbox"]'));
                    for (const el of tbs) {
                        const a = (el.getAttribute('aria-label') || '').toLowerCase();
                        if (/search|recipient|to:|new chat|jump to/i.test(a)) {
                            el.focus();
                            return true;
                        }
                    }
                    return false;
                }""",
                recipient,
            )
            if r_clicked:
                await asyncio.sleep(0.3)
                await page.keyboard.type(recipient.strip(), delay=20)
                await asyncio.sleep(1.5)
                try:
                    await page.keyboard.press("Enter")
                except Exception:
                    pass
                await asyncio.sleep(1.5)

        # 2. Compose via role=textbox with relevant aria-label
        composed = await page.evaluate(
            """() => {
                const tbs = Array.from(document.querySelectorAll('[role="textbox"]'));
                for (const el of tbs) {
                    if (!el || el.offsetParent === null) continue;
                    const a = (el.getAttribute('aria-label') || '').toLowerCase();
                    if (/message|compose|reply|write|note|comment|tweet/i.test(a)) {
                        el.focus();
                        el.setAttribute('data-jarvis-aria-compose', '1');
                        return true;
                    }
                }
                return false;
            }"""
        )
        if not composed:
            return False, {"reason": "aria_compose_not_found"}
        if message:
            await page.keyboard.type(message, delay=10)
            await asyncio.sleep(0.3)

        # 3. Send via role=button with name containing Send/Post/Submit
        send_clicked = await page.evaluate(
            """() => {
                const btns = Array.from(document.querySelectorAll('[role="button"], button'));
                for (const b of btns) {
                    if (!b || b.offsetParent === null) continue;
                    const a = (b.getAttribute('aria-label') || '').toLowerCase();
                    const t = (b.innerText || '').trim().toLowerCase();
                    if (/^send$|^post$|^submit$|^tweet$|^reply$/i.test(t) ||
                        /^send$|send message|post message/i.test(a)) {
                        b.click();
                        return true;
                    }
                }
                return false;
            }"""
        )
        if not send_clicked:
            return False, {"reason": "aria_send_not_found"}

        await asyncio.sleep(2.0)
        info["aria_used"] = True
        if message:
            try:
                body = await page.evaluate("() => document.body.innerText")
            except Exception:
                body = ""
            if message.strip() in (body or "")[-3000:]:
                return True, info
        return True, {**info, "verified": False}

    # ----- Tier 1 / saved macro replay -----

    async def _replay_macro_async(self, page, macro: dict, recipient: str, message: str, attachment_path: str) -> bool:
        """Replay a saved macro on the current page. The macro is a list of
        action dicts: {type: 'click', selector: '...'} | {type: 'type', text: '<MESSAGE>'} | {type: 'wait', ms: 500}.
        Template placeholders {{MESSAGE}}, {{RECIPIENT}}, {{ATTACH}} are
        substituted at replay time.
        """
        actions = (macro or {}).get("macros", {}).get("send", []) or []
        if not actions:
            return False
        for step in actions:
            try:
                kind = step.get("type")
                if kind == "wait":
                    await asyncio.sleep(float(step.get("ms", 300)) / 1000)
                    continue
                if kind == "click":
                    sel = step.get("selector") or ""
                    if not sel:
                        return False
                    el = await page.query_selector(sel)
                    if el is None:
                        return False
                    await el.click(timeout=4000)
                    continue
                if kind == "type":
                    raw = step.get("text") or ""
                    text = (raw
                            .replace("{{MESSAGE}}", message or "")
                            .replace("{{RECIPIENT}}", recipient or "")
                            .replace("{{ATTACH}}", attachment_path or ""))
                    if text:
                        await page.keyboard.type(text, delay=10)
                    continue
                if kind == "press":
                    key = step.get("key", "Enter")
                    await page.keyboard.press(key)
                    continue
                if kind == "file_chooser_click":
                    sel = step.get("selector") or ""
                    if not sel or not attachment_path or not os.path.exists(attachment_path):
                        return False
                    async with page.expect_file_chooser(timeout=15000) as fc_info:
                        await (await page.query_selector(sel)).click()
                    fc = await fc_info.value
                    await fc.set_files(attachment_path)
                    await asyncio.sleep(3.0)
                    continue
            except Exception as e:
                log.info("macro_step_failed", step=str(step)[:120], error=str(e)[:120])
                return False
        await asyncio.sleep(1.0)
        # Light verify
        if message:
            try:
                body = await page.evaluate("() => document.body.innerText")
            except Exception:
                body = ""
            if message.strip() in (body or "")[-3000:]:
                return True
        return True

    # ----- Teach mode (Phase 4) -----

    async def _teach_start_async(self, url: str) -> dict:
        """Inject event listeners into the page that capture user clicks +
        typing. The captured events are stored in window.__jarvis_macro_log__
        until teach_stop_async retrieves them.
        """
        page, sk = await self._ensure_tab_async(url)
        try:
            await page.bring_to_front()
        except Exception:
            pass
        await page.evaluate(
            """() => {
                window.__jarvis_macro_log__ = window.__jarvis_macro_log__ || [];
                if (window.__jarvis_macro_listener_installed__) return;
                window.__jarvis_macro_listener_installed__ = true;
                const log = (entry) => {
                    entry.t = Date.now();
                    window.__jarvis_macro_log__.push(entry);
                };
                const makeSelector = (el) => {
                    if (!el) return null;
                    // Prefer data-* attributes, then id, then nth-of-type
                    const di = el.getAttribute('data-tid') || el.getAttribute('data-testid') || el.getAttribute('data-icon');
                    if (di) return `[data-tid="${di}"], [data-testid="${di}"], [data-icon="${di}"]`;
                    if (el.id) return `#${el.id}`;
                    const al = el.getAttribute('aria-label');
                    if (al) return `[aria-label="${al.replace(/"/g, '\\\\"')}"]`;
                    // Tag + classes
                    let s = el.tagName.toLowerCase();
                    if (el.className && typeof el.className === 'string') {
                        const cls = el.className.split(/\\s+/).filter(c => c && !c.startsWith('css-') && c.length < 30).slice(0, 3);
                        if (cls.length) s += '.' + cls.join('.');
                    }
                    return s;
                };
                document.addEventListener('click', (e) => {
                    const el = e.target.closest('button, [role="button"], a, label, input, [contenteditable="true"]') || e.target;
                    log({type: 'click', selector: makeSelector(el), text: (el.innerText || '').slice(0, 60)});
                }, true);
                document.addEventListener('input', (e) => {
                    const el = e.target;
                    if (!el) return;
                    log({type: 'type', selector: makeSelector(el), value: (el.value || el.innerText || '').slice(0, 200)});
                }, true);
                document.addEventListener('keydown', (e) => {
                    if (e.key === 'Enter' || e.key === 'Tab' || e.key === 'Escape') {
                        log({type: 'press', key: e.key});
                    }
                }, true);
            }"""
        )
        # Reset log explicitly (fresh recording)
        await page.evaluate("() => { window.__jarvis_macro_log__ = []; }")
        self._recording_active[sk] = {"started_at": time.time(), "url": page.url}
        return {"ok": True, "site_key": sk, "url": page.url}

    async def _teach_stop_async(self, site_key: str, label: str) -> dict:
        if site_key not in self._recording_active:
            return {"ok": False, "error": f"No active recording for {site_key}"}
        page = self._tabs.get(site_key)
        if page is None:
            return {"ok": False, "error": "Tab gone"}
        try:
            entries = await page.evaluate("() => window.__jarvis_macro_log__ || []")
        except Exception as e:
            return {"ok": False, "error": f"Log fetch fail: {e}"}

        # Convert raw events into normalized macro steps. Coalesce consecutive
        # typing events on the same selector into one "type" step with template.
        steps: list[dict] = []
        current_type_sel = None
        for ev in entries:
            t = ev.get("type")
            if t == "click":
                steps.append({"type": "click", "selector": ev.get("selector", "")})
                steps.append({"type": "wait", "ms": 500})
                current_type_sel = None
            elif t == "type":
                # Only one "type" step per field — value will be templated to {{MESSAGE}}
                sel = ev.get("selector", "")
                if current_type_sel != sel:
                    steps.append({"type": "click", "selector": sel})
                    steps.append({"type": "type", "text": "{{MESSAGE}}"})
                    current_type_sel = sel
            elif t == "press":
                steps.append({"type": "press", "key": ev.get("key", "Enter")})
                current_type_sel = None

        if not steps:
            self._recording_active.pop(site_key, None)
            return {"ok": False, "error": "Koi action record nahi hua. Tu compose box pe click + type karke send dabaa."}

        macro_data = self._load_macro(site_key) or {"site_key": site_key, "macros": {}}
        macro_data["macros"][label or "send"] = steps
        import datetime as _dt
        macro_data["learned_at"] = _dt.datetime.utcnow().isoformat() + "Z"
        self._save_macro(site_key, macro_data)
        self._recording_active.pop(site_key, None)
        log.info("universal_macro_saved", site_key=site_key, label=label, steps=len(steps))
        return {"ok": True, "site_key": site_key, "label": label, "steps": len(steps)}
