"""Smart Notifications — Jarvis khud message bhejta hai chat mein."""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import func, select, text

from app.core.config import ServerConfig
from app.core.database import async_session
from app.core.logging import get_logger
from app.models.task import Task

log = get_logger(__name__)


class SmartNotifier:
    """Checks for important events and sends proactive messages to chat.

    Runs periodically and inserts messages into chat_messages table
    so they appear in the dashboard chat automatically.
    """

    def __init__(self, config: ServerConfig):
        self._config = config

    async def check_and_notify(self) -> list[str]:
        """Run all notification checks. Returns list of messages sent."""
        notifications = []

        # Check pending tasks that need attention
        msg = await self._check_pending_tasks()
        if msg:
            notifications.append(msg)

        # Check overdue/old tasks
        msg = await self._check_old_tasks()
        if msg:
            notifications.append(msg)

        # Save notifications to chat
        for msg in notifications:
            await self._save_to_chat(msg)

        return notifications

    async def _check_pending_tasks(self) -> str | None:
        """Notify if there are high priority pending tasks."""
        async with async_session() as db:
            result = await db.execute(
                select(Task).where(
                    Task.status == "pending",
                    Task.priority.in_(["high", "urgent"]),
                )
            )
            tasks = result.scalars().all()

        if not tasks:
            return None

        if len(tasks) == 1:
            return f"Bhai, ek urgent task pending hai: \"{tasks[0].title}\" — approve karain?"
        else:
            task_list = "\n".join(f"  • {t.title} ({t.priority})" for t in tasks[:5])
            return f"{len(tasks)} high/urgent tasks pending hain:\n{task_list}\n\nApprove karna hai to Tasks page pe jaao."

    async def _check_old_tasks(self) -> str | None:
        """Notify about tasks pending for more than 24 hours."""
        async with async_session() as db:
            result = await db.execute(
                text("""
                    SELECT COUNT(*) FROM tasks
                    WHERE status = 'pending'
                    AND created_at < datetime('now', '-24 hours')
                """)
            )
            count = result.scalar()

        if count and count > 0:
            return f"{count} tasks 24 ghante se zyada se pending hain — review karo!"
        return None

    async def _save_to_chat(self, message: str) -> None:
        """Insert a proactive message into chat history."""
        async with async_session() as db:
            await db.execute(
                text(
                    "INSERT INTO chat_messages (role, content, actions) VALUES (:role, :content, :actions)"
                ),
                {"role": "assistant", "content": f"🔔 {message}", "actions": "[]"},
            )
            await db.commit()
        log.info("smart_notification_sent", message=message[:60])
