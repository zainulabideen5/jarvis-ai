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
    """Manages meeting lifecycle — start, track, end with summary.

    WINDOWED task extraction (Zain's design):
      - During the meeting, every WINDOW_SEC (5 min) a background loop pulls
        that window's transcripts and extracts tasks via Claude, holding them
        in memory (NOT shown on dashboard yet — hidden till meeting end).
      - This spreads the LLM load across the meeting (small 5-min calls) instead
        of one giant full-transcript call at the end — critical for long
        (2-4 hour) meetings that would otherwise hit token/latency limits.
      - At end_meeting, all held window-tasks go through ONE final Claude
        consolidation pass (smart dedup) and are delivered together.
    """

    WINDOW_SEC = 300  # 5-minute task-extraction window

    def __init__(self, config: ServerConfig):
        self._config = config
        self._client = None
        # In-memory hold for incremental window tasks, keyed by meeting_id.
        # Singleton instance (see routes.py) so this survives start→end.
        self._window_tasks: dict[int, list[dict]] = {}
        self._window_loops: dict[int, "object"] = {}  # asyncio.Task handles
        self._window_last_mark: dict[int, str] = {}   # ISO ts of last window cut
        self._window_summaries: dict[int, list[str]] = {}  # per-window mini-summaries

    def _get_client(self) -> LLMClient:
        if self._client is None:
            self._client = LLMClient(self._config)
        return self._client

    @staticmethod
    async def _ensure_client_column(db) -> None:
        """Safe inline migration — add meetings.client_id if missing. SQLite
        ADD COLUMN is idempotent-ish; we guard with a try/except."""
        try:
            await db.execute(text("ALTER TABLE meetings ADD COLUMN client_id INTEGER"))
            await db.commit()
            log.info("meetings_client_id_column_added")
        except Exception:
            # Column already exists — fine.
            pass

    async def start_meeting(self, title: str = "", client_id: int | None = None) -> dict:
        """Start a new meeting — turns on listening.

        client_id: optional — associate this meeting (and its tasks) with a
        known client. If None, the client is AUTO-DETECTED from the conversation
        at meeting end (works for any name/company/language). New client →
        created automatically.
        """
        now = _utc_now()
        title = title or f"Meeting - {now.strftime('%d %b %Y %I:%M %p')}"

        async with async_session() as db:
            await self._ensure_client_column(db)

            # End any active meeting first
            await db.execute(
                text("UPDATE meetings SET status = 'ended', ended_at = :now WHERE status = 'active'"),
                {"now": now.isoformat()},
            )

            # Create new meeting (with optional client link)
            await db.execute(
                text("INSERT INTO meetings (title, started_at, status, client_id) VALUES (:title, :started_at, 'active', :client_id)"),
                {"title": title, "started_at": now.isoformat(), "client_id": client_id},
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

        # Launch the 5-min windowed task-extraction loop in the background.
        # It accumulates tasks in memory; nothing shows on the dashboard until
        # end_meeting consolidates and delivers them together.
        try:
            import asyncio
            self._window_tasks[meeting_id] = []
            self._window_summaries[meeting_id] = []
            loop_task = asyncio.create_task(self._window_loop(meeting_id))
            self._window_loops[meeting_id] = loop_task
        except Exception as e:
            log.warning("window_loop_launch_failed", error=str(e)[:120])

        log.info("meeting_started", title=title, meeting_id=meeting_id)
        return {"id": meeting_id, "title": title, "status": "active"}

    async def _window_loop(self, meeting_id: int) -> None:
        """Every WINDOW_SEC, extract tasks from the just-elapsed window's
        transcripts and hold them in memory. Runs until cancelled by
        end_meeting. Never crashes the meeting — logs and continues."""
        import asyncio
        from app.services.task_extractor import TaskExtractor

        extractor = TaskExtractor(self._config)
        # Track the high-water mark so each window only reads NEW transcripts.
        last_mark = _utc_now().isoformat()
        self._window_last_mark[meeting_id] = last_mark
        try:
            while True:
                await asyncio.sleep(self.WINDOW_SEC)
                window_end = _utc_now().isoformat()
                try:
                    # Pull transcripts produced in this 5-min window
                    async with async_session() as db:
                        result = await db.execute(
                            text("""
                                SELECT text FROM transcriptions
                                WHERE datetime(created_at) > datetime(:start)
                                  AND datetime(created_at) <= datetime(:end)
                                  AND length(text) > 5
                                ORDER BY created_at
                            """),
                            {"start": last_mark, "end": window_end},
                        )
                        rows = result.fetchall()
                    last_mark = window_end
                    self._window_last_mark[meeting_id] = last_mark

                    if not rows:
                        continue
                    window_text = " ".join(r[0] for r in rows).strip()
                    if len(window_text) < 25:
                        continue

                    tasks = await asyncio.to_thread(
                        extractor.extract_tasks, window_text, f"Meeting window (meeting {meeting_id})"
                    )
                    if tasks:
                        self._window_tasks.setdefault(meeting_id, []).extend(tasks)
                        log.info("meeting_window_tasks", meeting_id=meeting_id,
                                 window_count=len(tasks),
                                 total_held=len(self._window_tasks[meeting_id]))

                    # Also generate a SHORT mini-summary of this 5-min window and
                    # hold it. At end_meeting we combine these small summaries
                    # instead of re-reading the whole (huge) transcript — keeps
                    # the final summary fast even for 5-hour meetings.
                    try:
                        mini = await self._generate_mini_summary(window_text)
                        if mini:
                            self._window_summaries.setdefault(meeting_id, []).append(mini)
                    except Exception as e:
                        log.debug("mini_summary_failed", error=str(e)[:100])
                except Exception as e:
                    log.warning("meeting_window_extract_failed", meeting_id=meeting_id, error=str(e)[:120])
        except asyncio.CancelledError:
            log.info("window_loop_cancelled", meeting_id=meeting_id)
            raise

    async def end_meeting(self) -> dict:
        """End active meeting — generate summary from transcriptions.

        Briefly waits for the audio processing pipeline (transcription + task
        extraction) to flush any in-flight chunks from the last few seconds
        of the meeting. Without this delay, end_meeting often reports
        Chunks/Tasks: 0 even when work was actually completed seconds later.
        """
        import asyncio
        now = _utc_now()

        # ----- FAST: mark ended + listening OFF immediately (before any sleep) -----
        # This makes the End button respond instantly AND prevents a double-press:
        # the second call finds no 'active' meeting and returns gracefully instead
        # of running the whole heavy pipeline twice.
        async with async_session() as db:
            await self._ensure_client_column(db)
            result = await db.execute(
                text("SELECT id, title, started_at, client_id FROM meetings WHERE status = 'active' LIMIT 1")
            )
            meeting = result.fetchone()
            if not meeting:
                return {"error": "Koi active meeting pehle se band hai", "already_ended": True}

            meeting_id, title, started_at = meeting[0], meeting[1], meeting[2]
            client_id = meeting[3] if len(meeting) > 3 else None

            # Mark ended + listening off RIGHT NOW (atomic guard against double-end)
            await db.execute(
                text("UPDATE meetings SET status = 'ended', ended_at = :now WHERE id = :id"),
                {"now": now.isoformat(), "id": meeting_id},
            )
            await db.execute(
                text("INSERT OR REPLACE INTO system_state (key, value) VALUES ('listening', 'off')")
            )
            await db.commit()

        # Now do the (slower) flush + extraction. Meeting is already marked ended,
        # so a second button press won't reach here.
        await asyncio.sleep(8)

        async with async_session() as db:

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

        # ----- Stop the windowed extraction loop for this meeting -----
        loop_task = self._window_loops.pop(meeting_id, None)
        if loop_task is not None:
            try:
                loop_task.cancel()
            except Exception:
                pass

        # Generate summary + CONSOLIDATE windowed tasks (Zain's design)
        summary = "Koi transcription nahi hui meeting mein."
        meeting_tasks = []
        if transcriptions:
            trans_text = "\n".join(f"- [{t[1]}] {t[0]}" for t in transcriptions)
            full_text = " ".join(t[0] for t in transcriptions)

            # Generate summary — prefer the held per-window mini-summaries
            # (small → fast to combine) over re-reading the full transcript.
            # For a 5-hour meeting this turns a huge single call into a quick
            # combine of ~60 short notes. Fallback to full transcript only if
            # no mini-summaries exist (short meeting / server restart mid-meeting).
            mini_summaries = list(self._window_summaries.get(meeting_id, []))
            if mini_summaries:
                combined_notes = "\n".join(f"- {m}" for m in mini_summaries)
                summary = await self._generate_summary(combined_notes, duration)
                log.info("summary_from_mini_windows", windows=len(mini_summaries))
            else:
                summary = await self._generate_summary(trans_text, duration)

            from app.services.task_extractor import TaskExtractor
            extractor = TaskExtractor(self._config)
            import asyncio
            try:
                # 1) Start with all tasks the 5-min windows already extracted (held in memory)
                held = list(self._window_tasks.get(meeting_id, []))

                # 2) Extract the FINAL partial window (transcripts after the last
                #    5-min cut that the loop didn't process yet).
                final_mark = self._window_last_mark.get(meeting_id)
                if final_mark:
                    async with async_session() as db:
                        tail = await db.execute(
                            text("""
                                SELECT text FROM transcriptions
                                WHERE datetime(created_at) > datetime(:start)
                                  AND length(text) > 5
                                ORDER BY created_at
                            """),
                            {"start": final_mark},
                        )
                        tail_rows = tail.fetchall()
                    tail_text = " ".join(r[0] for r in tail_rows).strip()
                    if len(tail_text) >= 25:
                        tail_tasks = await asyncio.to_thread(
                            extractor.extract_tasks, tail_text, f"Meeting final window: {title}"
                        )
                        held.extend(tail_tasks or [])

                # 3) Fallback: if NO windowed tasks (short meeting < 5min, or
                #    server restarted mid-meeting), extract from full transcript.
                if not held:
                    held = await asyncio.to_thread(
                        extractor.extract_tasks, full_text, f"Meeting: {title}"
                    ) or []

                # 4) ONE final smart consolidation (Claude dedups intelligently)
                meeting_tasks = await self._consolidate_tasks(held)

                # 4b) Resolve the client for this meeting. If the user didn't
                #     pre-select one, AUTO-DETECT from the transcript (any
                #     name/company/language) and find-or-create the client.
                if client_id is None:
                    client_id = await self._detect_and_resolve_client(full_text)
                    if client_id:
                        # Persist the detected client back onto the meeting row
                        async with async_session() as db:
                            await db.execute(
                                text("UPDATE meetings SET client_id = :cid WHERE id = :id"),
                                {"cid": client_id, "id": meeting_id},
                            )
                            await db.commit()
                        log.info("meeting_client_autodetected", meeting_id=meeting_id, client_id=client_id)

                if meeting_tasks:
                    from app.models.task import Task
                    async with async_session() as db:
                        for task_data in meeting_tasks:
                            task = Task(
                                chunk_id=None,
                                client_id=client_id,  # tag task with the meeting's client
                                title=task_data.get("title", "Untitled"),
                                description=task_data.get("description"),
                                assigned_to=task_data.get("assigned_to"),
                                priority=task_data.get("priority", "medium"),
                                source_text=full_text[:500],
                                action_type=task_data.get("action_type"),
                            )
                            db.add(task)
                        await db.commit()
                    log.info("meeting_tasks_extracted", count=len(meeting_tasks), client_id=client_id)

                    # Bump client stats
                    if client_id:
                        async with async_session() as db:
                            await db.execute(
                                text("UPDATE clients SET total_tasks = total_tasks + :n, "
                                     "last_contact = :now WHERE id = :id"),
                                {"n": len(meeting_tasks), "now": now.isoformat(), "id": client_id},
                            )
                            await db.commit()
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

        # Wait for any in-flight chunks (queued during the meeting) to finish
        # processing — otherwise the task count is a snapshot taken before the
        # last chunks' tasks land, showing "Tasks: 0" even though tasks appear
        # seconds later. Drain the queue (with a cap) so the count is accurate.
        try:
            import asyncio
            from app.ws.handler import processing_queue
            for _ in range(20):  # up to ~20s
                if processing_queue.empty():
                    break
                await asyncio.sleep(1)
            await asyncio.sleep(2)  # let the last in-flight chunk's tasks commit
        except Exception:
            pass

        # LIVE re-count of tasks actually saved for this meeting window — robust
        # against the timing race (covers both end-consolidation tasks AND any
        # straggler per-chunk tasks committed after end_meeting started).
        total_all = total_tasks + len(meeting_tasks)
        try:
            async with async_session() as db:
                live = await db.execute(
                    text("SELECT COUNT(*) FROM tasks WHERE datetime(created_at) >= datetime(:start)"),
                    {"start": started_at},
                )
                live_count = live.scalar() or 0
                if live_count > total_all:
                    total_all = live_count
                # Keep the meeting row's stored count in sync with reality
                await db.execute(
                    text("UPDATE meetings SET total_tasks = :t WHERE id = :id"),
                    {"t": total_all, "id": meeting_id},
                )
                await db.commit()
        except Exception:
            pass

        # Notify in chat — meeting summary
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

        # Clean up in-memory windowed state for this meeting
        self._window_tasks.pop(meeting_id, None)
        self._window_last_mark.pop(meeting_id, None)
        self._window_summaries.pop(meeting_id, None)

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

    async def _detect_and_resolve_client(self, full_text: str) -> int | None:
        """Auto-detect the client/company from a meeting transcript and return
        a client_id (find-or-create). Works for ANY name/company in ANY
        language. Returns None if no clear client can be detected.

        Used when the user didn't pre-select a client before the meeting.
        """
        if not full_text or len(full_text.strip()) < 40:
            return None

        detected_name = None
        detected_company = None
        # Ask Claude to pull the external party (client) — NOT the user's own team.
        sys_prompt = (
            "Yeh ek meeting transcript hai (boss + client). Bata is meeting ka "
            "CLIENT (bahar wala banda/company) kaun hai. SIRF ek JSON object de: "
            '{"name": "<client ka naam ya company, ya empty agar pata na chale>", '
            '"company": "<company ya empty>"}. Koi aur text nahi.'
        )
        try:
            from app.services.universal_engine.brain import ClaudeCLIBrain
            import asyncio
            brain = ClaudeCLIBrain(model="opus")
            if brain.is_available():
                out = await asyncio.to_thread(
                    brain.ask, sys_prompt, [{"role": "user", "content": full_text[:6000]}]
                )
                import json as _json
                import re as _re
                m = _re.search(r"\{.*\}", out or "", _re.DOTALL)
                if m:
                    data = _json.loads(m.group())
                    detected_name = (data.get("name") or "").strip()
                    detected_company = (data.get("company") or "").strip()
        except Exception as e:
            log.debug("client_detect_failed", error=str(e)[:120])

        # Use name, else company, as the client identifier
        label = detected_name or detected_company
        if not label or len(label) < 2:
            return None

        # Find-or-create client (case-insensitive name match)
        try:
            async with async_session() as db:
                existing = await db.execute(
                    text("SELECT id FROM clients WHERE lower(name) = lower(:n) LIMIT 1"),
                    {"n": label},
                )
                row = existing.fetchone()
                if row:
                    return row[0]
                # Create new client (provide ALL NOT-NULL columns explicitly —
                # SQLAlchemy model defaults are app-level, not DB-level)
                await db.execute(
                    text("INSERT INTO clients (name, company, is_active, total_conversations, total_tasks) "
                         "VALUES (:name, :company, 1, 1, 0)"),
                    {"name": label, "company": detected_company or None},
                )
                await db.commit()
                newid = await db.execute(text("SELECT last_insert_rowid()"))
                cid = newid.scalar()
                log.info("client_auto_created", name=label, client_id=cid)
                return cid
        except Exception as e:
            log.warning("client_resolve_failed", error=str(e)[:120])
            return None

    async def _generate_mini_summary(self, window_text: str) -> str:
        """Short summary of a single 5-min window. Claude primary, Groq fallback.
        Output is a few bullet points — small, so combining many at end is fast."""
        if not window_text or len(window_text.strip()) < 25:
            return ""
        sys_prompt = (
            "Yeh meeting ke 5-min hisse ki transcript hai. 1-3 chhote bullet points "
            "mein key baatein likho (Roman Urdu + English). Sirf points, koi heading nahi."
        )
        # PRIMARY: Claude
        try:
            from app.services.universal_engine.brain import ClaudeCLIBrain
            import asyncio
            brain = ClaudeCLIBrain(model="opus")
            if brain.is_available():
                out = await asyncio.to_thread(
                    brain.ask, sys_prompt, [{"role": "user", "content": window_text[:4000]}]
                )
                if out and out.strip():
                    return out.strip()
        except Exception as e:
            log.debug("claude_mini_summary_failed", error=str(e)[:100])
        # FALLBACK: Groq (silent)
        try:
            client = self._get_client()
            resp = client.chat.completions.create(
                model=self._config.groq_model,
                messages=[{"role": "user", "content": f"{sys_prompt}\n\n{window_text[:4000]}"}],
                temperature=0.3,
                max_tokens=300,
            )
            return resp.choices[0].message.content.strip()
        except Exception:
            return ""

    async def _consolidate_tasks(self, tasks: list[dict]) -> list[dict]:
        """Final smart consolidation of all window tasks via Claude.

        Merges duplicates intelligently (same task discussed in two windows →
        one entry), keeps the clearest title/description/assignee/priority.
        Returns the clean deduplicated list. Falls back to the raw list if
        Claude is unavailable or errors (never loses tasks)."""
        if len(tasks) <= 1:
            return tasks
        try:
            from app.services.universal_engine.brain import ClaudeCLIBrain
            brain = ClaudeCLIBrain(model="opus")
            if not brain.is_available():
                return self._dedup_fallback(tasks)

            tasks_json = json.dumps(tasks, ensure_ascii=False)
            system = (
                "Tu meeting tasks consolidate karta hai. Neeche ek JSON array hai "
                "jisme meeting ke alag-alag hisson se nikle tasks hain — kuch DUPLICATE "
                "ho sakte (same kaam 2 baar bola gaya). Inhe SMART tareeqe se merge kar:\n"
                "- Same task (chahe alfaaz alag hon) → EK hi rakho, behtareen title/description\n"
                "- Alag tasks alag rakho\n"
                "- Har task: title, description, assigned_to, priority, action_type\n"
                "SIRF ek clean JSON array return kar, koi aur text nahi."
            )
            result = brain.ask(system, [{"role": "user", "content": tasks_json}])
            from app.services.task_extractor import TaskExtractor
            consolidated = TaskExtractor._parse_json(result)
            if consolidated:
                log.info("meeting_tasks_consolidated", before=len(tasks), after=len(consolidated))
                return consolidated
            return self._dedup_fallback(tasks)
        except Exception as e:
            log.warning("task_consolidation_failed_using_fallback", error=str(e)[:120])
            return self._dedup_fallback(tasks)

    @staticmethod
    def _dedup_fallback(tasks: list[dict]) -> list[dict]:
        """Simple dedup if Claude consolidation isn't available — match on
        normalized (title + assignee)."""
        seen = set()
        out = []
        for t in tasks:
            key = (
                (t.get("title") or "").strip().lower(),
                (t.get("assigned_to") or "").strip().lower(),
            )
            if key in seen:
                continue
            seen.add(key)
            out.append(t)
        return out

    async def _generate_summary(self, transcriptions: str, duration: int) -> str:
        """Generate meeting summary. Claude CLI PRIMARY, Groq silent fallback."""
        prompt = SUMMARY_PROMPT.replace("__TRANSCRIPTIONS__", transcriptions)

        # ----- PRIMARY: Claude CLI -----
        try:
            from app.services.universal_engine.brain import ClaudeCLIBrain
            brain = ClaudeCLIBrain(model="opus")
            if brain.is_available():
                import asyncio
                result = await asyncio.to_thread(
                    brain.ask,
                    "Tu JARVIS hai. Professional meeting summary bana Roman Urdu + English mein, format follow kar.",
                    [{"role": "user", "content": prompt}],
                )
                if result and result.strip():
                    return result.strip()
        except Exception as e:
            log.warning("claude_summary_failed_falling_to_groq", error=str(e)[:140])

        # ----- FALLBACK: Groq (silent background) -----
        try:
            client = self._get_client()
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
