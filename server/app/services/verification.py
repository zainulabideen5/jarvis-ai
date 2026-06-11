"""Verification Service — interactive task verification flow after meetings.

Flow:
    1. Meeting ends -> tasks extracted
    2. VerificationSession created with all tasks grouped by client
    3. Chat shows tasks one-by-one to boss
    4. Boss approves/rejects each
    5. On approve -> chat asks platform (WhatsApp/Teams/Email)
    6. On platform select -> message sent via laptop_control
    7. Move to next task
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Literal

from sqlalchemy import text

from app.core.database import async_session
from app.core.logging import get_logger

log = get_logger(__name__)


VerificationState = Literal["awaiting_approval", "awaiting_platform", "done"]


class VerificationService:
    """Singleton — tracks the active verification session.

    Earlier `get()` had a check-then-set race: two concurrent requests could
    both see `_instance is None` and create separate instances, dropping any
    pending session that was on the "loser". Initialising at class definition
    time eliminates the race entirely; `get()` just returns the existing one.
    """

    _instance: "VerificationService | None" = None

    def __init__(self):
        self.active_session: dict | None = None

    @classmethod
    def get(cls) -> "VerificationService":
        # _instance is guaranteed non-None — assigned right after this class body.
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @staticmethod
    async def ensure_table() -> None:
        async with async_session() as db:
            await db.execute(text("""
                CREATE TABLE IF NOT EXISTS verification_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    meeting_id INTEGER,
                    task_id INTEGER,
                    assigned_to TEXT,
                    title TEXT,
                    decision TEXT,
                    platform TEXT,
                    delivery_status TEXT,
                    delivery_message TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """))
            await db.commit()

    async def start_session(self, meeting_id: int, tasks: list[dict]) -> dict:
        """Initialize a verification session for a list of tasks."""
        # Group tasks by assigned_to (client name)
        groups: dict[str, list[dict]] = {}
        unassigned = []
        for t in tasks:
            who = (t.get("assigned_to") or "").strip()
            if not who:
                unassigned.append(t)
            else:
                groups.setdefault(who, []).append(t)

        # Build flat queue: client by client, task by task
        queue: list[dict] = []
        for client, ts in groups.items():
            for t in ts:
                queue.append({**t, "_client": client})
        # Add unassigned at the end
        for t in unassigned:
            queue.append({**t, "_client": "Unassigned"})

        self.active_session = {
            "meeting_id": meeting_id,
            "queue": queue,
            "current_index": 0,
            "state": "awaiting_approval",
            "stats": {"approved": 0, "rejected": 0, "sent": 0, "total": len(queue)},
            "started_at": datetime.now().isoformat(),
        }

        log.info("verification_session_started", meeting_id=meeting_id,
                 tasks=len(queue), clients=len(groups))
        return self._intro_message(groups)

    def _intro_message(self, groups: dict) -> dict:
        """First message — overview of tasks per client."""
        lines = [f"📋 **Meeting Verification Mode**\n"]
        lines.append(f"Total tasks: **{sum(len(v) for v in groups.values())}** · "
                     f"Clients: **{len(groups)}**\n")
        for client, ts in groups.items():
            lines.append(f"━━━ **{client}** ({len(ts)} tasks) ━━━")
            for i, t in enumerate(ts, 1):
                lines.append(f"  {i}. {t.get('title', 'Untitled')}")
            lines.append("")
        lines.append("Ek-ek task verify karta hun — Approve / Reject buttons dabao.")
        return {"intro": "\n".join(lines)}

    def current_task(self) -> dict | None:
        """Get the current task being verified."""
        s = self.active_session
        if not s or s["current_index"] >= len(s["queue"]):
            return None
        return s["queue"][s["current_index"]]

    def get_status(self) -> dict | None:
        """Return current verification status (for UI sync)."""
        s = self.active_session
        if not s:
            return None
        task = self.current_task()
        return {
            "active": True,
            "state": s["state"],
            "current_index": s["current_index"],
            "total": s["stats"]["total"],
            "stats": s["stats"],
            "task": task,
        }

    def task_prompt(self) -> dict:
        """Build the prompt message for the current task."""
        s = self.active_session
        if not s:
            return {"reply": "Koi active verification nahi", "verification": None}

        task = self.current_task()
        if task is None:
            return self._finish()

        idx = s["current_index"] + 1
        total = s["stats"]["total"]
        client = task.get("_client", "Unassigned")
        title = task.get("title", "Untitled")
        priority = task.get("priority", "medium")
        desc = task.get("description") or ""

        if s["state"] == "awaiting_approval":
            text = (
                f"📌 **Task {idx}/{total}** — *{client}*\n\n"
                f"**{title}**\n"
                f"{('  ' + desc) if desc else ''}\n"
                f"Priority: `{priority.upper()}`\n\n"
                f"Approve karein?"
            )
            return {
                "reply": text,
                "verification": {
                    "state": "awaiting_approval",
                    "task_index": s["current_index"],
                    "total": total,
                    "buttons": [
                        {"id": "approve", "label": "✅ Approve", "color": "green"},
                        {"id": "reject", "label": "❌ Reject", "color": "red"},
                    ],
                },
            }
        elif s["state"] == "awaiting_platform":
            text = (
                f"✅ Approve ho gaya: **{title}**\n\n"
                f"Kis platform pe bhejun **{client}** ko?"
            )
            return {
                "reply": text,
                "verification": {
                    "state": "awaiting_platform",
                    "task_index": s["current_index"],
                    "total": total,
                    "buttons": [
                        {"id": "whatsapp", "label": "📱 WhatsApp", "color": "green"},
                        {"id": "teams", "label": "💬 Teams", "color": "purple"},
                        {"id": "email", "label": "📧 Email", "color": "blue"},
                    ],
                },
            }

        return self._finish()

    async def reject_current(self) -> dict:
        """Reject current task and move to next."""
        s = self.active_session
        if not s:
            return {"reply": "Koi active verification nahi", "verification": None}

        task = self.current_task()
        if task is None:
            return self._finish()

        s["stats"]["rejected"] += 1
        await self._log_decision(task, "rejected", platform=None)

        s["current_index"] += 1
        s["state"] = "awaiting_approval"

        if s["current_index"] >= len(s["queue"]):
            return self._finish()

        next_prompt = self.task_prompt()
        return {
            "reply": f"❌ Task rejected.\n\n{next_prompt['reply']}",
            "verification": next_prompt["verification"],
        }

    async def approve_current(self) -> dict:
        """Approve current task — move to platform selection."""
        s = self.active_session
        if not s:
            return {"reply": "Koi active verification nahi", "verification": None}

        task = self.current_task()
        if task is None:
            return self._finish()

        s["stats"]["approved"] += 1
        s["state"] = "awaiting_platform"

        return self.task_prompt()

    async def send_via_platform(self, platform: str, config) -> dict:
        """Send the approved task message via the chosen platform."""
        s = self.active_session
        if not s:
            return {"reply": "Koi active verification nahi", "verification": None}

        task = self.current_task()
        if task is None:
            return self._finish()

        client = task.get("_client", "")
        title = task.get("title", "")
        desc = task.get("description") or ""
        priority = task.get("priority", "medium")

        # Build the message
        message = self._build_message(title, desc, priority)

        send_status = "failed"
        send_msg = ""

        try:
            if platform in ("whatsapp", "teams"):
                # Per-app scrapers removed in the universal-engine repivot;
                # sends will route through the new UIA-based engine.
                send_msg = (
                    f"{platform.title()} send naye universal engine mein rebuild ho raha hai — "
                    "abhi yeh message manually bhejna hoga."
                )

            elif platform == "email":
                if "@" not in client:
                    send_msg = f"'{client}' ka email address nahi mila — Clients page mein add karo."
                else:
                    from app.services.laptop_control.email_sender import EmailSender
                    import asyncio
                    ok, m = await asyncio.to_thread(
                        EmailSender.send, client, f"Task: {title}", message
                    )
                    send_status = "sent" if ok else "failed"
                    send_msg = m

        except Exception as e:
            send_msg = f"Send failed: {e}"

        # Log
        await self._log_decision(task, "approved", platform=platform,
                                 delivery_status=send_status, delivery_message=send_msg)

        if send_status == "sent":
            s["stats"]["sent"] += 1
            reply_prefix = f"✅ {send_msg}"
        else:
            reply_prefix = f"⚠️ {send_msg}"

        # Move to next
        s["current_index"] += 1
        s["state"] = "awaiting_approval"

        if s["current_index"] >= len(s["queue"]):
            finish = self._finish()
            return {
                "reply": f"{reply_prefix}\n\n{finish['reply']}",
                "verification": None,
            }

        next_prompt = self.task_prompt()
        return {
            "reply": f"{reply_prefix}\n\n{next_prompt['reply']}",
            "verification": next_prompt["verification"],
        }

    def _build_message(self, title: str, desc: str, priority: str) -> str:
        """Build the message text for the team member."""
        lines = []
        lines.append(f"📌 *Task: {title}*")
        if desc and desc.strip():
            lines.append(f"{desc.strip()}")
        if priority and priority.lower() != "medium":
            lines.append(f"Priority: {priority.upper()}")
        lines.append("")
        lines.append("— via Jarvis (Boss)")
        return "\n".join(lines)

    def _finish(self) -> dict:
        """Verification session done."""
        s = self.active_session
        if not s:
            return {"reply": "Koi session active nahi.", "verification": None}

        stats = s["stats"]
        text = (
            f"🎉 **Verification Complete**\n\n"
            f"Total: {stats['total']}\n"
            f"✅ Approved: {stats['approved']}\n"
            f"❌ Rejected: {stats['rejected']}\n"
            f"📤 Sent: {stats['sent']}\n"
        )

        log.info("verification_session_complete", stats=stats)
        self.active_session = None

        return {"reply": text, "verification": None}

    async def cancel(self) -> dict:
        """Cancel the active session."""
        if self.active_session:
            log.info("verification_cancelled")
            self.active_session = None
        return {"reply": "Verification cancel ho gaya.", "verification": None}

    async def _log_decision(
        self,
        task: dict,
        decision: str,
        platform: str | None = None,
        delivery_status: str = "",
        delivery_message: str = "",
    ) -> None:
        async with async_session() as db:
            await db.execute(
                text("""
                    INSERT INTO verification_log
                    (meeting_id, task_id, assigned_to, title, decision, platform,
                     delivery_status, delivery_message)
                    VALUES (:mid, :tid, :who, :title, :decision, :platform, :status, :msg)
                """),
                {
                    "mid": self.active_session.get("meeting_id") if self.active_session else None,
                    "tid": task.get("id"),
                    "who": task.get("_client", ""),
                    "title": task.get("title", ""),
                    "decision": decision,
                    "platform": platform or "",
                    "status": delivery_status or "",
                    "msg": delivery_message[:300] if delivery_message else "",
                },
            )
            await db.commit()


# Eagerly create the singleton at import time — eliminates the check-then-set
# race that two concurrent requests could previously hit in VerificationService.get().
VerificationService._instance = VerificationService()
