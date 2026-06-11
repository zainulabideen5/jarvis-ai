"""Meeting Mode — Start/End meetings with auto summary generation."""

from __future__ import annotations

import json
from datetime import datetime

from app.core.llm import LLMClient
from sqlalchemy import text

from app.core.config import ServerConfig
from app.core.database import async_session
from app.core.logging import get_logger

log = get_logger(__name__)


def _utc_now() -> datetime:
    """Naive UTC datetime — matches SQLite's CURRENT_TIMESTAMP format so
    timestamp comparisons against audio_chunks/transcriptions/tasks (all
    UTC by SQLite default) work correctly. Using local time here breaks
    "chunks during meeting" queries when the user's TZ differs from UTC.
    """
    return datetime.utcnow()

SUMMARY_PROMPT = """Tu JARVIS hai. Meeting khatam hui hai. Neeche meeting ki transcriptions hain. Ek professional meeting summary bana Roman Urdu + English mein.

Format:
## Meeting Summary
- **Duration**: X minutes
- **Participants**: (jo speakers detect hue)

## Key Points:
1. ...
2. ...

## Action Items / Tasks:
1. [Task] — assigned to: [name]
2. [Task] — assigned to: [name]

## Decisions Made:
1. ...

## Follow-ups:
1. ...

Transcriptions:
__TRANSCRIPTIONS__

Summary chhota aur kaam ka rakh — max 300 words."""


class MeetingService:
    """Manages meeting lifecycle — start, track, end with summary."""

    def __init__(self, config: ServerConfig):
        self._config = config
        self._client = None

    def _get_client(self) -> LLMClient:
        if self._client is None:
            self._client = LLMClient(self._config)
        return self._client

    async def start_meeting(self, title: str = "") -> dict:
        """Start a new meeting — turns on listening."""
        now = _utc_now()
        title = title or f"Meeting - {now.strftime('%d %b %Y %I:%M %p')}"

        async with async_session() as db:
            # End any active meeting first
            await db.execute(
                text("UPDATE meetings SET status = 'ended', ended_at = :now WHERE status = 'active'"),
                {"now": now.isoformat()},
            )

            # Create new meeting
            await db.execute(
                text("INSERT INTO meetings (title, started_at, status) VALUES (:title, :started_at, 'active')"),
                {"title": title, "started_at": now.isoformat()},
            )

            # Turn on listening
            await db.execute(
                text("INSERT OR REPLACE INTO system_state (key, value) VALUES ('listening', 'on')")
            )

            await db.commit()

            result = await db.execute(text("SELECT last_insert_rowid()"))
            meeting_id = result.scalar()

            # Notify in chat
            await db.execute(
                text("INSERT INTO chat_messages (role, content, actions) VALUES ('assistant', :msg, '[]')"),
                {"msg": f"📋 Meeting started: \"{title}\"\nListening ON — sab sun raha hun. Meeting khatam ho to \"End Meeting\" dabana."},
            )
            await db.commit()

        log.info("meeting_started", title=title, meeting_id=meeting_id)
        return {"id": meeting_id, "title": title, "status": "active"}

    async def end_meeting(self) -> dict:
        """End active meeting — generate summary from transcriptions.

        Briefly waits for the audio processing pipeline (transcription + task
        extraction) to flush any in-flight chunks from the last few seconds
        of the meeting. Without this delay, end_meeting often reports
        Chunks/Tasks: 0 even when work was actually completed seconds later.
        """
        import asyncio
        await asyncio.sleep(8)

        now = _utc_now()

        async with async_session() as db:
            # Get active meeting
            result = await db.execute(
                text("SELECT id, title, started_at FROM meetings WHERE status = 'active' LIMIT 1")
            )
            meeting = result.fetchone()
            if not meeting:
                return {"error": "Koi active meeting nahi hai"}

            meeting_id, title, started_at = meeting[0], meeting[1], meeting[2]

            # Turn off listening
            await db.execute(
                text("INSERT OR REPLACE INTO system_state (key, value) VALUES ('listening', 'off')")
            )

            # Comparisons use SQLite's datetime() to parse both sides — this
            # was previously a pure-string compare which broke whenever the
            # meeting timestamp had "T"/microseconds (Python ISO format) but
            # audio_chunks.created_at had "space" + no fractional seconds
            # (SQLite CURRENT_TIMESTAMP). Lex order ' ' < 'T' caused real
            # chunks to be reported as "Chunks: 0".
            result = await db.execute(
                text("""
                    SELECT text, language FROM transcriptions
                    WHERE datetime(created_at) >= datetime(:start) AND length(text) > 5
                    ORDER BY created_at
                """),
                {"start": started_at},
            )
            transcriptions = result.fetchall()

            # Count chunks and tasks
            chunk_result = await db.execute(
                text("SELECT COUNT(*) FROM audio_chunks WHERE datetime(created_at) >= datetime(:start)"),
                {"start": started_at},
            )
            total_chunks = chunk_result.scalar() or 0

            task_result = await db.execute(
                text("SELECT COUNT(*) FROM tasks WHERE datetime(created_at) >= datetime(:start)"),
                {"start": started_at},
            )
            total_tasks = task_result.scalar() or 0

            # Calculate duration
            start_dt = datetime.fromisoformat(started_at)
            duration = int((now - start_dt).total_seconds() / 60)

            # Update meeting
            await db.execute(
                text("""
                    UPDATE meetings SET
                        status = 'ended',
                        ended_at = :ended,
                        duration_minutes = :duration,
                        total_chunks = :chunks,
                        total_tasks = :tasks
                    WHERE id = :id
                """),
                {"ended": now.isoformat(), "duration": duration, "chunks": total_chunks, "tasks": total_tasks, "id": meeting_id},
            )
            await db.commit()

        # Generate summary + extract tasks from FULL meeting transcript
        summary = "Koi transcription nahi hui meeting mein."
        meeting_tasks = []
        if transcriptions:
            trans_text = "\n".join(f"- [{t[1]}] {t[0]}" for t in transcriptions)
            full_text = " ".join(t[0] for t in transcriptions)

            # Generate summary
            summary = await self._generate_summary(trans_text, duration)

            # Extract tasks from FULL meeting transcript (not chunk by chunk)
            from app.services.task_extractor import TaskExtractor
            extractor = TaskExtractor(self._config)
            try:
                import asyncio
                meeting_tasks = await asyncio.to_thread(
                    extractor.extract_tasks, full_text, f"Meeting: {title}"
                )
                if meeting_tasks:
                    from app.models.task import Task
                    async with async_session() as db:
                        for task_data in meeting_tasks:
                            task = Task(
                                chunk_id=None,
                                title=task_data.get("title", "Untitled"),
                                description=task_data.get("description"),
                                assigned_to=task_data.get("assigned_to"),
                                priority=task_data.get("priority", "medium"),
                                source_text=full_text[:500],
                                action_type=task_data.get("action_type"),
                            )
                            db.add(task)
                        await db.commit()
                    log.info("meeting_tasks_extracted", count=len(meeting_tasks))
            except Exception as e:
                log.warning("meeting_task_extraction_failed", error=str(e))

            # Save summary
            async with async_session() as db:
                total_tasks_final = total_tasks + len(meeting_tasks)
                await db.execute(
                    text("UPDATE meetings SET summary = :summary, total_tasks = :tasks WHERE id = :id"),
                    {"summary": summary, "id": meeting_id, "tasks": total_tasks_final},
                )
                await db.commit()

        # Notify in chat — meeting summary
        total_all = total_tasks + len(meeting_tasks)
        async with async_session() as db:
            await db.execute(
                text("INSERT INTO chat_messages (role, content, actions) VALUES ('assistant', :msg, '[]')"),
                {"msg": f"📋 Meeting ended: \"{title}\"\nDuration: {duration} min | Chunks: {total_chunks} | Tasks: {total_all}\n\n{summary}"},
            )
            await db.commit()

        # Trigger interactive verification flow if tasks were extracted
        if meeting_tasks:
            try:
                from app.services.verification import VerificationService
                vs = VerificationService.get()
                intro = await vs.start_session(meeting_id, meeting_tasks)
                first_prompt = vs.task_prompt()

                # Save intro + first task prompt to chat
                async with async_session() as db:
                    await db.execute(
                        text("INSERT INTO chat_messages (role, content, actions) VALUES ('assistant', :msg, '[]')"),
                        {"msg": intro["intro"]},
                    )
                    await db.execute(
                        text("INSERT INTO chat_messages (role, content, actions) VALUES ('assistant', :msg, :actions)"),
                        {
                            "msg": first_prompt["reply"],
                            "actions": json.dumps([{"verification": first_prompt["verification"]}]),
                        },
                    )
                    await db.commit()
                log.info("verification_started", tasks=len(meeting_tasks))
            except Exception as e:
                log.warning("verification_trigger_failed", error=str(e))

        log.info("meeting_ended", title=title, duration=duration, tasks=total_all)
        return {
            "id": meeting_id,
            "title": title,
            "duration_minutes": duration,
            "total_chunks": total_chunks,
            "total_tasks": total_all,
            "summary": summary,
            "meeting_tasks": meeting_tasks,
            "verification_started": bool(meeting_tasks),
        }

    async def _generate_summary(self, transcriptions: str, duration: int) -> str:
        """Generate meeting summary using AI."""
        client = self._get_client()
        prompt = SUMMARY_PROMPT.replace("__TRANSCRIPTIONS__", transcriptions)

        try:
            response = client.chat.completions.create(
                model=self._config.groq_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.5,
                max_tokens=1000,
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            log.error("summary_generation_failed", error=str(e))
            return f"Summary generate nahi ho saki: {e}"

    async def get_active_meeting(self) -> dict | None:
        """Get currently active meeting."""
        async with async_session() as db:
            result = await db.execute(
                text("SELECT id, title, started_at FROM meetings WHERE status = 'active' LIMIT 1")
            )
            row = result.fetchone()
            if row:
                start_dt = datetime.fromisoformat(row[2])
                duration = int((_utc_now() - start_dt).total_seconds() / 60)
                return {"id": row[0], "title": row[1], "started_at": row[2], "duration_minutes": duration}
        return None

    async def list_meetings(self) -> list[dict]:
        """List all meetings, each with the tasks that were extracted during it.

        Tasks are matched to a meeting by timestamp window — the same approach
        used in end_meeting for counts. `datetime()` parses both sides so the
        ISO-vs-space format mismatch doesn't break the comparison.
        """
        async with async_session() as db:
            result = await db.execute(
                text(
                    "SELECT id, title, started_at, ended_at, duration_minutes, "
                    "total_chunks, total_tasks, summary, status "
                    "FROM meetings ORDER BY id DESC LIMIT 20"
                )
            )
            meetings = []
            for r in result.fetchall():
                started_at, ended_at = r[2], r[3]
                # Live duration in seconds. The stored `duration_minutes` rounds
                # down to 0 for short meetings — calculate from timestamps so
                # the UI can show "45s" or "1m 20s" properly.
                duration_sec = None
                if started_at:
                    try:
                        start_dt = datetime.fromisoformat(started_at)
                        end_dt = datetime.fromisoformat(ended_at) if ended_at else _utc_now()
                        duration_sec = max(0, int((end_dt - start_dt).total_seconds()))
                    except Exception:
                        pass

                meeting = {
                    "id": r[0], "title": r[1], "started_at": started_at, "ended_at": ended_at,
                    "duration_minutes": r[4],
                    "duration_seconds": duration_sec,
                    "summary": r[7], "status": r[8],
                    "tasks": [],
                    # total_chunks / total_tasks are computed live below from the
                    # actual window query — the stored fields go stale because
                    # tasks/transcriptions finish processing after end_meeting.
                    "total_chunks": r[5],
                    "total_tasks": r[6],
                }

                if started_at:
                    end_clause = "AND datetime(created_at) <= datetime(:end, '+60 seconds')" if ended_at else ""
                    params = {"start": started_at}
                    if ended_at:
                        params["end"] = ended_at

                    # Live chunk count
                    cq = await db.execute(
                        text(f"SELECT COUNT(*) FROM audio_chunks WHERE datetime(created_at) >= datetime(:start) {end_clause}"),
                        params,
                    )
                    live_chunks = cq.scalar() or 0
                    if live_chunks > 0:
                        meeting["total_chunks"] = live_chunks

                    # Tasks list (also doubles as live task count)
                    tq = await db.execute(
                        text(f"""
                            SELECT id, title, description, assigned_to, priority, status, action_type, created_at
                            FROM tasks
                            WHERE datetime(created_at) >= datetime(:start) {end_clause}
                            ORDER BY id DESC
                        """),
                        params,
                    )
                    meeting["tasks"] = [
                        {
                            "id": t[0], "title": t[1], "description": t[2],
                            "assigned_to": t[3], "priority": t[4], "status": t[5],
                            "action_type": t[6], "created_at": t[7],
                        }
                        for t in tq.fetchall()
                    ]
                    if meeting["tasks"]:
                        meeting["total_tasks"] = len(meeting["tasks"])
                meetings.append(meeting)
            return meetings

    async def get_meeting_tasks(self, meeting_id: int) -> dict:
        """Return a single meeting and the tasks extracted during it. Used by the
        per-meeting tasks endpoint on the dashboard."""
        async with async_session() as db:
            mq = await db.execute(
                text(
                    "SELECT id, title, started_at, ended_at, total_tasks, summary "
                    "FROM meetings WHERE id = :id"
                ),
                {"id": meeting_id},
            )
            m = mq.fetchone()
            if not m:
                return {"error": f"Meeting {meeting_id} not found"}

            if m[3]:
                tq = await db.execute(
                    text("""
                        SELECT id, title, description, assigned_to, priority, status, action_type,
                               action_payload, source_text, created_at
                        FROM tasks
                        WHERE datetime(created_at) >= datetime(:start)
                          AND datetime(created_at) <= datetime(:end, '+60 seconds')
                        ORDER BY id DESC
                    """),
                    {"start": m[2], "end": m[3]},
                )
            else:
                tq = await db.execute(
                    text("""
                        SELECT id, title, description, assigned_to, priority, status, action_type,
                               action_payload, source_text, created_at
                        FROM tasks
                        WHERE datetime(created_at) >= datetime(:start)
                        ORDER BY id DESC
                    """),
                    {"start": m[2]},
                )
            tasks = [
                {
                    "id": t[0], "title": t[1], "description": t[2], "assigned_to": t[3],
                    "priority": t[4], "status": t[5], "action_type": t[6],
                    "action_payload": t[7], "source_text": t[8], "created_at": t[9],
                }
                for t in tq.fetchall()
            ]
            return {
                "id": m[0], "title": m[1], "started_at": m[2], "ended_at": m[3],
                "total_tasks": m[4], "summary": m[5],
                "tasks": tasks,
            }
