"""Approved-recipients memory — confirmation only on FIRST send to a contact.

Behavior:
    First time user sends to "Saif" on WhatsApp → confirmation prompt.
    User confirms → "Saif" added to approved list.
    Next time → sends go straight through, no prompt.

The store is a small JSON file under data/. We keep it FILE-based (not in
the SQLite DB) so it's trivial to inspect, clear, or copy between machines —
this is per-user trust state, not shared application data.

Recipient normalization:
    For WhatsApp/Teams the same person can be referenced by name ("Saif"),
    phone (+92300...), or even multiple casings. We normalize to a stable
    lowercase key BUT we ALSO store the original recipient so the user can
    review their approved list later. is_approved() matches on the normalized
    key, so "saif" approved earlier matches "Saif"/"SAIF"/"saif" later.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from app.core.logging import get_logger

log = get_logger(__name__)

_STORE_PATH = Path(__file__).resolve().parents[2] / "data" / "approved_recipients.json"
_LOCK = asyncio.Lock()


def _normalize(recipient: Any) -> str:
    """Stable lowercase key for matching."""
    if recipient is None:
        return ""
    s = str(recipient).strip().lower()
    # Strip all non-alphanumeric so "+92 300 1234567" matches "923001234567"
    if re.fullmatch(r"[+\d\s\-()]+", s):
        return re.sub(r"\D", "", s)
    return s


def _load() -> dict:
    if not _STORE_PATH.exists():
        return {}
    try:
        return json.loads(_STORE_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("approved_recipients_load_failed", error=str(e))
        return {}


def _save(data: dict) -> None:
    _STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _STORE_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


class ApprovedRecipients:
    """Sync API — used from worker threads via asyncio.to_thread."""

    @staticmethod
    def is_approved(service: str, recipient: Any) -> bool:
        if not recipient:
            return False
        key = _normalize(recipient)
        if not key:
            return False
        data = _load()
        approved = data.get(service.lower(), {})
        return key in approved

    @staticmethod
    def approve(service: str, recipient: Any) -> None:
        if not recipient:
            return
        key = _normalize(recipient)
        if not key:
            return
        import time
        data = _load()
        bucket = data.setdefault(service.lower(), {})
        # Keep the original spelling for human review
        bucket[key] = {"original": str(recipient), "approved_at": int(time.time())}
        _save(data)
        log.info("recipient_approved", service=service, key=key, original=str(recipient))

    @staticmethod
    def forget(service: str, recipient: Any) -> bool:
        """Remove a single recipient from the approved list. Returns True if removed."""
        key = _normalize(recipient)
        if not key:
            return False
        data = _load()
        bucket = data.get(service.lower(), {})
        if key in bucket:
            del bucket[key]
            _save(data)
            log.info("recipient_forgotten", service=service, key=key)
            return True
        return False

    @staticmethod
    def list_all() -> dict:
        return _load()

    @staticmethod
    def clear_service(service: str) -> int:
        """Forget all approvals for one service. Returns count cleared."""
        data = _load()
        bucket = data.pop(service.lower(), {})
        if bucket:
            _save(data)
        return len(bucket)
