"""WebSocket handler for receiving audio chunks from desktop agents."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import WebSocket, WebSocketDisconnect
from sqlalchemy import select

from app.core.config import ServerConfig
from app.core.database import async_session
from app.core.logging import get_logger
from app.models.agent import Agent
from app.models.chunk import AudioChunk

log = get_logger(__name__)

# Global processing queue — services pick chunks from here
processing_queue: asyncio.Queue[str] = asyncio.Queue()

# Track connected agents
connected_agents: dict[str, WebSocket] = {}


async def _register_agent(agent_id: str, hostname: str | None = None) -> None:
    """Register or update agent in the database."""
    async with async_session() as db:
        result = await db.execute(
            select(Agent).where(Agent.agent_id == agent_id)
        )
        agent = result.scalar_one_or_none()

        if agent:
            agent.is_online = True
            agent.last_seen = datetime.utcnow()
            if hostname:
                agent.hostname = hostname
        else:
            agent = Agent(
                agent_id=agent_id,
                name=agent_id,
                hostname=hostname,
                is_online=True,
                last_seen=datetime.utcnow(),
            )
            db.add(agent)

        await db.commit()


async def _mark_agent_offline(agent_id: str) -> None:
    """Mark agent as offline."""
    async with async_session() as db:
        result = await db.execute(
            select(Agent).where(Agent.agent_id == agent_id)
        )
        agent = result.scalar_one_or_none()
        if agent:
            agent.is_online = False
            agent.last_seen = datetime.utcnow()
            await db.commit()


async def _increment_agent_chunks(agent_id: str) -> None:
    """Increment chunk count for agent."""
    async with async_session() as db:
        result = await db.execute(
            select(Agent).where(Agent.agent_id == agent_id)
        )
        agent = result.scalar_one_or_none()
        if agent:
            agent.total_chunks += 1
            agent.last_seen = datetime.utcnow()
            await db.commit()


async def agent_ws_handler(websocket: WebSocket) -> None:
    """Handle a WebSocket connection from a desktop agent.

    Protocol:
        1. Agent sends JSON text frame (chunk metadata)
        2. Agent sends binary frame (PCM audio data)
        3. Repeat
    """
    await websocket.accept()
    agent_id = websocket.query_params.get("agent_id", "default")
    hostname = websocket.query_params.get("hostname")
    log.info("agent_connected", agent_id=agent_id, hostname=hostname)

    # Register agent
    await _register_agent(agent_id, hostname)
    connected_agents[agent_id] = websocket

    config = ServerConfig()
    config.audio_chunks_dir.mkdir(parents=True, exist_ok=True)

    try:
        while True:
            # Always receive the meta+audio PAIR first (so the stream stays in
            # sync), THEN parse/save inside an inner try — a single malformed
            # chunk must NOT tear down the whole agent connection (it used to:
            # bad JSON / bad ISO date crashed the loop → full reconnect churn).
            meta_raw = await websocket.receive_text()      # disconnect propagates
            audio_data = await websocket.receive_bytes()
            try:
                meta = json.loads(meta_raw)
                chunk_id = meta.get("chunk_id", str(uuid.uuid4()))
                has_speech = meta.get("has_speech", True)

                audio_path = config.audio_chunks_dir / f"{chunk_id}.pcm"
                audio_path.write_bytes(audio_data)

                async with async_session() as db:
                    chunk = AudioChunk(
                        chunk_id=chunk_id,
                        agent_id=agent_id,
                        source=meta.get("source", "unknown"),
                        start_time=datetime.fromisoformat(meta["start_time"]),
                        end_time=datetime.fromisoformat(meta["end_time"]),
                        duration_sec=meta.get("duration_sec", 0),
                        has_speech=has_speech,
                        speech_ratio=meta.get("speech_ratio", 0),
                        active_window=meta.get("active_window"),
                        active_window_title=meta.get("active_window_title"),
                        audio_file_path=str(audio_path),
                        status="received",
                    )
                    db.add(chunk)
                    await db.commit()

                await _increment_agent_chunks(agent_id)
                log.info(
                    "chunk_received",
                    chunk_id=chunk_id[:8],
                    agent_id=agent_id,
                    source=meta.get("source"),
                    duration=f"{meta.get('duration_sec', 0):.1f}s",
                    has_speech=has_speech,
                    size_kb=len(audio_data) // 1024,
                )
                if has_speech:
                    await processing_queue.put(chunk_id)
                await websocket.send_json({
                    "status": "ok", "chunk_id": chunk_id, "queued": has_speech,
                })
            except Exception as e:
                # bad chunk → skip it, keep the connection alive
                log.warning("chunk_skipped", agent_id=agent_id, error=str(e)[:200])
                try:
                    await websocket.send_json({"status": "error", "error": str(e)[:120]})
                except Exception:
                    pass
                continue

    except WebSocketDisconnect:
        log.info("agent_disconnected", agent_id=agent_id)
    except Exception as e:
        log.error("ws_error", agent_id=agent_id, error=str(e))
    finally:
        connected_agents.pop(agent_id, None)
        await _mark_agent_offline(agent_id)
