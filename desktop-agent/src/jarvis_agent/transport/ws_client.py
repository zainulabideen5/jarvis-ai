"""Reconnecting WebSocket client for uploading audio chunks."""

from __future__ import annotations

import asyncio

import websockets

from jarvis_agent.config import AgentConfig
from jarvis_agent.core.events import AudioChunk
from jarvis_agent.transport.models import ChunkMetadata
from jarvis_agent.transport.offline_queue import OfflineQueue
from jarvis_agent.utils.logging import get_logger

log = get_logger(__name__)


class AgentWebSocketClient:
    """Drains the chunk queue and uploads via WebSocket.

    Automatically reconnects on disconnect. Chunks are persisted
    to a local SQLite queue when the server is unreachable, and
    drained on reconnection — zero data loss.
    """

    def __init__(self, config: AgentConfig, chunk_queue: asyncio.Queue):
        import socket
        hostname = socket.gethostname()
        agent_id = config.agent_id
        self._url = f"{config.server_ws_url}?agent_id={agent_id}&hostname={hostname}"
        self._queue = chunk_queue
        self._reconnect_delay = config.reconnect_delay_sec
        self._timeout = config.upload_timeout_sec
        self._offline = OfflineQueue(config)
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    async def run(self) -> None:
        """Main loop — connect, drain offline queue, send live, reconnect on failure."""
        while True:
            try:
                async with websockets.connect(self._url) as ws:
                    self._connected = True
                    log.info("ws_connected", url=self._url)

                    # Drain offline queue first
                    await self._drain_offline(ws)

                    # Then send live chunks
                    await self._send_loop(ws)
            except (
                ConnectionRefusedError,
                websockets.ConnectionClosed,
                websockets.exceptions.InvalidStatus,
                OSError,
            ) as e:
                self._connected = False
                log.warning(
                    "ws_disconnected",
                    error=str(e),
                    retry_in=self._reconnect_delay,
                    offline_pending=self._offline.count,
                )
                # Drain any queued chunks into offline storage
                await self._flush_to_offline()
                await asyncio.sleep(self._reconnect_delay)

    async def _drain_offline(self, ws) -> None:
        """Send any chunks stored in the offline queue."""
        while True:
            batch = await asyncio.to_thread(self._offline.peek_batch, 5)
            if not batch:
                break

            for row_id, chunk_id, meta_json, audio_data in batch:
                try:
                    await ws.send(meta_json)
                    await ws.send(audio_data)
                    await asyncio.to_thread(self._offline.remove, row_id)
                    log.info("offline_chunk_sent", chunk_id=chunk_id[:8])
                except Exception:
                    return  # Connection lost, will retry

    async def _send_loop(self, ws) -> None:
        """Drain live queue and send chunks as JSON metadata + binary PCM."""
        while True:
            chunk: AudioChunk = await self._queue.get()

            meta = ChunkMetadata(
                chunk_id=chunk.chunk_id,
                source=chunk.source,
                start_time=chunk.start_time.isoformat(),
                end_time=chunk.end_time.isoformat(),
                duration_sec=chunk.duration_sec,
                has_speech=chunk.has_speech,
                speech_ratio=chunk.speech_ratio,
                active_window=chunk.active_window,
                active_window_title=chunk.active_window_title,
            )

            try:
                await ws.send(meta.model_dump_json())
                await ws.send(chunk.audio_data)
                log.debug(
                    "chunk_uploaded",
                    chunk_id=chunk.chunk_id[:8],
                    size_kb=len(chunk.audio_data) // 1024,
                )
            except Exception:
                # Store to offline queue on send failure
                self._offline.enqueue(
                    chunk.chunk_id, meta.model_dump_json(), chunk.audio_data
                )
                raise  # Trigger reconnect

            self._queue.task_done()

    async def _flush_to_offline(self) -> None:
        """Move any live queued chunks to the offline SQLite store."""
        flushed = 0
        while not self._queue.empty():
            try:
                chunk: AudioChunk = self._queue.get_nowait()
                meta = ChunkMetadata(
                    chunk_id=chunk.chunk_id,
                    source=chunk.source,
                    start_time=chunk.start_time.isoformat(),
                    end_time=chunk.end_time.isoformat(),
                    duration_sec=chunk.duration_sec,
                    has_speech=chunk.has_speech,
                    speech_ratio=chunk.speech_ratio,
                    active_window=chunk.active_window,
                    active_window_title=chunk.active_window_title,
                )
                self._offline.enqueue(
                    chunk.chunk_id, meta.model_dump_json(), chunk.audio_data
                )
                flushed += 1
            except asyncio.QueueEmpty:
                break

        if flushed:
            log.info("chunks_flushed_to_offline", count=flushed)
