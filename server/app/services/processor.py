"""Background processor — picks chunks from queue, transcribes, extracts tasks."""

from __future__ import annotations

import asyncio
from pathlib import Path

from sqlalchemy import select, text

from app.core.config import ServerConfig
from app.core.database import async_session
from app.core.logging import get_logger
from app.models.chunk import AudioChunk
from app.models.client import Client
from app.models.task import Task
from app.models.transcription import Transcription
from app.services.calendar_service import CalendarService
from app.services.diarization import DiarizationService
from app.services.rules_engine import RulesEngine
from app.services.task_extractor import TaskExtractor
from app.services.transcription import TranscriptionService
from app.ws.handler import processing_queue

log = get_logger(__name__)


class ChunkProcessor:
    """Background worker that processes audio chunks."""

    def __init__(self, config: ServerConfig):
        self._config = config
        self._transcriber = TranscriptionService(config)
        self._task_extractor = TaskExtractor(config)
        self._rules_engine = RulesEngine()
        self._calendar = CalendarService(config)
        self._diarizer = DiarizationService(config) if config.diarization_enabled else None

    async def run(self) -> None:
        """Main processing loop — runs forever, picks from queue."""
        log.info("processor_started")

        # Load whisper model on first use (lazy)
        while True:
            chunk_id = await processing_queue.get()

            try:
                await self._process_chunk(chunk_id)
            except Exception as e:
                log.error("chunk_processing_failed", chunk_id=chunk_id[:8], error=str(e))
                # Mark as failed in DB
                async with async_session() as db:
                    result = await db.execute(
                        select(AudioChunk).where(AudioChunk.chunk_id == chunk_id)
                    )
                    chunk = result.scalar_one_or_none()
                    if chunk:
                        chunk.status = "failed"
                        await db.commit()

    async def _process_chunk(self, chunk_id: str) -> None:
        """Process a single chunk: transcribe -> extract tasks."""
        log.info("processing_chunk", chunk_id=chunk_id[:8])

        # Get chunk from DB
        async with async_session() as db:
            result = await db.execute(
                select(AudioChunk).where(AudioChunk.chunk_id == chunk_id)
            )
            chunk = result.scalar_one_or_none()
            if not chunk:
                log.warning("chunk_not_found", chunk_id=chunk_id[:8])
                return

            # Update status
            chunk.status = "transcribing"
            await db.commit()

        # Read audio file
        audio_path = Path(chunk.audio_file_path)
        if not audio_path.exists():
            log.error("audio_file_missing", path=str(audio_path))
            return

        pcm_bytes = audio_path.read_bytes()

        # Transcribe (CPU-bound, run in thread)
        result = await asyncio.to_thread(
            self._transcriber.transcribe_pcm, pcm_bytes
        )

        text = result["text"]

        # Speaker diarization (optional)
        diarized_text = text
        if self._diarizer and text and len(text.strip()) > 20:
            try:
                segments = await asyncio.to_thread(
                    self._diarizer.diarize_pcm, pcm_bytes
                )
                if segments:
                    # Fetch client speaker mappings from DB
                    speaker_names = {}
                    async with async_session() as db:
                        clients_result = await db.execute(
                            select(Client).where(Client.speaker_label.isnot(None))
                        )
                        for client in clients_result.scalars().all():
                            if client.speaker_label:
                                speaker_names[client.speaker_label] = client.name

                    diarized_text = self._diarizer.merge_with_transcription(
                        segments, text, speaker_names
                    )
                    log.info(
                        "diarization_complete",
                        chunk_id=chunk_id[:8],
                        speakers=len(set(s["speaker"] for s in segments)),
                    )
            except Exception as e:
                log.warning("diarization_skipped", error=str(e))

        # Save transcription
        async with async_session() as db:
            transcription = Transcription(
                chunk_id=chunk_id,
                text=diarized_text if diarized_text != text else text,
                language=result["language"],
                confidence=result["confidence"],
                processing_time_sec=result["time_sec"],
            )
            db.add(transcription)

            # Update chunk status
            db_chunk = await db.execute(
                select(AudioChunk).where(AudioChunk.chunk_id == chunk_id)
            )
            chunk_row = db_chunk.scalar_one()
            chunk_row.status = "transcribed"
            await db.commit()

        log.info(
            "chunk_transcribed",
            chunk_id=chunk_id[:8],
            text_preview=text[:100] if text else "(empty)",
            language=result["language"],
        )

        # During an ACTIVE meeting, skip per-chunk task extraction entirely —
        # the MeetingService's 5-min windowed loop handles tasks (batched,
        # consolidated at end). Per-chunk extraction here would duplicate that
        # work, spam the LLM (~480 calls for a 4hr meeting), and surface raw
        # fragmented tasks on the dashboard mid-meeting (user wants them only
        # at meeting end). We still transcribe + save every chunk above.
        meeting_active = False
        try:
            async with async_session() as db:
                mres = await db.execute(
                    text("SELECT 1 FROM meetings WHERE status = 'active' LIMIT 1")
                )
                meeting_active = mres.fetchone() is not None
        except Exception:
            meeting_active = False

        # Extract tasks only if there's meaningful text (>40 chars filters out
        # short garbage that the garbage-detector lets through). Raising this
        # threshold prevents hallucinated tasks from noisy chunks like
        # "Adiky aski, we can not stop you..." that aren't real conversation.
        if text and len(text.strip()) > 40 and not meeting_active:
            try:
                window_ctx = f"{chunk.active_window} - {chunk.active_window_title}"
                tasks = await asyncio.to_thread(
                    self._task_extractor.extract_tasks, text, window_ctx
                )

                if tasks:
                    async with async_session() as db:
                        saved_tasks = []
                        for task_data in tasks:
                            task = Task(
                                chunk_id=chunk_id,
                                title=task_data.get("title", "Untitled"),
                                description=task_data.get("description"),
                                assigned_to=task_data.get("assigned_to"),
                                priority=task_data.get("priority", "medium"),
                                source_text=text,
                                action_type=task_data.get("action_type"),
                                action_payload=(
                                    str(task_data.get("action_payload"))
                                    if task_data.get("action_payload")
                                    else None
                                ),
                            )
                            db.add(task)
                            saved_tasks.append(task)
                        await db.flush()

                        # Run rules engine on each new task
                        for task in saved_tasks:
                            await self._rules_engine.evaluate(task, db)

                        await db.commit()

                    log.info(
                        "tasks_saved",
                        chunk_id=chunk_id[:8],
                        count=len(tasks),
                    )
            except Exception as e:
                log.warning("task_extraction_skipped", error=str(e))

            # Extract calendar events from transcription
            try:
                events = await asyncio.to_thread(
                    self._calendar.extract_events, text
                )
                for event in events:
                    await self._calendar.save_event(event, chunk_id)
                if events:
                    log.info("calendar_events_found", chunk_id=chunk_id[:8], count=len(events))
            except Exception as e:
                log.debug("calendar_extraction_skipped", error=str(e))

        # Mark as fully processed
        async with async_session() as db:
            db_chunk = await db.execute(
                select(AudioChunk).where(AudioChunk.chunk_id == chunk_id)
            )
            chunk_row = db_chunk.scalar_one()
            chunk_row.status = "processed"
            await db.commit()
