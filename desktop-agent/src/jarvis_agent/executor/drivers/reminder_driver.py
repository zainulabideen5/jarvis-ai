"""Reminder driver — shows Windows toast notifications."""

from __future__ import annotations

import asyncio
import ctypes

from jarvis_agent.config import AgentConfig
from jarvis_agent.executor.drivers.base import ActionResult, BaseDriver
from jarvis_agent.utils.logging import get_logger

log = get_logger(__name__)


class ReminderDriver(BaseDriver):
    """Show desktop notification reminders using Windows MessageBox."""

    def __init__(self, config: AgentConfig):
        self._config = config

    async def execute(self, task: dict, payload: dict) -> ActionResult:
        title = payload.get("title") or task.get("title", "Jarvis Reminder")
        message = (
            payload.get("message")
            or task.get("description")
            or task.get("title", "")
        )

        if not message:
            return ActionResult(success=False, error="No reminder message")

        try:
            await asyncio.to_thread(self._show_notification, title, message)
            log.info("reminder_shown", title=title)
            return ActionResult(success=True, message=f"Reminder shown: {title}")
        except Exception as e:
            return ActionResult(success=False, error=f"Reminder failed: {e}")

    @staticmethod
    def _show_notification(title: str, message: str) -> None:
        """Show a Windows message box (non-blocking via thread)."""
        MB_OK = 0x00000000
        MB_ICONINFORMATION = 0x00000040
        MB_TOPMOST = 0x00040000
        ctypes.windll.user32.MessageBoxW(
            0, message, f"Jarvis: {title}", MB_OK | MB_ICONINFORMATION | MB_TOPMOST
        )
