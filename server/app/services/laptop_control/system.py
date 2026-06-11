"""System commands — volume, wifi, shutdown, lock."""

from __future__ import annotations

import subprocess

from app.core.logging import get_logger

log = get_logger(__name__)


class SystemCommands:
    """System-level commands. Safety: destructive ones require confirmation."""

    @staticmethod
    def execute(command: str) -> tuple[bool, str]:
        """Execute a system command."""
        cmd = command.lower().strip()

        try:
            if cmd == "volume_up":
                import pyautogui
                for _ in range(5):
                    pyautogui.press("volumeup")
                return True, "Volume badha diya"

            if cmd == "volume_down":
                import pyautogui
                for _ in range(5):
                    pyautogui.press("volumedown")
                return True, "Volume kam kar diya"

            if cmd == "volume_mute":
                import pyautogui
                pyautogui.press("volumemute")
                return True, "Volume mute"

            if cmd == "lock":
                subprocess.Popen(["rundll32.exe", "user32.dll,LockWorkStation"])
                return True, "Laptop lock ho gaya"

            if cmd == "sleep":
                subprocess.Popen(
                    ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"]
                )
                return True, "Laptop sleep mein"

            if cmd == "shutdown":
                subprocess.Popen(["shutdown", "/s", "/t", "60"])
                return True, "Laptop 1 min baad band ho jayega — rokne ke liye 'shutdown /a' bolo"

            if cmd == "restart":
                subprocess.Popen(["shutdown", "/r", "/t", "60"])
                return True, "Laptop 1 min baad restart — rokne ke liye 'shutdown /a' bolo"

            if cmd == "cancel_shutdown":
                subprocess.Popen(["shutdown", "/a"])
                return True, "Shutdown cancel"

            if cmd == "wifi_off":
                subprocess.Popen(
                    ["netsh", "interface", "set", "interface", "Wi-Fi", "admin=disable"],
                    shell=True,
                )
                return True, "WiFi band"

            if cmd == "wifi_on":
                subprocess.Popen(
                    ["netsh", "interface", "set", "interface", "Wi-Fi", "admin=enable"],
                    shell=True,
                )
                return True, "WiFi chalu"

            return False, f"Unknown system command: {cmd}"

        except Exception as e:
            return False, f"Command failed: {e}"
