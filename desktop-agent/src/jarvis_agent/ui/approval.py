"""Approval popup — shows a Windows dialog for task approval."""

from __future__ import annotations

import asyncio
import ctypes

from jarvis_agent.utils.logging import get_logger

log = get_logger(__name__)

# Windows MessageBox constants
MB_YESNO = 0x00000004
MB_ICONQUESTION = 0x00000020
MB_TOPMOST = 0x00040000
MB_SETFOREGROUND = 0x00010000
IDYES = 6


async def show_approval_popup(task: dict) -> bool:
    """Show a Windows approval dialog for a task.

    Returns True if user clicks Yes, False for No.
    """
    title = task.get("title", "Unknown Task")
    description = task.get("description", "")
    action_type = task.get("action_type", "other")
    priority = task.get("priority", "medium")

    message = (
        f"Task: {title}\n\n"
        f"Action: {action_type}\n"
        f"Priority: {priority}\n"
    )
    if description:
        message += f"\n{description}\n"
    message += "\nApprove this action?"

    result = await asyncio.to_thread(
        ctypes.windll.user32.MessageBoxW,
        0,
        message,
        "Jarvis - Task Approval",
        MB_YESNO | MB_ICONQUESTION | MB_TOPMOST | MB_SETFOREGROUND,
    )

    approved = result == IDYES
    log.info("approval_result", task_id=task.get("id"), approved=approved)
    return approved
