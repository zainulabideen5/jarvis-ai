"""Daily AI Report — generates daily summary and sends to chat + optional email."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from app.core.llm import LLMClient
from sqlalchemy import text

from app.core.config import ServerConfig
from app.core.database import async_session
from app.core.logging import get_logger

log = get_logger(__name__)

REPORT_PROMPT = """You are JARVIS AI assistant. Generate a daily work summary report based on the following data.

Write in a mix of Urdu/Hindi/English (Roman Urdu), keep it concise and actionable.

Format:
1. Yesterday's Summary (kya kya hua)
2. Key Conversations (important baatein)
3. Tasks Status (kitne complete, kitne pending)
4. Upcoming Events (aaj/kal kya hai)
5. Action Items (kya karna chahiye aaj)

Data:
__DATA__

Keep the report under 300 words. Be specific — use names, numbers, details."""


class DailyReportService:
    """Generates daily AI summary reports."""

    def __init__(self, config: ServerConfig):
        self._config = config
        self._client = None
        self._last_report_date: str | None = None

    def _get_client(self) -> LLMClient:
        if self._client is None:
            self._client = LLMClient(self._config)
        return self._client

    async def should_generate(self) -> bool:
        """Check if report should be generated (once per day, morning)."""
        # "Morning" is local-time concept for the user
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")

        # Generate between 8 AM - 9 AM, once per day
        if now.hour == 8 and self._last_report_date != today:
            return True

        # Also allow manual trigger
        return False

    async def generate_report(self) -> str:
        """Generate daily report from yesterday's data.

        DB timestamps are UTC (SQLite CURRENT_TIMESTAMP) so we use UTC dates to
        match. Mixing local-time strings with UTC `created_at` causes the report
        to miss conversations from the user's early-morning hours.
        """
        today = datetime.utcnow().strftime("%Y-%m-%d")
        yesterday = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")

        async with async_session() as db:
            # Yesterday's transcriptions
            result = await db.execute(
                text("""
                    SELECT text, language FROM transcriptions
                    WHERE date(created_at) = :yesterday AND length(text) > 10
                    ORDER BY created_at
                    LIMIT 20
                """),
                {"yesterday": yesterday},
            )
            transcriptions = result.fetchall()
            trans_text = "\n".join(f"- [{r[1]}] {r[0][:200]}" for r in transcriptions) or "No conversations yesterday."

            # Task stats
            result = await db.execute(
                text("""
                    SELECT status, COUNT(*) FROM tasks
                    GROUP BY status
                """)
            )
            task_stats = {r[0]: r[1] for r in result.fetchall()}

            # Yesterday's new tasks
            result = await db.execute(
                text("""
                    SELECT title, priority, status FROM tasks
                    WHERE date(created_at) = :yesterday
                    ORDER BY created_at
                """),
                {"yesterday": yesterday},
            )
            new_tasks = result.fetchall()
            tasks_text = "\n".join(f"- {r[0]} [{r[1]}, {r[2]}]" for r in new_tasks) or "No new tasks yesterday."

            # Today's calendar events
            result = await db.execute(
                text("""
                    SELECT title, event_time, attendees FROM calendar_events
                    WHERE event_date = :today
                    ORDER BY event_time
                """),
                {"today": today},
            )
            events = result.fetchall()
            events_text = "\n".join(
                f"- {r[0]} at {r[1] or 'TBD'} (attendees: {r[2] or 'N/A'})"
                for r in events
            ) or "No events today."

            # Client count
            result = await db.execute(text("SELECT COUNT(*) FROM clients"))
            client_count = result.scalar()

        data = f"""
Date: {today} (report for {yesterday})

Conversations ({len(transcriptions)}):
{trans_text}

New Tasks Yesterday:
{tasks_text}

Task Summary: {json.dumps(task_stats)}

Today's Calendar:
{events_text}

Total Clients: {client_count}
"""

        # Generate report with AI
        client = self._get_client()
        prompt = REPORT_PROMPT.replace("__DATA__", data)

        try:
            response = client.chat.completions.create(
                model=self._config.groq_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=800,
            )
            report = response.choices[0].message.content.strip()
        except Exception as e:
            report = f"Report generation failed: {e}\n\nRaw Data:\n{data}"

        # Save to chat
        await self._save_to_chat(f"📊 Daily Report — {today}\n\n{report}")

        self._last_report_date = today
        log.info("daily_report_generated", date=today)

        return report

    async def _save_to_chat(self, message: str) -> None:
        async with async_session() as db:
            await db.execute(
                text("INSERT INTO chat_messages (role, content, actions) VALUES (:role, :content, :actions)"),
                {"role": "assistant", "content": message, "actions": "[]"},
            )
            await db.commit()
