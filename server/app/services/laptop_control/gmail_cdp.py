"""Gmail send via Chrome CDP — uses the user's already-logged-in Gmail accounts.

Why: Most users have multiple Gmail accounts logged into Chrome (work, personal,
client, etc.). SMTP requires an app-password per account (tedious setup). Using
Chrome CDP lets JARVIS send from any account that is already authenticated in
the user's browser — no per-account credential setup.

Account selection:
    Gmail web routes accounts as `/mail/u/<index>/` (u/0 = first, u/1 = second, …).
    Callers can specify `from_account` as either:
        - an integer index ("u/0", "u/1", "0", "1")
        - an email substring (e.g. "ahmed.business" → matched against the
          currently-logged-in account labels found in the menu)
    Defaults to u/0.

Flow:
    1. Connect to Chrome via CDP (same connection as Teams/WhatsApp/Trello)
    2. Find or open a Gmail tab at u/<index>
    3. Use Gmail's deep-link compose URL to pre-fill To / Subject / Body
    4. Attach files via the page's hidden <input type="file"> (set_input_files)
    5. Click Send (or Ctrl+Enter)
    6. Verify by checking the compose dialog disappears
"""

from __future__ import annotations

import os
import re
import time
import urllib.parse
from pathlib import Path
from typing import Any

from app.core.logging import get_logger

log = get_logger(__name__)


def _split_recipients(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        items = value
    else:
        s = str(value)
        for sep in (";", ","):
            if sep in s:
                items = [p.strip() for p in s.split(sep)]
                break
        else:
            items = [s.strip()]
    return [x for x in (i.strip() for i in items) if x]


def _looks_like_email(s: str) -> bool:
    return "@" in s and "." in s.split("@")[-1] and len(s) >= 6


def _resolve_account_index(from_account: str | int | None) -> int:
    """Translate a user-given account hint to a Gmail /u/<idx>/ index.

    Numeric input is taken at face value. String input is parsed for digits
    (e.g. "u/2" → 2). Defaults to 0 when ambiguous. Email-substring matching
    against the live account-switcher is done in `_resolve_account_index_live`.
    """
    if from_account is None or from_account == "":
        return 0
    if isinstance(from_account, int):
        return max(0, from_account)
    s = str(from_account).strip()
    m = re.search(r"\d+", s)
    if m:
        return int(m.group(0))
    return 0


class GmailCDP:
    """Send Gmail via Chrome CDP (user's logged-in accounts, no SMTP needed)."""

    @staticmethod
    def send(
        to: Any,
        subject: str,
        body: str = "",
        attachments: Any | None = None,
        from_account: str | int | None = None,
    ) -> tuple[bool, str]:
        """Send an email through whatever Chrome the CDP is pointed at.

        to: list/string/comma-separated emails (names not resolved here — caller
            should pre-resolve to emails using clients DB if desired).
        from_account: int index, "u/N" string, or email substring. Default 0.
        """
        from app.services.laptop_control import chrome_cdp

        if not chrome_cdp.is_debug_running():
            return False, (
                "Chrome CDP chal nahi raha. JARVIS Chrome (background mode) "
                "khol ke Gmail tab login karwa, phir retry kar."
            )

        recipients = _split_recipients(to)
        email_only = [r for r in recipients if _looks_like_email(r)]
        if not email_only:
            return False, (
                "Email address chahiye (name-resolution Gmail CDP me supported nahi). "
                "Email IDs ke saath retry kar."
            )

        # Resolve attachment paths
        attach_paths: list[str] = []
        unresolved: list[str] = []
        for raw in _split_recipients(attachments):
            ok, p = GmailCDP._resolve_file(raw)
            if ok:
                attach_paths.append(p)
            else:
                unresolved.append(f"{raw} ({p})")
        if unresolved:
            return False, "Attachments nahi mile:\n  - " + "\n  - ".join(unresolved)

        try:
            p, browser, context = chrome_cdp.connect_playwright_cdp()
        except Exception as e:
            return False, f"CDP connect fail: {e}"

        try:
            idx = _resolve_account_index(from_account)

            # If from_account looked like an email, prefer matching by content
            # against the existing Gmail tabs' URLs/titles.
            if isinstance(from_account, str) and "@" in from_account:
                matched_idx = GmailCDP._match_account_by_email(browser, from_account)
                if matched_idx is not None:
                    idx = matched_idx

            # Find a Gmail tab on this user index, else open one.
            gmail_page = GmailCDP._find_gmail_tab(browser, idx)
            if gmail_page is None:
                gmail_page = context.new_page()
                gmail_page.goto(f"https://mail.google.com/mail/u/{idx}/", timeout=30000)

            try:
                gmail_page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass

            # If user got bounced to accounts.google.com login, abort with clear msg.
            cur_url = (gmail_page.url or "").lower()
            if "accounts.google.com" in cur_url or "signin" in cur_url:
                return False, (
                    f"Gmail account u/{idx} JARVIS Chrome mein logged in nahi. "
                    "Pehle iss Chrome mein us account se login kar."
                )

            # Navigate to compose via Gmail's deeplink — Gmail pre-fills these fields.
            to_param = ",".join(email_only)
            params = {
                "view": "cm",
                "fs": "1",
                "to": to_param,
                "su": subject or "",
                "body": body or "",
            }
            compose_url = (
                f"https://mail.google.com/mail/u/{idx}/?"
                + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
            )
            gmail_page.goto(compose_url, timeout=30000, wait_until="domcontentloaded")
            time.sleep(2.0)

            # Re-check login state after compose nav
            cur_url = (gmail_page.url or "").lower()
            if "accounts.google.com" in cur_url or "signin" in cur_url:
                return False, (
                    f"Gmail account u/{idx} logged out — JARVIS Chrome mein login kar."
                )

            # Optional attachments
            if attach_paths:
                # Gmail's hidden file input. Multiple uploads supported.
                file_input = None
                deadline = time.monotonic() + 4
                while time.monotonic() < deadline:
                    file_input = gmail_page.query_selector('input[type="file"][name="Filedata"]')
                    if not file_input:
                        file_input = gmail_page.query_selector('input[type="file"]')
                    if file_input:
                        break
                    time.sleep(0.3)
                if not file_input:
                    return False, "Gmail attach input nahi mila page mein."
                try:
                    file_input.set_input_files(attach_paths)
                except Exception as e:
                    return False, f"Gmail attachment upload fail: {e}"
                # Wait for Gmail to finish uploading (progress chips disappear)
                time.sleep(min(2 + len(attach_paths), 8))

            # Click the Send button (or Ctrl+Enter as fallback)
            send_clicked = False
            for sel in (
                'div[role="button"][data-tooltip*="Send" i]',
                'div[role="button"][aria-label*="Send" i]',
                'div[role="button"][data-tooltip="Send ‪(Ctrl-Enter)‬"]',
                'div[role="button"][data-tooltip*="‪Ctrl" i]',
            ):
                try:
                    btn = gmail_page.query_selector(sel)
                    if btn:
                        btn.click()
                        send_clicked = True
                        break
                except Exception:
                    continue
            if not send_clicked:
                try:
                    gmail_page.keyboard.press("Control+Enter")
                    send_clicked = True
                except Exception:
                    pass

            # Verify the compose dialog disappeared (Gmail removes the popup on success)
            verified = False
            deadline = time.monotonic() + 8.0
            while time.monotonic() < deadline:
                still_compose = (
                    gmail_page.query_selector('div[role="dialog"]')
                    or gmail_page.query_selector('div.aDh')  # Gmail compose popup class
                )
                if not still_compose:
                    verified = True
                    break
                time.sleep(0.4)

            attach_note = ""
            if attach_paths:
                names = ", ".join(os.path.basename(a) for a in attach_paths)
                attach_note = f" + {len(attach_paths)} attachment ({names})"

            if not verified:
                return False, (
                    f"Send button click hua par confirm nahi hua. "
                    f"Gmail tab khol ke check kar (Sent folder)."
                )

            return True, (
                f"Email bhej diya {len(email_only)} log ko via Chrome (u/{idx}): "
                f"{', '.join(email_only)}{attach_note} (background)"
            )
        except Exception as e:
            log.warning("gmail_cdp_send_failed", error=str(e))
            return False, f"Gmail CDP send fail: {e}"
        finally:
            try:
                p.stop()
            except Exception:
                pass

    # ---------- helpers ----------

    @staticmethod
    def _find_gmail_tab(browser, idx: int):
        """Find an existing Gmail tab at /mail/u/<idx>/ — or any Gmail tab if idx=0."""
        url_marker = f"/mail/u/{idx}/"
        candidate_any_idx = None
        for ctx in browser.contexts:
            for page in ctx.pages:
                u = (page.url or "").lower()
                if "mail.google.com" not in u:
                    continue
                if url_marker in u:
                    return page
                if candidate_any_idx is None:
                    candidate_any_idx = page
        # If user asked for u/0 and we found any Gmail tab, use that
        return candidate_any_idx if idx == 0 else None

    @staticmethod
    def _match_account_by_email(browser, email_hint: str) -> int | None:
        """Best-effort: look at open Gmail tabs' titles for the given email."""
        hint = email_hint.lower().strip()
        for ctx in browser.contexts:
            for page in ctx.pages:
                try:
                    u = (page.url or "").lower()
                    if "mail.google.com" not in u:
                        continue
                    title = (page.title() or "").lower()
                    if hint in title:
                        # Parse /mail/u/<idx>/ out of URL
                        m = re.search(r"/mail/u/(\d+)/", u)
                        if m:
                            return int(m.group(1))
                except Exception:
                    continue
        return None

    @staticmethod
    def _resolve_file(name_or_path: str) -> tuple[bool, str]:
        """Reuse the WhatsApp file resolver for consistency."""
        from app.services.laptop_control.whatsapp import WhatsAppAutomation
        return WhatsAppAutomation._resolve_file_path(name_or_path)
