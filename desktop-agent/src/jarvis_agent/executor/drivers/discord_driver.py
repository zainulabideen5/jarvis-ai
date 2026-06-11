"""Discord driver — sends messages via Discord Webhook."""

from __future__ import annotations

import asyncio
import json
import urllib.request

from jarvis_agent.config import AgentConfig
from jarvis_agent.executor.drivers.base import ActionResult, BaseDriver
from jarvis_agent.utils.logging import get_logger

log = get_logger(__name__)


class DiscordDriver(BaseDriver):
    """Send Discord messages via Webhook.

    Env vars:
        JARVIS_DISCORD_WEBHOOK_URL — Discord channel webhook URL
    """

    def __init__(self, config: AgentConfig):
        self._config = config

    async def execute(self, task: dict, payload: dict) -> ActionResult:
        message = payload.get("message") or task.get("description") or task.get("title", "")
        webhook_url = payload.get("webhook_url") or self._config.discord_webhook_url

        if not message:
            return ActionResult(success=False, error="No message to send")

        if not webhook_url:
            return ActionResult(
                success=False,
                error="Discord not configured. Set JARVIS_DISCORD_WEBHOOK_URL",
            )

        try:
            return await asyncio.to_thread(self._send_webhook, webhook_url, message)
        except Exception as e:
            return ActionResult(success=False, error=f"Discord send failed: {e}")

    @staticmethod
    def _send_webhook(webhook_url: str, content: str) -> ActionResult:
        body = json.dumps({"content": content}).encode()
        req = urllib.request.Request(
            webhook_url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            # Discord returns 204 No Content on success
            pass

        log.info("discord_sent")
        return ActionResult(success=True, message="Discord message sent")
