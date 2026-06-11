"""Extension Bridge — WebSocket hub for the JARVIS Chrome extension.

The extension runs inside the user's regular Chrome (already logged in to all
their accounts) and connects here over WebSocket. When chat needs to fire
a browser action (WhatsApp send, Gmail compose, etc.), it queues a command
that gets relayed over this socket to the extension, which executes it in
a new background tab and reports back.

Why a single global bridge (not per-request socket): the extension keeps
ONE socket alive permanently. Multiple chat requests share it. A pending-
request map keyed by req_id correlates responses back to the caller.

Concurrency model:
  - At most one extension is connected at a time (one user, one Chrome)
  - Each `send_command` returns an asyncio.Future that the message handler
    resolves when the matching `result` message arrives
  - Commands time out after 60s by default; future is rejected with
    a TimeoutError

Failure modes (intentionally surfaced, not swallowed):
  - No extension connected → caller gets "extension_not_connected"
  - Command times out → caller gets "extension_timeout"
  - Extension reports failure → caller gets the extension's error verbatim
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from typing import Any

from app.core.logging import get_logger

log = get_logger(__name__)


class ExtensionBridge:
    """Singleton bridge between FastAPI and the Chrome extension."""

    _instance: "ExtensionBridge | None" = None

    def __init__(self):
        self._ws = None  # current WebSocket (FastAPI WebSocket object)
        self._pending: dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()
        self._connected_at: float = 0.0
        self._client_info: dict = {}
        # The event loop where the WebSocket is running. Captured on attach()
        # so that worker threads (e.g. asyncio.to_thread inside laptop control
        # dispatch) can schedule coroutines onto this loop via run_coroutine_threadsafe.
        # Without this, calling send_command() from a worker thread that
        # creates its own loop would never see the ws (it lives on the main loop).
        self._main_loop: asyncio.AbstractEventLoop | None = None

    @classmethod
    def get(cls) -> "ExtensionBridge":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ---------- state ----------

    def is_connected(self) -> bool:
        return self._ws is not None

    def status(self) -> dict:
        return {
            "connected": self.is_connected(),
            "connected_at": self._connected_at,
            "pending_count": len(self._pending),
            "client": self._client_info,
        }

    # ---------- lifecycle (called from the WS endpoint) ----------

    async def attach(self, ws) -> None:
        """Register an incoming WebSocket as the active extension."""
        async with self._lock:
            # If another extension was connected, drop it — only one bridge at a time
            if self._ws is not None:
                try:
                    await self._ws.close()
                except Exception:
                    pass
                # Reject any pending requests left over
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_exception(RuntimeError("extension_replaced"))
                self._pending.clear()
            self._ws = ws
            self._connected_at = time.time()
            # Capture the loop the ws is bound to so worker threads can
            # schedule commands across the thread boundary.
            try:
                self._main_loop = asyncio.get_running_loop()
            except RuntimeError:
                self._main_loop = None
        log.info("extension_attached")

    async def detach(self) -> None:
        async with self._lock:
            self._ws = None
            self._connected_at = 0.0
            self._client_info = {}
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(RuntimeError("extension_disconnected"))
            self._pending.clear()
        log.info("extension_detached")

    async def handle_message(self, msg: dict) -> None:
        """Process one inbound message from the extension."""
        mtype = msg.get("type", "")
        if mtype == "hello":
            self._client_info = {
                "client": msg.get("client"),
                "version": msg.get("version"),
            }
            log.info("extension_hello", **self._client_info)
            return
        if mtype == "ping":
            await self._safe_send({"type": "pong", "req_id": msg.get("req_id"), "ts": msg.get("ts")})
            return
        if mtype == "pong":
            return
        if mtype == "result":
            req_id = msg.get("req_id")
            fut = self._pending.pop(req_id, None) if req_id else None
            if fut and not fut.done():
                if msg.get("ok"):
                    fut.set_result(msg.get("result"))
                else:
                    fut.set_exception(RuntimeError(msg.get("error") or "extension_error"))
            else:
                log.debug("orphan_result", req_id=req_id)
            return
        log.warning("unknown_ext_message", mtype=mtype)

    # ---------- outbound commands ----------

    async def send_command(self, action: str, params: dict | None = None, *, timeout: float = 60.0) -> Any:
        """Send a command and wait for the extension's response.

        Raises RuntimeError if the extension is not connected, or asyncio.TimeoutError
        if the response doesn't arrive in `timeout` seconds.
        """
        if not self.is_connected():
            raise RuntimeError("extension_not_connected")

        req_id = secrets.token_urlsafe(12)
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[req_id] = fut

        payload = {
            "type": "command",
            "req_id": req_id,
            "action": action,
            "params": params or {},
        }

        ok = await self._safe_send(payload)
        if not ok:
            self._pending.pop(req_id, None)
            raise RuntimeError("extension_send_failed")

        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise
        finally:
            self._pending.pop(req_id, None)

    async def _safe_send(self, payload: dict) -> bool:
        ws = self._ws
        if ws is None:
            return False
        try:
            await ws.send_text(json.dumps(payload))
            return True
        except Exception as e:
            log.warning("ext_send_failed", error=str(e))
            return False

    def send_command_sync(self, action: str, params: dict | None = None, *, timeout: float = 60.0) -> Any:
        """Sync wrapper for use from worker threads (asyncio.to_thread context).

        The WebSocket lives on the main event loop. Worker threads can't
        directly await on a coroutine bound to that loop — they have to
        schedule it cross-thread. This wraps run_coroutine_threadsafe.
        """
        if self._main_loop is None or not self.is_connected():
            raise RuntimeError("extension_not_connected")
        import concurrent.futures
        fut = asyncio.run_coroutine_threadsafe(
            self.send_command(action, params, timeout=timeout),
            self._main_loop,
        )
        try:
            # Give a little headroom over the inner timeout for scheduling overhead
            return fut.result(timeout=timeout + 5)
        except concurrent.futures.TimeoutError:
            raise asyncio.TimeoutError("send_command_sync_timeout")
