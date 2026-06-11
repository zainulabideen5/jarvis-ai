"""Contact routing memory — learns how to reach each person.

The first time the boss assigns a task to someone, JARVIS asks which channel
to use (WhatsApp / Teams / Email). The answer is saved here so next time it
reaches the same person automatically without asking. This is the "seekhta
jaata hai" (learns over time) layer for task assignment.
"""

from __future__ import annotations

from sqlalchemy import text

from app.core.database import async_session
from app.core.logging import get_logger

log = get_logger(__name__)

VALID_CHANNELS = ("whatsapp", "teams", "email")


def _norm(name: str) -> str:
    return (name or "").strip().lower()


class ContactRouting:
    """Per-contact preferred channel + identifier, learned from past sends."""

    @staticmethod
    async def ensure_table() -> None:
        async with async_session() as db:
            await db.execute(text("""
                CREATE TABLE IF NOT EXISTS contact_routing (
                    name_key   TEXT PRIMARY KEY,
                    display_name TEXT,
                    channel    TEXT,
                    identifier TEXT,
                    use_count  INTEGER DEFAULT 1,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """))
            await db.commit()

    @staticmethod
    async def get(name: str) -> dict | None:
        """Return the learned routing for a contact, or None if not learned."""
        key = _norm(name)
        if not key:
            return None
        async with async_session() as db:
            row = (await db.execute(
                text("SELECT display_name, channel, identifier, use_count "
                     "FROM contact_routing WHERE name_key = :k"),
                {"k": key},
            )).first()
        if not row:
            return None
        return {
            "display_name": row[0],
            "channel": row[1],
            "identifier": row[2],
            "use_count": row[3],
        }

    @staticmethod
    async def remember(name: str, channel: str, identifier: str = "") -> None:
        """Save / update how to reach a contact. Bumps use_count on repeat."""
        key = _norm(name)
        if not key or channel not in VALID_CHANNELS:
            return
        async with async_session() as db:
            await db.execute(
                text("""
                    INSERT INTO contact_routing (name_key, display_name, channel, identifier, use_count, updated_at)
                    VALUES (:k, :d, :c, :i, 1, CURRENT_TIMESTAMP)
                    ON CONFLICT(name_key) DO UPDATE SET
                        channel = :c,
                        identifier = CASE WHEN :i != '' THEN :i ELSE contact_routing.identifier END,
                        use_count = contact_routing.use_count + 1,
                        updated_at = CURRENT_TIMESTAMP
                """),
                {"k": key, "d": (name or "").strip(), "c": channel, "i": identifier or ""},
            )
            await db.commit()
        log.info("contact_routing_learned", contact=key, channel=channel)

    @staticmethod
    async def list_all() -> list[dict]:
        async with async_session() as db:
            rows = (await db.execute(
                text("SELECT display_name, channel, identifier, use_count "
                     "FROM contact_routing ORDER BY use_count DESC")
            )).all()
        return [
            {"display_name": r[0], "channel": r[1], "identifier": r[2], "use_count": r[3]}
            for r in rows
        ]

    @staticmethod
    async def forget(name: str) -> bool:
        key = _norm(name)
        async with async_session() as db:
            res = await db.execute(
                text("DELETE FROM contact_routing WHERE name_key = :k"), {"k": key}
            )
            await db.commit()
        return bool(res.rowcount)
