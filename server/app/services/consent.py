"""Consent Service — manages user's full-laptop-access permission."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import text

from app.core.database import async_session
from app.core.logging import get_logger

log = get_logger(__name__)


class ConsentService:
    """Tracks whether user has granted full laptop access."""

    @staticmethod
    async def ensure_table() -> None:
        async with async_session() as db:
            await db.execute(text("""
                CREATE TABLE IF NOT EXISTS consent_state (
                    id INTEGER PRIMARY KEY,
                    granted INTEGER DEFAULT 0,
                    granted_at TIMESTAMP,
                    revoked_at TIMESTAMP,
                    user_agent TEXT,
                    notes TEXT
                )
            """))
            # Ensure single row exists
            row = await db.execute(text("SELECT COUNT(*) FROM consent_state"))
            if (row.scalar() or 0) == 0:
                await db.execute(text(
                    "INSERT INTO consent_state (id, granted) VALUES (1, 0)"
                ))
            await db.commit()

    @staticmethod
    async def is_granted() -> bool:
        async with async_session() as db:
            result = await db.execute(text("SELECT granted FROM consent_state WHERE id = 1"))
            row = result.fetchone()
            return bool(row and row[0])

    @staticmethod
    async def get_state() -> dict:
        async with async_session() as db:
            result = await db.execute(text(
                "SELECT granted, granted_at, revoked_at FROM consent_state WHERE id = 1"
            ))
            row = result.fetchone()
            if not row:
                return {"granted": False, "granted_at": None, "revoked_at": None}
            return {
                "granted": bool(row[0]),
                "granted_at": row[1],
                "revoked_at": row[2],
            }

    @staticmethod
    async def grant(user_agent: str = "") -> dict:
        now = datetime.utcnow().isoformat()
        async with async_session() as db:
            await db.execute(
                text("""
                    UPDATE consent_state SET
                        granted = 1,
                        granted_at = :now,
                        revoked_at = NULL,
                        user_agent = :ua
                    WHERE id = 1
                """),
                {"now": now, "ua": user_agent[:300]},
            )
            await db.commit()
        log.info("consent_granted")
        return await ConsentService.get_state()

    @staticmethod
    async def revoke() -> dict:
        now = datetime.utcnow().isoformat()
        async with async_session() as db:
            await db.execute(
                text("""
                    UPDATE consent_state SET
                        granted = 0,
                        revoked_at = :now
                    WHERE id = 1
                """),
                {"now": now},
            )
            await db.commit()
        log.info("consent_revoked")
        return await ConsentService.get_state()
