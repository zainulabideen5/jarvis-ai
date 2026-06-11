"""Action Router — dispatches approved tasks to the correct driver.

Per-app drivers (WhatsApp/Gmail/Telegram/Slack/Discord/Notion/Playwright
browser) were removed in the universal-engine repivot. Messaging actions
now return a clear "rebuilding" result until the new UIA-based universal
engine lands. Reminder and vision (screenshot fallback) drivers remain.
"""

from __future__ import annotations

from jarvis_agent.executor.drivers.base import ActionResult
from jarvis_agent.executor.drivers.reminder_driver import ReminderDriver
from jarvis_agent.executor.drivers.vision_driver import VisionDriver
from jarvis_agent.config import AgentConfig
from jarvis_agent.utils.logging import get_logger

log = get_logger(__name__)

REMOVED_MESSAGE = (
    "Yeh feature naye universal engine mein rebuild ho raha hai — "
    "purane per-app drivers hata diye gaye hain."
)


class ActionRouter:
    """Routes task actions to the appropriate driver.

    Each action_type maps to a driver. The action_payload dict
    provides driver-specific params (e.g., to, message, platform).
    """

    def __init__(self, config: AgentConfig):
        self._config = config
        self._drivers = {
            "send_message": self._route_removed,
            "api_call": self._route_removed,
            "browser_action": self._route_removed,
            "reminder": self._route_reminder,
            "create_doc": self._route_removed,
            "schedule_meeting": self._route_generic,
            "other": self._route_generic,
        }
        # Lazy-init drivers
        self._vision = None
        self._reminder = None

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

    async def _route_removed(self, task: dict) -> ActionResult:
        """Per-app drivers removed — pending universal engine rebuild."""
        log.warning("action_removed", task_id=task.get("id"))
        return ActionResult(success=False, error=REMOVED_MESSAGE)

    async def _route_reminder(self, task: dict) -> ActionResult:
        """Handle reminders."""
        payload = _parse_payload(task.get("action_payload"))
        return await self._get_reminder().execute(task, payload)

    async def _route_generic(self, task: dict) -> ActionResult:
        """Fallback: use vision driver to handle any action via screenshot + AI."""
        payload = _parse_payload(task.get("action_payload"))
        return await self._get_vision().execute(task, payload)

    async def close(self) -> None:
        """Clean up driver resources."""
        return None


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
