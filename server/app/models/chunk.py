"""Audio chunk database model."""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AudioChunk(Base):
    __tablename__ = "audio_chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chunk_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    agent_id: Mapped[str] = mapped_column(String(100), default="default")
    source: Mapped[str] = mapped_column(String(20))  # "loopback" or "mic"
    start_time: Mapped[datetime] = mapped_column(DateTime)
    end_time: Mapped[datetime] = mapped_column(DateTime)
    duration_sec: Mapped[float] = mapped_column(Float)
    has_speech: Mapped[bool] = mapped_column(Boolean)
    speech_ratio: Mapped[float] = mapped_column(Float)
    active_window: Mapped[str | None] = mapped_column(String(200), nullable=True)
    active_window_title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    audio_file_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), default="received"
    )  # received, transcribing, transcribed, processed, failed
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
