"""Gmail driver — sends emails via SMTP."""

from __future__ import annotations

import asyncio
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from jarvis_agent.config import AgentConfig
from jarvis_agent.executor.drivers.base import ActionResult, BaseDriver
from jarvis_agent.utils.logging import get_logger

log = get_logger(__name__)


class GmailDriver(BaseDriver):
    """Send emails via Gmail SMTP.

    Requires env vars:
        JARVIS_GMAIL_ADDRESS  — your Gmail address
        JARVIS_GMAIL_APP_PASSWORD — app-specific password (not your real password)
    """

    def __init__(self, config: AgentConfig):
        self._config = config

    async def execute(self, task: dict, payload: dict) -> ActionResult:
        to = payload.get("to")
        subject = payload.get("subject", task.get("title", "Jarvis Message"))
        body = payload.get("message") or payload.get("body") or task.get("description", "")

        if not to:
            return ActionResult(success=False, error="No recipient ('to') specified")

        gmail_addr = self._config.gmail_address
        gmail_pass = self._config.gmail_app_password

        if not gmail_addr or not gmail_pass:
            return ActionResult(
                success=False,
                error="Gmail not configured. Set JARVIS_GMAIL_ADDRESS and JARVIS_GMAIL_APP_PASSWORD",
            )

        try:
            result = await asyncio.to_thread(
                self._send_email, gmail_addr, gmail_pass, to, subject, body
            )
            return result
        except Exception as e:
            return ActionResult(success=False, error=f"Gmail send failed: {e}")

    @staticmethod
    def _send_email(
        from_addr: str, password: str, to: str, subject: str, body: str
    ) -> ActionResult:
        msg = MIMEMultipart()
        msg["From"] = from_addr
        msg["To"] = to
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(from_addr, password)
            server.send_message(msg)

        log.info("email_sent", to=to, subject=subject)
        return ActionResult(success=True, message=f"Email sent to {to}")
