"""System tray icon with status and controls."""

from __future__ import annotations

import threading
from pathlib import Path

from jarvis_agent.utils.logging import get_logger

log = get_logger(__name__)


class TrayIcon:
    """Windows system tray icon for Jarvis agent.

    Shows:
        - Green icon when connected
        - Yellow icon when reconnecting
        - Right-click menu: Open Dashboard, Pause/Resume, Quit
    """

    def __init__(self, on_quit=None, on_pause=None, dashboard_url="http://localhost:3000"):
        self._on_quit = on_quit
        self._on_pause = on_pause
        self._dashboard_url = dashboard_url
        self._paused = False
        self._icon = None
        self._thread = None

    def start(self) -> None:
        """Start the tray icon in a background thread."""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        log.info("tray_started")

    def _run(self) -> None:
        try:
            import pystray
            from PIL import Image, ImageDraw

            icon_image = self._create_icon("#22c55e")  # Green

            menu = pystray.Menu(
                pystray.MenuItem("Jarvis AI Agent", None, enabled=False),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Open Dashboard", self._open_dashboard),
                pystray.MenuItem(
                    lambda item: "Resume" if self._paused else "Pause",
                    self._toggle_pause,
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit", self._quit),
            )

            self._icon = pystray.Icon("jarvis", icon_image, "Jarvis Agent", menu)
            self._icon.run()
        except ImportError:
            log.warning("tray_unavailable", reason="pystray or Pillow not installed")
        except Exception as e:
            log.error("tray_error", error=str(e))

    @staticmethod
    def _create_icon(color: str):
        """Create a simple colored circle icon."""
        from PIL import Image, ImageDraw

        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.ellipse([8, 8, 56, 56], fill=color)
        draw.text((20, 18), "J", fill="white")
        return img

    def _open_dashboard(self, icon=None, item=None):
        import webbrowser
        webbrowser.open(self._dashboard_url)

    def _toggle_pause(self, icon=None, item=None):
        self._paused = not self._paused
        if self._on_pause:
            self._on_pause(self._paused)
        color = "#eab308" if self._paused else "#22c55e"
        if self._icon:
            self._icon.icon = self._create_icon(color)
        log.info("agent_paused" if self._paused else "agent_resumed")

    def _quit(self, icon=None, item=None):
        log.info("tray_quit_requested")
        if self._icon:
            self._icon.stop()
        if self._on_quit:
            self._on_quit()

    @property
    def paused(self) -> bool:
        return self._paused

    def update_status(self, connected: bool) -> None:
        """Update tray icon color based on connection status."""
        if self._icon:
            color = "#22c55e" if connected else "#eab308"
            self._icon.icon = self._create_icon(color)
