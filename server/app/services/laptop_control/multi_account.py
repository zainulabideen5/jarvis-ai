"""Multi-account routing — Gmail, Slack, Teams, any service with /u/N URLs.

Currently focused on Gmail (mail.google.com/mail/u/0/, u/1/, ... u/13/).
Same pattern can extend to Slack workspaces, multi-Teams tenants.

How it works:
    1. JARVIS Chromium pe user multiple Gmail accounts logged in karta.
    2. Detect kare: kitne accounts hain, har account ka email kya hai.
    3. User dashboard mein labels assigns kare:
        u/0 → "Personal"
        u/1 → "Work"
        u/2 → "Client A"
        ...
    4. User chat mein bole "Personal Gmail se Ahmed ko bhej" — JARVIS
       label resolve karke mail.google.com/mail/u/0/ tab pe action kare.

Storage: server/data/chrome_accounts.json
    {
      "gmail": [
        {"index": 0, "email": "webnewbiz2025@gmail.com", "label": "Personal"},
        {"index": 1, "email": "work@company.com",       "label": "Work"},
        ...
      ]
    }

Uses the SAME persistent Playwright Chromium as WhatsApp/Teams/Gmail/Trello —
shares cookies, so accounts that are signed-in there are immediately available.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

from app.core.logging import get_logger

log = get_logger(__name__)

_CONFIG_PATH = Path(__file__).resolve().parents[3] / "data" / "chrome_accounts.json"


def _load_config() -> dict:
    if not _CONFIG_PATH.exists():
        return {"gmail": []}
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"gmail": []}


def _save_config(data: dict) -> None:
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CONFIG_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


class GmailMultiAccount:
    """Singleton — multi-Gmail account routing within JARVIS Chromium."""

    _instance: "GmailMultiAccount | None" = None

    @classmethod
    def get(cls) -> "GmailMultiAccount":
        if cls._instance is None:
            cls._instance = GmailMultiAccount()
        return cls._instance

    def _wa(self):
        from app.services.laptop_control.wa_playwright import WhatsAppPlaywright
        return WhatsAppPlaywright.get()

    def _submit(self, coro, timeout: float = 90.0):
        return self._wa()._submit(coro, timeout=timeout)

    # ==================================================================
    # Sync API
    # ==================================================================

    def detect_accounts_sync(self, max_probe: int = 14, timeout_sec: float = 90.0) -> dict:
        """Probe u/0 to u/max_probe in the SAME Chromium — detect which
        accounts are logged in, capture each's email address.
        """
        return self._submit(self._detect_accounts_async(max_probe), timeout=timeout_sec)

    def list_labeled_accounts_sync(self) -> list[dict]:
        """Return saved label list. Sync, no Playwright needed."""
        cfg = _load_config()
        return cfg.get("gmail", [])

    def set_label_sync(self, index: int, label: str, email: str = "") -> dict:
        """Assign / update a label for account at index `index`."""
        cfg = _load_config()
        accounts = cfg.get("gmail", [])
        # Update if exists, else add
        found = False
        for a in accounts:
            if a.get("index") == index:
                a["label"] = label
                if email:
                    a["email"] = email
                found = True
                break
        if not found:
            accounts.append({"index": index, "label": label, "email": email or ""})
        cfg["gmail"] = accounts
        _save_config(cfg)
        return {"ok": True, "accounts": accounts}

    def remove_label_sync(self, index: int) -> dict:
        cfg = _load_config()
        accounts = [a for a in cfg.get("gmail", []) if a.get("index") != index]
        cfg["gmail"] = accounts
        _save_config(cfg)
        return {"ok": True, "remaining": len(accounts)}

    def resolve_label(self, label: str) -> int | None:
        """Resolve a label string to an account index. Case-insensitive."""
        if not label:
            return None
        label_low = label.strip().lower()
        cfg = _load_config()
        for a in cfg.get("gmail", []):
            if (a.get("label") or "").lower() == label_low:
                return int(a.get("index", -1))
        return None

    def send_via_label_sync(
        self,
        label: str,
        to: str,
        subject: str = "",
        body: str = "",
        attachment_path: str = "",
        timeout_sec: float = 120.0,
    ) -> dict:
        """Send email from a labeled Gmail account."""
        idx = self.resolve_label(label)
        if idx is None or idx < 0:
            return {"ok": False, "error": f"Label '{label}' set nahi hai. Dashboard mein label assign kar."}
        return self._submit(
            self._send_via_index_async(idx, to, subject, body, attachment_path),
            timeout=timeout_sec,
        )

    def open_account_sync(self, label_or_index, timeout_sec: float = 30.0) -> dict:
        """Open the Gmail tab for a labeled / indexed account."""
        if isinstance(label_or_index, str):
            idx = self.resolve_label(label_or_index)
            if idx is None or idx < 0:
                return {"ok": False, "error": f"Label '{label_or_index}' resolve nahi hua"}
        else:
            idx = int(label_or_index)
        return self._submit(self._open_account_async(idx), timeout=timeout_sec)

    # ==================================================================
    # Async internals
    # ==================================================================

    async def _detect_accounts_async(self, max_probe: int) -> dict:
        """Probe each /u/N URL. If it loads to inbox (not sign-in redirect),
        capture the email shown in the account-switcher avatar.
        """
        wa = self._wa()
        await wa._ensure_browser_async(headless=False, minimize_after_launch=True)
        browser = wa._browser
        detected = []
        # Use a SCRATCH tab so we don't disrupt existing Gmail tab (u/0)
        page = await browser.new_page()
        try:
            for i in range(max_probe):
                url = f"https://mail.google.com/mail/u/{i}/"
                try:
                    await page.goto(url, timeout=20000, wait_until="domcontentloaded")
                except Exception as e:
                    log.debug("gmail_probe_goto_fail", index=i, error=str(e)[:80])
                    continue
                try:
                    await page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass
                await asyncio.sleep(1.2)
                # Check if we're on Gmail inbox vs redirected to login
                final_url = (page.url or "").lower()
                if "accounts.google.com" in final_url or "signin" in final_url:
                    # No account at this slot — but DON'T break. User may
                    # have accounts at u/3, u/5 with gaps. Continue scanning.
                    log.info("gmail_no_account_at_slot", index=i)
                    continue
                # Extract email from account button (top-right circle's aria-label)
                email = await page.evaluate(
                    """() => {
                        // Account switcher button usually has aria-label like
                        // "Google Account: First Last (email@gmail.com)"
                        const candidates = Array.from(document.querySelectorAll('a[aria-label*="@"], [aria-label*="Google Account" i]'));
                        for (const el of candidates) {
                            const al = el.getAttribute('aria-label') || '';
                            const m = al.match(/[\\w.+-]+@[\\w.-]+\\.[a-z]{2,}/i);
                            if (m) return m[0];
                        }
                        return '';
                    }"""
                )
                detected.append({"index": i, "email": email or f"unknown-{i}"})
                log.info("gmail_account_detected", index=i, email=email)
        finally:
            try:
                await page.close()
            except Exception:
                pass
        # Merge with existing config — preserve labels for known indices
        cfg = _load_config()
        existing = {a.get("index"): a for a in cfg.get("gmail", [])}
        merged = []
        for d in detected:
            i = d["index"]
            if i in existing:
                e = existing[i]
                if d.get("email"):
                    e["email"] = d["email"]
                merged.append(e)
            else:
                merged.append({"index": i, "email": d["email"], "label": ""})
        cfg["gmail"] = merged
        _save_config(cfg)
        return {"ok": True, "count": len(merged), "accounts": merged}

    async def _open_account_async(self, idx: int) -> dict:
        wa = self._wa()
        await wa._ensure_browser_async(headless=False, minimize_after_launch=True)
        browser = wa._browser
        url = f"https://mail.google.com/mail/u/{idx}/"
        # Reuse existing tab if any
        target = None
        for p in (browser.pages or []):
            try:
                u = (p.url or "").lower()
                if f"mail.google.com/mail/u/{idx}" in u:
                    target = p
                    break
            except Exception:
                continue
        if target is None:
            target = await browser.new_page()
            try:
                await target.goto(url, timeout=30000, wait_until="domcontentloaded")
            except Exception as e:
                log.debug("gmail_open_account_goto_fail", error=str(e)[:120])
        else:
            try:
                await target.bring_to_front()
            except Exception:
                pass
        try:
            await target.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        await asyncio.sleep(1.0)
        return {"ok": True, "url": target.url, "index": idx}

    async def _send_via_index_async(
        self,
        idx: int,
        to: str,
        subject: str,
        body: str,
        attachment_path: str,
    ) -> dict:
        """Send a Gmail email from account at /u/{idx}/."""
        wa = self._wa()
        await wa._ensure_browser_async(headless=False, minimize_after_launch=True)
        browser = wa._browser
        target_url = f"https://mail.google.com/mail/u/{idx}/"
        # Find or create the tab
        page = None
        for p in (browser.pages or []):
            try:
                u = (p.url or "").lower()
                if f"mail.google.com/mail/u/{idx}" in u:
                    page = p
                    break
            except Exception:
                continue
        if page is None:
            page = await browser.new_page()
            try:
                await page.goto(target_url, timeout=30000, wait_until="domcontentloaded")
            except Exception as e:
                return {"ok": False, "error": f"Tab open fail: {e}"}
        else:
            try:
                await page.bring_to_front()
            except Exception:
                pass

        # Verify logged in (not sign-in page)
        await asyncio.sleep(1.2)
        if "accounts.google.com" in (page.url or "").lower():
            return {"ok": False, "error": f"u/{idx} pe login nahi hai. Browser mein sign in karo."}

        # ----- Compose -----
        compose_btn = None
        for sel in ('div[role="button"][aria-label*="Compose" i]', 'div[gh="cm"]'):
            compose_btn = await page.query_selector(sel)
            if compose_btn:
                break
        if compose_btn is None:
            return {"ok": False, "error": "Compose button nahi mila"}
        try:
            await compose_btn.click()
        except Exception:
            await compose_btn.click(force=True)
        await asyncio.sleep(1.5)

        # ----- Fill To -----
        to_el = None
        for sel in (
            'div[role="dialog"] input[aria-label*="To recipients" i]',
            'textarea[name="to"]',
            'input[aria-label="To"]',
            'div[role="dialog"] input[type="email"]',
        ):
            to_el = await page.query_selector(sel)
            if to_el:
                break
        if to_el is None:
            return {"ok": False, "error": "To field nahi mila"}

        await to_el.click()
        await asyncio.sleep(0.2)
        emails = [e.strip() for e in (to or "").replace(";", ",").split(",") if e.strip()]
        if not emails:
            return {"ok": False, "error": "No 'to' provided"}
        for i, em in enumerate(emails):
            await page.keyboard.type(em, delay=10)
            await asyncio.sleep(0.7)
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
            await asyncio.sleep(0.2)
            if i < len(emails) - 1:
                await page.keyboard.type(", ", delay=10)
                await asyncio.sleep(0.2)
        await page.keyboard.press("Tab")
        await asyncio.sleep(0.3)

        # ----- Subject -----
        if subject:
            subj_el = None
            for sel in ('div[role="dialog"] input[name="subjectbox"]', 'input[aria-label="Subject"]'):
                subj_el = await page.query_selector(sel)
                if subj_el:
                    break
            if subj_el:
                await subj_el.click()
                await asyncio.sleep(0.2)
                await page.keyboard.type(subject, delay=10)

        # ----- Body -----
        body_el = None
        for sel in (
            'div[role="dialog"] div[role="textbox"][aria-label*="Message Body" i]',
            'div[aria-label="Message Body"]',
            'div[role="textbox"][contenteditable="true"]',
        ):
            body_el = await page.query_selector(sel)
            if body_el:
                break
        if body_el is None:
            return {"ok": False, "error": "Body field nahi mila"}
        await body_el.click()
        await asyncio.sleep(0.2)
        if body:
            await page.keyboard.type(body, delay=8)
            await asyncio.sleep(0.3)

        # ----- Attachment -----
        if attachment_path:
            if not os.path.exists(attachment_path):
                return {"ok": False, "error": f"Attachment file nahi mili: {attachment_path}"}
            attach_btn = None
            for sel in (
                'div[role="dialog"] div[aria-label*="Attach files" i]',
                'div[command="Files"]',
                'div[role="dialog"] div[role="button"][aria-label*="Attach" i]',
            ):
                attach_btn = await page.query_selector(sel)
                if attach_btn:
                    break
            if attach_btn is None:
                return {"ok": False, "error": "Attach button nahi mila"}
            try:
                async with page.expect_file_chooser(timeout=15000) as fc_info:
                    await attach_btn.click()
                fc = await fc_info.value
                await fc.set_files(attachment_path)
            except Exception as e:
                return {"ok": False, "error": f"Attach fail: {e}"}
            await asyncio.sleep(4.0)

        # ----- Re-verify session before send -----
        # Session can expire while user was filling out the form. If we
        # send blindly after redirect, we either click on a sign-in page
        # OR fail silently. Re-check explicitly.
        try:
            current_url = (page.url or "").lower()
        except Exception:
            current_url = ""
        if "accounts.google.com" in current_url or "signin" in current_url:
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
            return {"ok": False, "error": f"Session u/{idx} pe expire ho gayi — Workspace ko dobara sign-in karwao."}

        # ----- Send -----
        send_btn = None
        for sel in (
            'div[role="dialog"] div[role="button"][data-tooltip*="Send" i]',
            'div[role="dialog"] div[role="button"][aria-label*="Send" i]',
            'div[role="dialog"] div[role="button"][data-tooltip*="Ctrl-Enter" i]',
            'div[role="dialog"] button[aria-label*="Send" i]',
        ):
            send_btn = await page.query_selector(sel)
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
            # Fallback shortcut
            try:
                if body_el:
                    await body_el.click()
                    await asyncio.sleep(0.2)
            except Exception:
                pass
            await page.keyboard.press("Control+Enter")

        # ----- Verify -----
        for _ in range(10):
            await asyncio.sleep(1.0)
            still_open = await page.query_selector('div[role="dialog"] input[name="subjectbox"]')
            if still_open is None:
                return {"ok": True, "tier": "compose-closed", "from_index": idx, "to": ", ".join(emails)}
            try:
                body_text = await page.evaluate("() => document.body.innerText")
            except Exception:
                body_text = ""
            if any(marker in body_text for marker in (
                "Message sent", "message sent", "संदेश भेजा", "भेजा गया",
                "Mensaje enviado", "envoyé", "Conversation marked",
            )):
                return {"ok": True, "tier": "toast", "from_index": idx, "to": ", ".join(emails)}
        return {"ok": False, "error": "Send confirm nahi hua 10s mein"}
