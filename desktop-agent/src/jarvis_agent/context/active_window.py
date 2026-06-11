"""Active window detection using Win32 APIs."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import psutil
import win32gui
import win32process

from jarvis_agent.config import AgentConfig
from jarvis_agent.utils.logging import get_logger

log = get_logger(__name__)


@dataclass
class WindowInfo:
    process_name: str
    window_title: str


class ActiveWindowMonitor:
    """Polls the active foreground window at a configurable interval."""

    def __init__(self, config: AgentConfig):
        self._interval = config.window_poll_interval_sec
        self._current: WindowInfo | None = None

    @property
    def current(self) -> WindowInfo | None:
        return self._current

    def _poll_once(self) -> WindowInfo:
        """Get current foreground window info (blocking, sub-ms)."""
        hwnd = win32gui.GetForegroundWindow()
        title = win32gui.GetWindowText(hwnd)

        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            proc = psutil.Process(pid)
            name = proc.name()
        except (psutil.NoSuchProcess, psutil.AccessDenied, Exception):
            name = "unknown"

        return WindowInfo(process_name=name, window_title=title)

    async def run(self) -> None:
        """Continuously poll the active window."""
        log.info("window_monitor_started", interval=self._interval)
        last_logged = ""

        while True:
            try:
                info = await asyncio.to_thread(self._poll_once)
                self._current = info

                # Log only on change
                key = f"{info.process_name}:{info.window_title}"
                if key != last_logged:
                    log.info(
                        "active_window_changed",
                        process=info.process_name,
                        title=info.window_title[:80],
                    )
                    last_logged = key
            except Exception as e:
                log.warning("window_poll_error", error=str(e))

            await asyncio.sleep(self._interval)
