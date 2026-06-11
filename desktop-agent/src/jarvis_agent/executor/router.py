"""Action Router — dispatches approved tasks to the correct driver."""

from __future__ import annotations

from jarvis_agent.executor.drivers.base import ActionResult
from jarvis_agent.executor.drivers.browser_driver import BrowserDriver
from jarvis_agent.executor.drivers.discord_driver import DiscordDriver
from jarvis_agent.executor.drivers.gmail_driver import GmailDriver
from jarvis_agent.executor.drivers.notion_driver import NotionDriver
from jarvis_agent.executor.drivers.reminder_driver import ReminderDriver
from jarvis_agent.executor.drivers.slack_driver import SlackDriver
from jarvis_agent.executor.drivers.telegram_driver import TelegramDriver
from jarvis_agent.executor.drivers.vision_driver import VisionDriver
from jarvis_agent.executor.drivers.whatsapp_driver import WhatsAppDriver
from jarvis_agent.config import AgentConfig
from jarvis_agent.utils.logging import get_logger

log = get_logger(__name__)

# Platforms that use API drivers
API_PLATFORMS = {"gmail", "email", "telegram", "slack", "discord", "notion"}
# Platforms that use browser automation
BROWSER_PLATFORMS = {"whatsapp", "linkedin", "twitter", "x", "instagram", "facebook", "fb"}


class ActionRouter:
    """Routes task actions to the appropriate driver.

    Each action_type maps to a driver. The action_payload dict
    provides driver-specific params (e.g., to, message, platform).
    """

    def __init__(self, config: AgentConfig):
        self._config = config
        self._drivers = {
            "send_message": self._route_message,
            "api_call": self._route_api_call,
            "browser_action": self._route_browser,
            "reminder": self._route_reminder,
            "create_doc": self._route_create_doc,
            "schedule_meeting": self._route_generic,
            "other": self._route_generic,
        }
        # Lazy-init drivers
        self._gmail = None
        self._telegram = None
        self._slack = None
        self._discord = None
        self._notion = None
        self._whatsapp = None
        self._browser = None
        self._vision = None
        self._reminder = None

    def _get_gmail(self) -> GmailDriver:
        if self._gmail is None:
            self._gmail = GmailDriver(self._config)
        return self._gmail

    def _get_telegram(self) -> TelegramDriver:
        if self._telegram is None:
            self._telegram = TelegramDriver(self._config)
        return self._telegram

    def _get_slack(self) -> SlackDriver:
        if self._slack is None:
            self._slack = SlackDriver(self._config)
        return self._slack

    def _get_discord(self) -> DiscordDriver:
        if self._discord is None:
            self._discord = DiscordDriver(self._config)
        return self._discord

    def _get_notion(self) -> NotionDriver:
        if self._notion is None:
            self._notion = NotionDriver(self._config)
        return self._notion

    def _get_whatsapp(self) -> WhatsAppDriver:
        if self._whatsapp is None:
            self._whatsapp = WhatsAppDriver(self._config)
        return self._whatsapp

    def _get_browser(self) -> BrowserDriver:
        if self._browser is None:
            self._browser = BrowserDriver(self._config)
        return self._browser

    def _get_vision(self) -> VisionDriver:
        if self._vision is None:
            self._vision = VisionDriver(self._config)
        return self._vision

    def _get_reminder(self) -> ReminderDriver:
        if self._reminder is None:
            self._reminder = ReminderDriver(self._config)
        return self._reminder

    async def execute(self, task: dict) -> ActionResult:
        """Execute a task action. Returns ActionResult with success/failure."""
        action_type = task.get("action_type", "other")
        handler = self._drivers.get(action_type, self._route_generic)

        log.info(
            "routing_action",
            task_id=task.get("id"),
            action_type=action_type,
        )

        try:
            return await handler(task)
        except Exception as e:
            log.error("action_failed", task_id=task.get("id"), error=str(e))
            return ActionResult(success=False, error=str(e))

    async def _route_message(self, task: dict) -> ActionResult:
        """Route send_message to the right platform driver."""
        payload = _parse_payload(task.get("action_payload"))
        platform = payload.get("platform", "").lower()

        if platform in ("gmail", "email"):
            return await self._get_gmail().execute(task, payload)
        elif platform == "telegram":
            return await self._get_telegram().execute(task, payload)
        elif platform == "slack":
            return await self._get_slack().execute(task, payload)
        elif platform == "discord":
            return await self._get_discord().execute(task, payload)
        elif platform == "whatsapp":
            return await self._get_whatsapp().execute(task, payload)
        elif platform in BROWSER_PLATFORMS:
            return await self._get_browser().execute(task, payload)
        else:
            # Fall back to vision driver for unknown platforms
            log.warning("unknown_platform", platform=platform, fallback="vision")
            return await self._get_vision().execute(task, payload)

    async def _route_api_call(self, task: dict) -> ActionResult:
        """Route API calls to the appropriate driver."""
        payload = _parse_payload(task.get("action_payload"))
        platform = payload.get("platform", "").lower()

        if platform in ("gmail", "email"):
            return await self._get_gmail().execute(task, payload)
        elif platform == "telegram":
            return await self._get_telegram().execute(task, payload)
        elif platform == "slack":
            return await self._get_slack().execute(task, payload)
        elif platform == "discord":
            return await self._get_discord().execute(task, payload)
        elif platform == "notion":
            return await self._get_notion().execute(task, payload)
        else:
            return await self._get_vision().execute(task, payload)

    async def _route_browser(self, task: dict) -> ActionResult:
        """Route browser actions to the right browser driver."""
        payload = _parse_payload(task.get("action_payload"))
        platform = payload.get("platform", "").lower()

        if platform == "whatsapp":
            return await self._get_whatsapp().execute(task, payload)
        elif platform in BROWSER_PLATFORMS:
            return await self._get_browser().execute(task, payload)
        else:
            return await self._get_vision().execute(task, payload)

    async def _route_reminder(self, task: dict) -> ActionResult:
        """Handle reminders."""
        payload = _parse_payload(task.get("action_payload"))
        return await self._get_reminder().execute(task, payload)

    async def _route_create_doc(self, task: dict) -> ActionResult:
        """Route document creation to Notion or fallback."""
        payload = _parse_payload(task.get("action_payload"))
        platform = payload.get("platform", "").lower()

        if platform == "notion":
            return await self._get_notion().execute(task, payload)
        else:
            # Default: try Notion if configured, else vision
            if self._config.notion_token:
                return await self._get_notion().execute(task, payload)
            return await self._get_vision().execute(task, payload)

    async def _route_generic(self, task: dict) -> ActionResult:
        """Fallback: use vision driver to handle any action via screenshot + AI."""
        payload = _parse_payload(task.get("action_payload"))
        return await self._get_vision().execute(task, payload)

    async def close(self) -> None:
        """Clean up driver resources."""
        if self._whatsapp:
            await self._whatsapp.close()
        if self._browser:
            await self._browser.close()


def _parse_payload(payload) -> dict:
    """Parse action_payload which might be a JSON string or already a dict."""
    if payload is None:
        return {}
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str):
        import json
        try:
            return json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            return {"raw": payload}
    return {}
