"""WhatsApp Web text reader — captures recent messages from the active chat."""

from __future__ import annotations

import asyncio
import re

from jarvis_agent.config import AgentConfig
from jarvis_agent.utils.logging import get_logger

log = get_logger(__name__)


class WhatsAppReader:
    """Reads recent messages from WhatsApp Web when it's the active window.

    Uses accessibility APIs (via pygetwindow + UI Automation) to read
    visible text from the WhatsApp Web window without Playwright.
    Falls back to clipboard-based reading if UI Automation is unavailable.
    """

    def __init__(self, config: AgentConfig):
        self._config = config
        self._last_messages: list[str] = []

    async def read_active_chat(self) -> list[str]:
        """Read visible messages from the active WhatsApp Web chat.

        Returns list of message strings, newest last.
        """
        try:
            messages = await asyncio.to_thread(self._read_via_uia)
            if messages != self._last_messages:
                self._last_messages = messages
                log.debug("whatsapp_messages_read", count=len(messages))
            return messages
        except Exception as e:
            log.debug("whatsapp_read_failed", error=str(e))
            return []

    @staticmethod
    def _read_via_uia() -> list[str]:
        """Read WhatsApp Web messages using Windows UI Automation."""
        import ctypes
        import ctypes.wintypes

        # Find the WhatsApp Web window (Chrome/Edge with WhatsApp title)
        import win32gui

        def find_whatsapp_window():
            result = []
            def callback(hwnd, _):
                title = win32gui.GetWindowText(hwnd)
                if "WhatsApp" in title and win32gui.IsWindowVisible(hwnd):
                    result.append(hwnd)
            win32gui.EnumWindows(callback, None)
            return result[0] if result else None

        hwnd = find_whatsapp_window()
        if not hwnd:
            return []

        # Use clipboard approach: select all text in the chat area
        # This is the most reliable cross-browser approach
        import pyperclip
        import pyautogui

        # Store original clipboard
        try:
            original = pyperclip.paste()
        except Exception:
            original = ""

        try:
            # Focus the window
            win32gui.SetForegroundWindow(hwnd)
            import time
            time.sleep(0.2)

            # Ctrl+A to select all, Ctrl+C to copy
            pyautogui.hotkey('ctrl', 'a')
            time.sleep(0.1)
            pyautogui.hotkey('ctrl', 'c')
            time.sleep(0.1)
            pyautogui.press('escape')  # Deselect

            text = pyperclip.paste()

            # Restore clipboard
            try:
                pyperclip.copy(original)
            except Exception:
                pass

            if not text or text == original:
                return []

            # Parse messages (WhatsApp Web format: "[time] sender: message")
            lines = text.strip().split('\n')
            messages = []
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                # Remove timestamp patterns like [10:30 AM] or [10:30]
                cleaned = re.sub(r'^\[\d{1,2}:\d{2}(?:\s*[APap][Mm])?\]\s*', '', line)
                if cleaned:
                    messages.append(cleaned)

            return messages[-20:]  # Last 20 messages

        except Exception:
            try:
                pyperclip.copy(original)
            except Exception:
                pass
            return []

    def get_context_string(self) -> str:
        """Get a summary of recent WhatsApp messages for context enrichment."""
        if not self._last_messages:
            return ""
        return "WhatsApp recent: " + " | ".join(self._last_messages[-5:])
