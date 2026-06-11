"""Extracted task database model."""

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chunk_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    client_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # FK to clients
    title: Mapped[str] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    assigned_to: Mapped[str | None] = mapped_column(String(100), nullable=True)
    priority: Mapped[str] = mapped_column(String(20), default="medium")  # low, medium, high, urgent
    status: Mapped[str] = mapped_column(
        String(20), default="pending"
    )  # pending, approved, in_progress, completed, rejected
    source_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    action_type: Mapped[str | None] = mapped_column(
        String(50), nullable=True
    )  # send_message, create_doc, api_call, browser_action, etc.
    action_payload: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # JSON payload for action execution
    requires_approval: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
