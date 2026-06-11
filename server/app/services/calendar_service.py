"""Smart Calendar — detects dates/meetings from transcriptions, creates calendar events."""

from __future__ import annotations

import json
from datetime import datetime

from app.core.llm import LLMClient
from sqlalchemy import text

from app.core.config import ServerConfig
from app.core.database import async_session
from app.core.logging import get_logger

log = get_logger(__name__)

DATE_EXTRACT_PROMPT = """You are a date and meeting extraction AI. Analyze the transcript and extract any meetings, appointments, deadlines, or time-related commitments.

For each event found, return a JSON object:
- "title": Short event title (max 80 chars)
- "date": Date in YYYY-MM-DD format (use today's date context: __TODAY__)
- "time": Time in HH:MM format (24hr), or null if not mentioned
- "duration_minutes": Estimated duration (default 60)
- "attendees": List of names mentioned, or empty list
- "location": Location if mentioned, or null
- "notes": Any extra details

Date interpretation rules:
- "kal" / "tomorrow" = next day from __TODAY__
- "parson" / "day after tomorrow" = +2 days
- "agle hafte" / "next week" = +7 days
- "Friday ko" = next upcoming Friday
- "3 baje" = 15:00, "subah 10" = 10:00, "raat 9" = 21:00

If NO events/dates found, return empty array [].
Return ONLY valid JSON array.

Transcript:
{transcript}"""


class CalendarService:
    """Extracts date/meeting info from transcriptions and manages calendar events."""

    def __init__(self, config: ServerConfig):
        self._config = config
        self._client = None

    def _get_client(self) -> LLMClient:
        if self._client is None:
            self._client = LLMClient(self._config)
        return self._client

    def extract_events(self, transcript: str) -> list[dict]:
        """Extract calendar events from transcript text."""
        if not transcript or len(transcript.strip()) < 15:
            return []

        client = self._get_client()
        today = datetime.now().strftime("%Y-%m-%d")
        prompt = DATE_EXTRACT_PROMPT.replace("__TODAY__", today).replace("{transcript}", transcript)

        try:
            response = client.chat.completions.create(
                model=self._config.groq_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=1000,
            )

            content = response.choices[0].message.content.strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[1].rsplit("```", 1)[0]

            events = json.loads(content)
            if not isinstance(events, list):
                events = [events]

            log.info("calendar_events_extracted", count=len(events))
            return events

        except (json.JSONDecodeError, Exception) as e:
            log.debug("calendar_extraction_failed", error=str(e))
            return []

    async def save_event(self, event: dict, chunk_id: str | None = None) -> int:
        """Save a calendar event to database."""
        async with async_session() as db:
            await db.execute(
                text("""
                    INSERT INTO calendar_events
                    (title, event_date, event_time, duration_minutes, attendees, location, notes, chunk_id, status)
                    VALUES (:title, :date, :time, :duration, :attendees, :location, :notes, :chunk_id, 'pending')
                """),
                {
                    "title": event.get("title", "Untitled Event"),
                    "date": event.get("date"),
                    "time": event.get("time"),
                    "duration": event.get("duration_minutes", 60),
                    "attendees": json.dumps(event.get("attendees", [])),
                    "location": event.get("location"),
                    "notes": event.get("notes"),
                    "chunk_id": chunk_id,
                },
            )
            await db.commit()

            # Get last inserted id
            result = await db.execute(text("SELECT last_insert_rowid()"))
            event_id = result.scalar()

        log.info("calendar_event_saved", title=event.get("title"), date=event.get("date"))

        # Also notify in chat
        date_str = event.get("date", "?")
        time_str = event.get("time", "")
        title = event.get("title", "")
        attendees = ", ".join(event.get("attendees", [])) or "N/A"

        chat_msg = f"📅 Naya event detect hua:\n  {title}\n  Date: {date_str} {time_str}\n  Attendees: {attendees}\n\nCalendar page pe confirm karo!"
        await self._save_chat(chat_msg)

        return event_id

    async def _save_chat(self, message: str) -> None:
        async with async_session() as db:
            await db.execute(
                text("INSERT INTO chat_messages (role, content, actions) VALUES (:role, :content, :actions)"),
                {"role": "assistant", "content": message, "actions": "[]"},
            )
            await db.commit()
