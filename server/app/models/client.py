"""Client/Contact model — stores client profiles with full details."""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Client(Base):
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200))
    company: Mapped[str | None] = mapped_column(String(300), nullable=True)
    role: Mapped[str | None] = mapped_column(String(200), nullable=True)  # CEO, Manager, Developer etc.
    email: Mapped[str | None] = mapped_column(String(300), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    website: Mapped[str | None] = mapped_column(String(500), nullable=True)
    logo_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Speaker voice profile — links to pyannote speaker ID
    speaker_label: Mapped[str | None] = mapped_column(String(50), nullable=True)  # e.g. "SPEAKER_00"

    # Stats
    total_conversations: Mapped[int] = mapped_column(Integer, default=0)
    total_tasks: Mapped[int] = mapped_column(Integer, default=0)
    last_contact: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
