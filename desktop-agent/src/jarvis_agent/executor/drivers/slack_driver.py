"""Slack driver — sends messages via Slack Webhook or Bot API."""

from __future__ import annotations

import asyncio
import json
import urllib.request

from jarvis_agent.config import AgentConfig
from jarvis_agent.executor.drivers.base import ActionResult, BaseDriver
from jarvis_agent.utils.logging import get_logger

log = get_logger(__name__)


class SlackDriver(BaseDriver):
    """Send Slack messages via Incoming Webhook or Bot Token API.

    Env vars:
        JARVIS_SLACK_WEBHOOK_URL — Incoming Webhook URL (simplest setup)
        JARVIS_SLACK_BOT_TOKEN — Bot token for channel/DM posting (optional)
        JARVIS_SLACK_DEFAULT_CHANNEL — Default channel ID (optional)
    """

    def __init__(self, config: AgentConfig):
        self._config = config

    async def execute(self, task: dict, payload: dict) -> ActionResult:
        message = payload.get("message") or task.get("description") or task.get("title", "")
        channel = payload.get("channel") or self._config.slack_default_channel

        if not message:
            return ActionResult(success=False, error="No message to send")

        # Prefer webhook (simpler), fall back to bot token
        webhook_url = self._config.slack_webhook_url
        bot_token = self._config.slack_bot_token

        if webhook_url:
            return await asyncio.to_thread(self._send_webhook, webhook_url, message)
        elif bot_token and channel:
            return await asyncio.to_thread(self._send_bot_api, bot_token, channel, message)
        else:
            return ActionResult(
                success=False,
                error="Slack not configured. Set JARVIS_SLACK_WEBHOOK_URL or JARVIS_SLACK_BOT_TOKEN + JARVIS_SLACK_DEFAULT_CHANNEL",
            )

    @staticmethod
    def _send_webhook(webhook_url: str, text: str) -> ActionResult:
        body = json.dumps({"text": text}).encode()
        req = urllib.request.Request(
            webhook_url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()

        log.info("slack_webhook_sent")
        return ActionResult(success=True, message="Slack message sent via webhook")

    @staticmethod
    def _send_bot_api(token: str, channel: str, text: str) -> ActionResult:
        body = json.dumps({"channel": channel, "text": text}).encode()
        req = urllib.request.Request(
            "https://slack.com/api/chat.postMessage",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())

        if data.get("ok"):
            log.info("slack_bot_sent", channel=channel)
            return ActionResult(success=True, message=f"Slack message sent to {channel}")
        else:
            return ActionResult(success=False, error=f"Slack API error: {data.get('error')}")
