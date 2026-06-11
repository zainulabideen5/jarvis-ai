"""Rule model — defines automation rules for task processing."""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Rule(Base):
    __tablename__ = "rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # Conditions (all optional — if set, must match)
    match_action_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    match_priority: Mapped[str | None] = mapped_column(String(20), nullable=True)
    match_keyword: Mapped[str | None] = mapped_column(String(200), nullable=True)
    match_assigned_to: Mapped[str | None] = mapped_column(String(100), nullable=True)
    match_window: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # Actions
    auto_approve: Mapped[bool] = mapped_column(Boolean, default=False)
    override_priority: Mapped[str | None] = mapped_column(String(20), nullable=True)
    override_assigned_to: Mapped[str | None] = mapped_column(String(100), nullable=True)
    notify_telegram: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
