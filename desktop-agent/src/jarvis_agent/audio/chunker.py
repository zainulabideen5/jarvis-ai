"""VAD-aware audio chunker — splits audio into ~15-sec segments. Bulletproof."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta
from typing import Literal

import numpy as np

from jarvis_agent.config import AgentConfig
from jarvis_agent.core.events import AudioChunk
from jarvis_agent.utils.logging import get_logger
from jarvis_agent.vad.silero import SileroVAD

log = get_logger(__name__)

# Max buffer: 60 sec at 16kHz — hard safety limit
MAX_BUFFER = 960000


class AudioChunker:
    """Accumulates audio and emits chunks, splitting at silence gaps."""

    def __init__(
        self,
        config: AgentConfig,
        vad: SileroVAD,
        source: Literal["loopback", "mic"],
        output_queue: asyncio.Queue,
    ):
        self._target = int(config.audio_chunk_duration_sec * config.audio_sample_rate)
        self._rate = config.audio_sample_rate
        self._vad = vad
        self._source = source
        self._queue = output_queue
        self._buffer = np.array([], dtype=np.float32)
        self._start: datetime | None = None

    async def feed(self, audio: np.ndarray, timestamp: datetime) -> None:
        """Feed audio data into the chunker."""
        if self._start is None:
            self._start = timestamp

        try:
            self._buffer = np.concatenate([self._buffer, audio])
        except (MemoryError, Exception):
            # Emergency: clear buffer
            log.warning("chunker_memory_reset", source=self._source)
            self._buffer = audio.copy()
            self._start = timestamp

        # Hard safety: if buffer too big, force emit
        if len(self._buffer) > MAX_BUFFER:
            await self._emit_chunk(len(self._buffer))

        # Normal emit at target size
        while len(self._buffer) >= self._target:
            split = self._find_split()
            await self._emit_chunk(split)

    async def _emit_chunk(self, split: int) -> None:
        """Emit a chunk from buffer."""
        try:
            chunk_audio = self._buffer[:split]
            self._buffer = self._buffer[split:]

            duration = len(chunk_audio) / self._rate
            ratio = self._vad.speech_ratio(chunk_audio)

            chunk = AudioChunk(
                chunk_id=str(uuid.uuid4()),
                audio_data=self._to_pcm(chunk_audio),
                source=self._source,
                start_time=self._start,
                end_time=self._start + timedelta(seconds=duration),
                duration_sec=duration,
                has_speech=ratio > 0.0,
                speech_ratio=ratio,
            )

            log.info(
                "chunk_emitted",
                chunk_id=chunk.chunk_id[:8],
                source=self._source,
                duration=f"{duration:.1f}s",
                speech=f"{ratio:.2f}",
                has_speech=chunk.has_speech,
            )

            await self._queue.put(chunk)
            self._start = chunk.end_time

        except (MemoryError, Exception) as e:
            log.warning("chunk_emit_failed", source=self._source, error=str(e))
            self._buffer = np.array([], dtype=np.float32)
            self._start = None

    def _find_split(self) -> int:
        """Find best split point near target duration."""
        try:
            tail_size = 3 * self._rate  # Look in last 3 sec
            tail_start = max(0, self._target - tail_size)
            tail = self._buffer[tail_start:self._target]

            timestamps = self._vad.get_speech_timestamps(tail)

            if not timestamps:
                return self._target

            # Find largest silence gap
            best_start, best_size = None, 0

            if timestamps[0]["start"] > best_size:
                best_size = timestamps[0]["start"]
                best_start = 0

            for i in range(len(timestamps) - 1):
                gap = timestamps[i + 1]["start"] - timestamps[i]["end"]
                if gap > best_size:
                    best_size = gap
                    best_start = timestamps[i]["end"]

            last_gap = len(tail) - timestamps[-1]["end"]
            if last_gap > best_size:
                best_size = last_gap
                best_start = timestamps[-1]["end"]

            min_gap = int(0.3 * self._rate)
            if best_start is not None and best_size >= min_gap:
                return tail_start + best_start

        except Exception:
            pass

        return self._target

    async def flush(self) -> None:
        """Flush remaining buffer."""
        if len(self._buffer) < self._rate // 2 or self._start is None:
            return
        await self._emit_chunk(len(self._buffer))
        self._buffer = np.array([], dtype=np.float32)
        self._start = None
        log.info("chunker_flushed", source=self._source)

    @staticmethod
    def _to_pcm(audio: np.ndarray) -> bytes:
        """Convert float32 to 16-bit PCM bytes."""
        return (audio * 32767).clip(-32768, 32767).astype(np.int16).tobytes()
