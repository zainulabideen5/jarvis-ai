"""Typed data classes for internal event passing."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


@dataclass
class AudioChunk:
    """A processed audio chunk ready for upload."""

    audio_data: bytes  # 16-bit PCM, 16kHz, mono
    source: Literal["loopback", "mic"]
    start_time: datetime
    end_time: datetime
    duration_sec: float
    has_speech: bool
    speech_ratio: float  # 0.0 - 1.0
    active_window: str | None = None
    active_window_title: str | None = None
    chunk_id: str = field(default_factory=lambda: str(uuid.uuid4()))
