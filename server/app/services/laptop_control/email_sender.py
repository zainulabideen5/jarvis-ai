"""Server-side email sender — Gmail SMTP, multi-recipient, attachments.

Pure background (no browser, no UI): code talks SMTP directly to Google's mail
server. Faster and more reliable than CDP-driven Gmail web automation.
"""

from __future__ import annotations

import mimetypes
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path

from app.core.logging import get_logger

log = get_logger(__name__)


def _split_recipients(value: object) -> list[str]:
    """Accept a list, comma/semicolon-separated string, or single string. Return clean list."""
    if not value:
        return []
    if isinstance(value, list):
        items = value
    else:
        s = str(value)
        # Split on common separators
        for sep in (";", ","):
            if sep in s:
                items = [p.strip() for p in s.split(sep)]
                break
        else:
            items = [s.strip()]
    return [x for x in (i.strip() for i in items) if x]


def _looks_like_email(s: str) -> bool:
    return "@" in s and "." in s.split("@")[-1] and len(s) >= 6


class EmailSender:
    """Send emails via Gmail SMTP. Resolves bare names to emails via clients DB."""

    @staticmethod
    def send(
        to,
        subject: str,
        body: str,
        attachments: object | None = None,
        from_addr: str | None = None,
        password: str | None = None,
    ) -> tuple[bool, str]:
        """Send an email.

        to: list of emails OR comma/semicolon-separated string OR single string.
            Bare names (e.g. "Ahmed") get looked up in clients DB.
        subject: email subject.
        body: email body (plain text; HTML not supported in this minimal version).
        attachments: optional list of file paths or comma-separated string.
            Bare filenames are resolved via FileOperations.find_files.
        from_addr / password: override sender (defaults to .env config).

        Send chain (in priority order):
          1. Outlook COM (silent, zero setup — uses local Outlook profile)
          2. SMTP fallback — uses Gmail app password
        """
        if not subject:
            subject = "(no subject)"
        body = body or ""

        # ===== Outlook COM (silent — works on user's local Outlook profile) =====
        # No credentials needed — uses user's authenticated Outlook desktop.
        # 100% local + silent. Tries first because zero setup, zero LLM cost.
        try:
            from app.services.laptop_control.office_com import OfficeCOM
            # Resolve recipients first
            raw_oc_to = _split_recipients(to)
            oc_emails = []
            for r in raw_oc_to:
                if _looks_like_email(r):
                    oc_emails.append(r)
                else:
                    em = EmailSender._lookup_email_by_name(r)
                    if em:
                        oc_emails.append(em)
            if oc_emails:
                # Attachments
                att_list = []
                if attachments:
                    atts_in = attachments if isinstance(attachments, list) else _split_recipients(attachments)
                    for a in atts_in:
                        ok_a, resolved_a = EmailSender._resolve_file_path(a)
                        if ok_a:
                            att_list.append(resolved_a)
                r_oc = OfficeCOM.get().outlook_send_email(
                    to=oc_emails,
                    subject=subject,
                    body=body,
                    attachments=att_list,
                )
                if r_oc.get("ok"):
                    return True, f"Email bhej diya {', '.join(oc_emails)} ko (via Outlook desktop)"
                log.info("outlook_com_failed_fallback", error=r_oc.get("error", ""))
        except Exception as e:
            log.info("outlook_com_init_fallback", error=str(e)[:200])

        # Resolve sender credentials
        if not from_addr or not password:
            try:
                from app.core.config import ServerConfig
                cfg = ServerConfig()
                from_addr = from_addr or cfg.gmail_address
                password = password or cfg.gmail_app_password
            except Exception as e:
                return False, f"Email config load fail: {e}"

        if not from_addr or not password:
            return False, (
                "Gmail credentials nahi configure hain. server/.env mein "
                "JARVIS_GMAIL_ADDRESS aur JARVIS_GMAIL_APP_PASSWORD daalo. "
                "App password yahaan se: https://myaccount.google.com/apppasswords"
            )

        # Resolve recipients (each may be email OR bare name)
        raw_recipients = _split_recipients(to)
        if not raw_recipients:
            return False, "Recipient (to) dena hoga"

        resolved_recipients: list[str] = []
        unresolved: list[str] = []
        for r in raw_recipients:
            if _looks_like_email(r):
                resolved_recipients.append(r)
                continue
            email = EmailSender._lookup_email_by_name(r)
            if email:
                resolved_recipients.append(email)
            else:
                unresolved.append(r)

        if unresolved:
            return False, (
                f"In recipients ka email nahi mila: {', '.join(unresolved)}. "
                f"Email address ke saath bata ya Clients page mein add kar."
            )
        if not resolved_recipients:
            return False, "Koi valid recipient nahi mila"

        # Resolve attachments
        attach_paths: list[str] = []
        unresolved_attach: list[str] = []
        for raw in _split_recipients(attachments):
            ok, path_or_err = EmailSender._resolve_file_path(raw)
            if ok:
                attach_paths.append(path_or_err)
            else:
                unresolved_attach.append(f"{raw} ({path_or_err})")
        if unresolved_attach:
            return False, "Attachments nahi mile:\n  - " + "\n  - ".join(unresolved_attach)

        # Build message
        try:
            msg = EmailMessage()
            msg["From"] = from_addr
            msg["To"] = ", ".join(resolved_recipients)
            msg["Subject"] = subject
            msg.set_content(body)

            for path in attach_paths:
                ctype, encoding = mimetypes.guess_type(path)
                if ctype is None or encoding is not None:
                    ctype = "application/octet-stream"
                maintype, subtype = ctype.split("/", 1)
                with open(path, "rb") as fh:
                    data = fh.read()
                msg.add_attachment(
                    data,
                    maintype=maintype,
                    subtype=subtype,
                    filename=os.path.basename(path),
                )
        except Exception as e:
            return False, f"Email message build fail: {e}"

        # Send via SMTP_SSL (Gmail)
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
                server.login(from_addr, password)
                server.send_message(msg)
        except smtplib.SMTPAuthenticationError as e:
            return False, (
                "Gmail login fail (SMTPAuthenticationError). App password galat ya "
                "2FA + App Password setup nahi hua. https://myaccount.google.com/apppasswords"
            )
        except Exception as e:
            log.warning("email_send_failed", error=str(e))
            return False, f"Email send fail: {e}"

        log.info(
            "email_sent",
            to_count=len(resolved_recipients),
            attach_count=len(attach_paths),
        )
        attach_summary = ""
        if attach_paths:
            names = ", ".join(os.path.basename(p) for p in attach_paths)
            attach_summary = f" + {len(attach_paths)} attachment ({names})"
        return True, (
            f"Email bhej diya {len(resolved_recipients)} log ko: "
            f"{', '.join(resolved_recipients)}{attach_summary}"
        )

    @staticmethod
    def _lookup_email_by_name(name: str) -> str | None:
        """Look up email from clients DB — flexible matching (mirrors WhatsApp lookup)."""
        try:
            import sqlite3

            for candidate in (
                Path("data/jarvis.db"),
                Path("server/data/jarvis.db"),
                Path(__file__).parent.parent.parent.parent / "data" / "jarvis.db",
            ):
                if candidate.exists():
                    db_path = candidate
                    break
            else:
                return None

            STOPWORDS = {
                "client", "ka", "ki", "ke", "wala", "wali", "naam", "ji",
                "saab", "sahab", "sir", "bhai", "yaar", "yr",
            }
            words = [w for w in name.lower().split() if w and w not in STOPWORDS]
            cleaned = " ".join(words) if words else name.lower()

            # `with sqlite3.connect(...)` guarantees close on exception.
            with sqlite3.connect(str(db_path)) as conn:
                cur = conn.cursor()

                cur.execute(
                    "SELECT name, email FROM clients WHERE LOWER(name) = ? AND email IS NOT NULL AND email != '' LIMIT 1",
                    (cleaned,),
                )
                row = cur.fetchone()

                if not row:
                    cur.execute(
                        "SELECT name, email FROM clients WHERE LOWER(name) LIKE ? AND email IS NOT NULL AND email != '' LIMIT 1",
                        (f"%{cleaned}%",),
                    )
                    row = cur.fetchone()

                if not row and words:
                    cur.execute(
                        "SELECT name, email FROM clients WHERE email IS NOT NULL AND email != '' AND LENGTH(name) >= 2"
                    )
                    for db_name, db_email in cur.fetchall():
                        if db_name and db_name.lower() in cleaned:
                            row = (db_name, db_email)
                            break
                        db_words = set((db_name or "").lower().split())
                        if db_words & set(words):
                            row = (db_name, db_email)
                            break

                # Last-resort fuzzy match (typos)
                if not row:
                    import difflib
                    cur.execute(
                        "SELECT name, email FROM clients WHERE email IS NOT NULL AND email != '' AND LENGTH(name) >= 2"
                    )
                    all_rows = cur.fetchall()
                    if all_rows:
                        by_name = {(n or "").lower(): (n, e) for n, e in all_rows if n}
                        matches = difflib.get_close_matches(cleaned, list(by_name.keys()), n=1, cutoff=0.75)
                        if matches:
                            row = by_name[matches[0]]

            return row[1] if row else None
        except Exception as e:
            log.debug("email_lookup_failed", error=str(e))
            return None

    @staticmethod
    def _resolve_file_path(name_or_path: str) -> tuple[bool, str]:
        """Resolve a bare filename to a full path via FileOperations.find_files."""
        raw = (name_or_path or "").strip().strip('"').strip("'")
        if not raw:
            return False, "file name khali hai"
        p = Path(os.path.expandvars(os.path.expanduser(raw)))
        if p.exists() and p.is_file():
            return True, str(p)
        try:
            from app.services.laptop_control.files import FileOperations
            results = FileOperations.find_files(raw)
            for r in results or []:
                if not r.get("is_folder"):
                    return True, r["path"]
        except Exception as e:
            return False, f"file search fail: {e}"
        return False, "file nahi mili"
