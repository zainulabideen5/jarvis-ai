"""Vision driver — screenshot any app, use AI to plan clicks, execute with PyAutoGUI."""

from __future__ import annotations

import asyncio
import base64
import json
import urllib.request

import mss
import pyautogui

from jarvis_agent.config import AgentConfig
from jarvis_agent.executor.drivers.base import ActionResult, BaseDriver
from jarvis_agent.utils.logging import get_logger

log = get_logger(__name__)

# Prevent PyAutoGUI from pausing and failing
pyautogui.PAUSE = 0.3
pyautogui.FAILSAFE = True

VISION_PROMPT = """You are a desktop automation assistant. Given a screenshot of a computer screen and a task to perform, output the exact steps to accomplish it.

Task: {task}

Respond with a JSON array of steps. Each step is one of:
- {{"action": "click", "x": 500, "y": 300, "description": "Click on X button"}}
- {{"action": "type", "text": "hello world", "description": "Type the message"}}
- {{"action": "press", "key": "enter", "description": "Press Enter to send"}}
- {{"action": "hotkey", "keys": ["ctrl", "a"], "description": "Select all"}}
- {{"action": "scroll", "x": 500, "y": 300, "clicks": -3, "description": "Scroll down"}}
- {{"action": "wait", "seconds": 2, "description": "Wait for page to load"}}
- {{"action": "done", "description": "Task complete"}}

IMPORTANT:
- Provide EXACT pixel coordinates based on what you see in the screenshot.
- Be precise — click on the center of buttons/fields.
- Return ONLY valid JSON array, no other text."""


class VisionDriver(BaseDriver):
    """Use screenshots + Groq Vision LLM to automate ANY desktop application.

    Takes a screenshot, sends it to Groq Llama 3.2 Vision to plan clicks,
    then executes the plan with PyAutoGUI.
    """

    def __init__(self, config: AgentConfig):
        self._config = config

    async def execute(self, task: dict, payload: dict) -> ActionResult:
        task_description = (
            payload.get("description")
            or task.get("description")
            or task.get("title", "")
        )

        if not task_description:
            return ActionResult(success=False, error="No task description for vision driver")

        groq_key = self._config.groq_api_key
        if not groq_key:
            return ActionResult(
                success=False,
                error="Groq API key not set. Set JARVIS_GROQ_API_KEY",
            )

        try:
            # Take screenshot
            screenshot_b64 = await asyncio.to_thread(self._take_screenshot)

            # Ask AI to plan the actions
            steps = await asyncio.to_thread(
                self._plan_actions, groq_key, screenshot_b64, task_description
            )

            if not steps:
                return ActionResult(success=False, error="AI returned no action steps")

            # Execute steps
            executed = await self._execute_steps(steps)

            return ActionResult(
                success=True,
                message=f"Vision driver executed {executed} steps",
                details={"steps": steps, "executed": executed},
            )
        except Exception as e:
            return ActionResult(success=False, error=f"Vision driver failed: {e}")

    @staticmethod
    def _take_screenshot() -> str:
        """Capture primary monitor and return base64-encoded PNG."""
        with mss.mss() as sct:
            monitor = sct.monitors[1]  # Primary monitor
            img = sct.grab(monitor)
            # Convert to PNG bytes
            from mss.tools import to_png
            png_bytes = to_png(img.rgb, img.size)
            return base64.b64encode(png_bytes).decode()

    @staticmethod
    def _plan_actions(api_key: str, screenshot_b64: str, task: str) -> list[dict]:
        """Send screenshot to Groq Vision and get action plan."""
        prompt = VISION_PROMPT.format(task=task)

        body = json.dumps({
            "model": "llama-3.2-90b-vision-preview",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{screenshot_b64}",
                            },
                        },
                    ],
                }
            ],
            "temperature": 0.1,
            "max_tokens": 2000,
        }).encode()

        req = urllib.request.Request(
            "https://api.groq.com/openai/v1/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())

        content = data["choices"][0]["message"]["content"].strip()

        # Parse JSON from potential markdown block
        if content.startswith("```"):
            content = content.split("\n", 1)[1].rsplit("```", 1)[0]

        steps = json.loads(content)
        if not isinstance(steps, list):
            steps = [steps]

        log.info("vision_plan", steps=len(steps))
        return steps

    async def _execute_steps(self, steps: list[dict]) -> int:
        """Execute planned steps with PyAutoGUI."""
        executed = 0

        for step in steps:
            action = step.get("action")
            desc = step.get("description", "")
            log.info("vision_step", action=action, description=desc)

            if action == "click":
                await asyncio.to_thread(
                    pyautogui.click, step["x"], step["y"]
                )
            elif action == "type":
                await asyncio.to_thread(
                    pyautogui.typewrite, step["text"], interval=0.02
                )
            elif action == "press":
                await asyncio.to_thread(pyautogui.press, step["key"])
            elif action == "hotkey":
                await asyncio.to_thread(pyautogui.hotkey, *step["keys"])
            elif action == "scroll":
                await asyncio.to_thread(
                    pyautogui.scroll, step.get("clicks", -3),
                    step.get("x"), step.get("y"),
                )
            elif action == "wait":
                await asyncio.sleep(step.get("seconds", 1))
            elif action == "done":
                executed += 1
                break
            else:
                log.warning("unknown_vision_action", action=action)
                continue

            executed += 1
            await asyncio.sleep(0.3)  # Brief pause between actions

        return executed
