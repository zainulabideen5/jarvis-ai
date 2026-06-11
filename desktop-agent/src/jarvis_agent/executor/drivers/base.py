"""Base driver interface."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ActionResult:
    """Result of executing a driver action."""
    success: bool
    message: str = ""
    error: str = ""
    details: dict = field(default_factory=dict)


class BaseDriver:
    """Abstract base for all action drivers."""

    async def execute(self, task: dict, payload: dict) -> ActionResult:
        raise NotImplementedError
