"""Memory Service — AI learns from chat conversations.

User can teach the AI through normal chat:
    "Mujhe Boss bolna"           -> saves preference
    "Mera template folder Downloads mein hai"  -> saves location
    "Hamesha urgent tasks dikhao pehle"        -> saves behavior
    "Yaad rakh, Ahmed mera designer hai"       -> saves context
    "Kabhi delete karne se pehle confirm kar"  -> saves rule

Memories get auto-injected into every future AI reply so AI follows them.
"""

from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy import text

from app.core.database import async_session
from app.core.logging import get_logger

log = get_logger(__name__)

# Trigger phrases that signal user is teaching/instructing
TEACH_TRIGGERS_HI = [
    "yaad rakh", "yaad rakhna", "yaad rakhe", "remember kar",
    "hamesha", "always",
    "kabhi mat", "kabhi nahi", "never",
    "agla se", "aage se", "next time",
    "note kar", "save kar",
    "tu hai", "tu mera", "mera ", "meri ",
    "mujhe bol", "mujhe kaho",
]

CATEGORY_HINTS = {
    "preference": ["mujhe", "main pasand", "i prefer", "i like"],
    "person": ["mera designer", "mera client", "mera team", "ahmed hai", "hamza hai", "sara hai"],
    "rule": ["hamesha", "always", "kabhi", "never", "agla se", "next time"],
    "location": ["folder", "path", "directory", "hai mein", "hai mera"],
    "fact": ["yaad rakh", "remember", "note kar"],
}


class MemoryService:
    """Saves & retrieves AI memory entries from DB."""

    @staticmethod
    async def ensure_table() -> None:
        async with async_session() as db:
            await db.execute(text("""
                CREATE TABLE IF NOT EXISTS ai_memory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    category TEXT,
                    content TEXT NOT NULL,
                    source_message TEXT,
                    importance INTEGER DEFAULT 1,
                    is_active INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """))
            await db.commit()

    @staticmethod
    def detect_teaching(user_message: str) -> bool:
        """Quick check: does message look like the user is teaching/instructing?"""
        if not user_message:
            return False
        lower = user_message.lower().strip()

        # Skip questions
        QUESTION_WORDS = {
            "kya", "kaun", "kaisa", "kaise", "kahan", "kab", "kyun", "kyu",
            "what", "who", "how", "where", "when", "why",
        }
        if lower.endswith("?") or lower.startswith("?"):
            return False
        words = lower.split()
        if words and words[0] in QUESTION_WORDS:
            return False
        # If "kaun"/"kya" appears prominently → likely question, skip
        if " kaun " in f" {lower} " or " kya " in f" {lower} " or " kaisa " in f" {lower} ":
            return False

        # Strong teaching triggers (must contain at least one)
        return any(trigger in lower for trigger in TEACH_TRIGGERS_HI)

    @staticmethod
    def categorize(text: str) -> str:
        lower = text.lower()
        for cat, words in CATEGORY_HINTS.items():
            if any(w in lower for w in words):
                return cat
        return "general"

    @staticmethod
    async def save(content: str, category: str = "general",
                   source_message: str = "", importance: int = 1) -> int:
        """Save a memory entry. Returns id."""
        if not content or not content.strip():
            return 0
        content = content.strip()
        async with async_session() as db:
            # Avoid exact duplicates
            existing = await db.execute(
                text("SELECT id FROM ai_memory WHERE content = :c AND is_active = 1"),
                {"c": content},
            )
            if existing.fetchone():
                return 0

            await db.execute(
                text("""
                    INSERT INTO ai_memory (category, content, source_message, importance)
                    VALUES (:cat, :content, :src, :imp)
                """),
                {"cat": category, "content": content, "src": source_message[:300], "imp": importance},
            )
            await db.commit()
            row = await db.execute(text("SELECT last_insert_rowid()"))
            mem_id = row.scalar()
        log.info("memory_saved", id=mem_id, category=category, content=content[:80])
        return mem_id

    @staticmethod
    async def get_active(limit: int = 30) -> list[dict]:
        """Get all active memories ordered by importance + recency."""
        async with async_session() as db:
            result = await db.execute(
                text("""
                    SELECT id, category, content, importance, created_at
                    FROM ai_memory
                    WHERE is_active = 1
                    ORDER BY importance DESC, id DESC
                    LIMIT :limit
                """),
                {"limit": limit},
            )
            rows = result.fetchall()
        return [
            {"id": r[0], "category": r[1], "content": r[2],
             "importance": r[3], "created_at": r[4]}
            for r in rows
        ]

    @staticmethod
    async def list_all() -> list[dict]:
        async with async_session() as db:
            result = await db.execute(
                text("""
                    SELECT id, category, content, importance, is_active, created_at
                    FROM ai_memory ORDER BY id DESC
                """)
            )
            rows = result.fetchall()
        return [
            {"id": r[0], "category": r[1], "content": r[2],
             "importance": r[3], "is_active": bool(r[4]), "created_at": r[5]}
            for r in rows
        ]

    @staticmethod
    async def deactivate(mem_id: int) -> bool:
        async with async_session() as db:
            await db.execute(
                text("UPDATE ai_memory SET is_active = 0 WHERE id = :id"),
                {"id": mem_id},
            )
            await db.commit()
        return True

    @staticmethod
    async def forget_all() -> int:
        async with async_session() as db:
            result = await db.execute(
                text("UPDATE ai_memory SET is_active = 0 WHERE is_active = 1")
            )
            await db.commit()
            return result.rowcount

    @staticmethod
    async def build_context_block() -> str:
        """Build a memory block to inject into the AI system prompt."""
        memories = await MemoryService.get_active(limit=30)
        if not memories:
            return ""

        # Group by category
        groups: dict[str, list[str]] = {}
        for m in memories:
            cat = m["category"] or "general"
            groups.setdefault(cat, []).append(m["content"])

        order = ["rule", "preference", "person", "location", "fact", "general"]
        lines = ["## MEMORY (Boss ne train kiya hai — HAMESHA follow karo):"]
        for cat in order:
            if cat in groups:
                lines.append(f"\n### {cat.title()}:")
                for content in groups[cat]:
                    lines.append(f"  - {content}")
        for cat, items in groups.items():
            if cat not in order:
                lines.append(f"\n### {cat.title()}:")
                for content in items:
                    lines.append(f"  - {content}")

        return "\n".join(lines)

    @staticmethod
    async def extract_and_save_from_message(user_message: str) -> dict | None:
        """Use LLM to extract teaching content from a user message and save it.
        Returns the saved memory or None.
        """
        if not user_message or len(user_message) < 5:
            return None

        if not MemoryService.detect_teaching(user_message):
            return None

        # Heuristic extraction — get the meaningful part
        msg = user_message.strip()

        # Strip common prefixes
        for prefix in ("yaad rakh ke ", "yaad rakhna ", "yaad rakh ", "remember kar ke ",
                       "remember ", "note kar ke ", "note kar ", "save kar "):
            if msg.lower().startswith(prefix):
                msg = msg[len(prefix):].strip()
                break

        # Clean trailing punctuation
        content = msg.rstrip(".!? ").strip()
        if not content or len(content) < 3:
            return None

        category = MemoryService.categorize(content)
        importance = 2 if category in ("rule", "preference") else 1

        mem_id = await MemoryService.save(
            content=content,
            category=category,
            source_message=user_message,
            importance=importance,
        )
        if mem_id:
            return {"id": mem_id, "category": category, "content": content}
        return None
