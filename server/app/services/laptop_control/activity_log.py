"""Activity logger — records every Jarvis laptop action."""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import text

from app.core.database import async_session
from app.core.logging import get_logger

log = get_logger(__name__)


class ActivityLogger:
    """Logs all laptop control actions to database."""

    @staticmethod
    async def ensure_table() -> None:
        """Create activity_log table if not exists."""
        async with async_session() as db:
            await db.execute(text("""
                CREATE TABLE IF NOT EXISTS laptop_activity (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    params TEXT,
                    result TEXT,
                    status TEXT,
                    error TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """))
            await db.commit()

    @staticmethod
    async def log_action(
        action: str, params: dict, result: str = "", status: str = "success", error: str = ""
    ) -> int:
        """Log an action. Returns log id."""
        async with async_session() as db:
            result_row = await db.execute(
                text("""
                    INSERT INTO laptop_activity (action, params, result, status, error)
                    VALUES (:action, :params, :result, :status, :error)
                """),
                {
                    "action": action,
                    "params": json.dumps(params, ensure_ascii=False),
                    "result": result[:500] if result else "",
                    "status": status,
                    "error": error[:500] if error else "",
                },
            )
            await db.commit()
            row = await db.execute(text("SELECT last_insert_rowid()"))
            log_id = row.scalar()

        log.info("activity_logged", action=action, status=status, id=log_id)
        return log_id

    @staticmethod
    async def recent_send_summary(limit: int = 5) -> list[dict]:
        """Return the last few successful messaging actions, normalized for context-aware parsing.

        Each entry: {"platform": "whatsapp"|"teams"|"email", "recipient": "...", "is_phone": bool, "message_preview": "..."}
        Used by IntentDetector to resolve pronouns and infer platforms across turns.
        """
        async with async_session() as db:
            result = await db.execute(
                text("""
                    SELECT action, params, created_at FROM laptop_activity
                    WHERE status = 'success'
                      AND action IN ('send_whatsapp_message', 'send_teams_message', 'send_email')
                    ORDER BY id DESC LIMIT :limit
                """),
                {"limit": limit},
            )
            rows = result.fetchall()

        platform_map = {
            "send_whatsapp_message": "whatsapp",
            "send_teams_message": "teams",
            "send_email": "email",
        }
        out: list[dict] = []
        for action, params_json, created_at in rows:
            try:
                params = json.loads(params_json) if params_json else {}
            except json.JSONDecodeError:
                params = {}
            platform = platform_map.get(action, "unknown")
            recipient = (params.get("recipient") or params.get("to") or "").strip()
            message = (params.get("message") or params.get("body") or "")[:80]
            digits_only = recipient.replace("+", "").replace(" ", "").replace("-", "")
            is_phone = bool(recipient) and digits_only.isdigit() and len(digits_only) >= 7
            out.append({
                "platform": platform,
                "recipient": recipient,
                "is_phone": is_phone,
                "message_preview": message,
                "created_at": str(created_at) if created_at else "",
            })
        return out

    @staticmethod
    async def list_recent(limit: int = 50) -> list[dict]:
        """Get recent activity."""
        async with async_session() as db:
            result = await db.execute(
                text("""
                    SELECT id, action, params, result, status, error, created_at
                    FROM laptop_activity ORDER BY id DESC LIMIT :limit
                """),
                {"limit": limit},
            )
            rows = result.fetchall()
        return [
            {
                "id": r[0], "action": r[1],
                "params": json.loads(r[2]) if r[2] else {},
                "result": r[3], "status": r[4], "error": r[5],
                "created_at": r[6],
            }
            for r in rows
        ]
