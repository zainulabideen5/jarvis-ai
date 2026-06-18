"""Per-user persistent memory — JARVIS user ki batayi hui baatein YAAD rakhta hai.

Self-adaptive: jis bhi laptop pe software chale, wahan ke user ki baatein local
SQLite (jarvis.db) mein save rehti hain — restart/band hone ke baad bhi. Koi
cloud, koi hardcode nahi. Table pehli dafa khud ban jata hai (no migration).
"""

from __future__ import annotations

from sqlalchemy import text as sa_text

from app.core.database import async_session
from app.core.logging import get_logger

log = get_logger(__name__)

_INIT_DONE = False


async def _ensure_table() -> None:
    """user_memory table pehli dafa khud bana do (har machine pe self-setup)."""
    global _INIT_DONE
    if _INIT_DONE:
        return
    async with async_session() as db:
        await db.execute(sa_text(
            "CREATE TABLE IF NOT EXISTS user_memory ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " content TEXT NOT NULL,"
            " created_at TEXT DEFAULT (datetime('now')))"
        ))
        await db.commit()
    _INIT_DONE = True


class UserMemory:
    """User ki yaad-dasht — remember / recall / forget. General, per-machine."""

    @staticmethod
    async def remember(content: str) -> dict:
        """Ek fact yaad rakho. Duplicate ho to dobara save nahi karta."""
        content = (content or "").strip()
        if not content:
            return {"ok": False, "msg": "khali baat save nahi hoti"}
        await _ensure_table()
        async with async_session() as db:
            existing = await db.execute(sa_text(
                "SELECT id FROM user_memory WHERE lower(content)=lower(:c) LIMIT 1"),
                {"c": content})
            if existing.fetchone():
                return {"ok": True, "dup": True, "msg": "yeh pehle se yaad hai"}
            await db.execute(sa_text(
                "INSERT INTO user_memory (content) VALUES (:c)"), {"c": content})
            await db.commit()
        log.info("user_memory_saved", content=content[:80])
        return {"ok": True, "msg": "yaad rakh liya"}

    @staticmethod
    async def all(limit: int = 200) -> list[str]:
        """Saari yaad-dasht (nayi pehle). Context mein inject karne ke liye."""
        await _ensure_table()
        async with async_session() as db:
            r = await db.execute(sa_text(
                "SELECT content FROM user_memory ORDER BY id DESC LIMIT :l"),
                {"l": int(limit)})
            return [row[0] for row in r.fetchall()]

    @staticmethod
    async def forget(match: str) -> dict:
        """Jis baat mein `match` aaye usay bhula do (substring, case-insensitive)."""
        match = (match or "").strip()
        if not match:
            return {"ok": False, "msg": "kya bhulun? thoda batao"}
        await _ensure_table()
        async with async_session() as db:
            r = await db.execute(sa_text(
                "DELETE FROM user_memory WHERE lower(content) LIKE lower(:m)"),
                {"m": f"%{match}%"})
            await db.commit()
            n = r.rowcount or 0
        return {"ok": n > 0,
                "count": n,
                "msg": f"{n} baat bhula di" if n else "aisi koi baat yaad nahi thi"}
