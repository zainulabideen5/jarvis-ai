"""Telegram driver — sends messages via Telegram Bot API."""

from __future__ import annotations

import asyncio
import urllib.request
import urllib.parse
import json

from jarvis_agent.config import AgentConfig
from jarvis_agent.executor.drivers.base import ActionResult, BaseDriver
from jarvis_agent.utils.logging import get_logger

log = get_logger(__name__)


class TelegramDriver(BaseDriver):
    """Send Telegram messages via Bot API.

    Requires env vars:
        JARVIS_TELEGRAM_BOT_TOKEN — from @BotFather
        JARVIS_TELEGRAM_CHAT_ID — default chat ID (can be overridden in payload)
    """

    def __init__(self, config: AgentConfig):
        self._config = config

    async def execute(self, task: dict, payload: dict) -> ActionResult:
        message = payload.get("message") or task.get("description") or task.get("title", "")
        chat_id = payload.get("chat_id") or self._config.telegram_chat_id

        if not message:
            return ActionResult(success=False, error="No message to send")

        token = self._config.telegram_bot_token
        if not token:
            return ActionResult(
                success=False,
                error="Telegram not configured. Set JARVIS_TELEGRAM_BOT_TOKEN",
            )
        if not chat_id:
            return ActionResult(
                success=False,
                error="No chat_id. Set JARVIS_TELEGRAM_CHAT_ID or include in payload",
            )

        try:
            result = await asyncio.to_thread(
                self._send_message, token, chat_id, message
            )
            return result
        except Exception as e:
            return ActionResult(success=False, error=f"Telegram send failed: {e}")

    @staticmethod
    def _send_message(token: str, chat_id: str, text: str) -> ActionResult:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
        }).encode()

        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())

        if body.get("ok"):
            log.info("telegram_sent", chat_id=chat_id)
            return ActionResult(success=True, message=f"Telegram message sent to {chat_id}")
        else:
            return ActionResult(success=False, error=f"Telegram API error: {body}")
