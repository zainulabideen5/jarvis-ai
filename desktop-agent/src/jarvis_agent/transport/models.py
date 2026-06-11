"""Wire protocol models for WebSocket communication."""

from __future__ import annotations

from pydantic import BaseModel


class ChunkMetadata(BaseModel):
    """JSON metadata sent before the binary audio frame."""

    chunk_id: str
    source: str
    start_time: str  # ISO format
    end_time: str
    duration_sec: float
    has_speech: bool
    speech_ratio: float
    active_window: str | None
    active_window_title: str | None
    sample_rate: int = 16000
    channels: int = 1
    encoding: str = "pcm_s16le"
