"""SQLite-backed offline queue — stores chunks when the server is unreachable."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from jarvis_agent.config import AgentConfig
from jarvis_agent.utils.logging import get_logger

log = get_logger(__name__)


class OfflineQueue:
    """Persists audio chunk data to SQLite when the server is down.

    On reconnect, the ws_client drains this queue before sending live chunks.
    """

    def __init__(self, config: AgentConfig):
        db_path = config.data_dir / "offline_queue.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chunk_id TEXT UNIQUE,
                metadata_json TEXT NOT NULL,
                audio_data BLOB NOT NULL,
                created_at REAL NOT NULL
            )
        """)
        self._conn.commit()

        count = self._conn.execute("SELECT COUNT(*) FROM pending_chunks").fetchone()[0]
        if count > 0:
            log.info("offline_queue_loaded", pending=count)

    def enqueue(self, chunk_id: str, metadata_json: str, audio_data: bytes) -> None:
        """Store a chunk for later delivery."""
        try:
            self._conn.execute(
                "INSERT OR IGNORE INTO pending_chunks (chunk_id, metadata_json, audio_data, created_at) VALUES (?, ?, ?, ?)",
                (chunk_id, metadata_json, audio_data, time.time()),
            )
            self._conn.commit()
            log.debug("chunk_queued_offline", chunk_id=chunk_id[:8])
        except Exception as e:
            log.error("offline_enqueue_failed", error=str(e))

    def peek_batch(self, limit: int = 10) -> list[tuple[int, str, str, bytes]]:
        """Get oldest pending chunks without removing them.

        Returns list of (id, chunk_id, metadata_json, audio_data).
        """
        cursor = self._conn.execute(
            "SELECT id, chunk_id, metadata_json, audio_data FROM pending_chunks ORDER BY id ASC LIMIT ?",
            (limit,),
        )
        return cursor.fetchall()

    def remove(self, row_id: int) -> None:
        """Remove a successfully sent chunk."""
        self._conn.execute("DELETE FROM pending_chunks WHERE id = ?", (row_id,))
        self._conn.commit()

    @property
    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM pending_chunks").fetchone()[0]

    def close(self) -> None:
        self._conn.close()
