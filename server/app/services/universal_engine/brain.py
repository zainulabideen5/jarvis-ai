"""Brain — the ONE swappable LLM layer for the universal engine.

Currently Claude CLI (`claude -p` headless) for testing. To switch to the
Anthropic API later, add an `AnthropicAPIBrain` with the same `think()`
signature and flip `get_brain()` — nothing else in the engine changes.
"""

from __future__ import annotations

import json
import shutil
import subprocess

from app.core.logging import get_logger

log = get_logger(__name__)

# Per-call timeout. Claude CLI cold-start + a long ui_tree can take a while.
THINK_TIMEOUT_SEC = 180


class BrainError(Exception):
    """Brain could not produce a usable response."""


class ClaudeCLIBrain:
    """Talks to the locally installed Claude Code CLI in headless mode.

    Each think() call sends the full conversation as one prompt and gets
    a single completion back (no CLI-side tools, no agentic turns).
    """

    def __init__(self) -> None:
        self._exe = self._find_cli()

    @staticmethod
    def _find_cli() -> str | None:
        # npm shim on Windows is claude.cmd; plain `claude` covers PATH setups.
        for name in ("claude.cmd", "claude"):
            path = shutil.which(name)
            if path:
                return path
        return None

    def is_available(self) -> bool:
        return self._exe is not None

    def think(self, system: str, transcript: list[dict]) -> str:
        """Run one completion. transcript = [{role, content}, ...].

        Returns the raw model text (engine parses the JSON protocol).
        """
        if not self._exe:
            raise BrainError(
                "Claude CLI nahi mila. Install: npm install -g @anthropic-ai/claude-code"
            )

        prompt = self._build_prompt(transcript)
        try:
            proc = subprocess.run(
                [
                    self._exe, "-p",
                    "--output-format", "json",
                    # 3 turns: if the model slips and calls a (denied) tool,
                    # the denial bounces it back to plain text within budget.
                    "--max-turns", "3",
                    "--system-prompt", system,
                    # The brain only thinks; our engine executes. Built-in
                    # tools off; account-level MCP connectors (Gmail etc.)
                    # can't be unloaded headlessly, so deny them wholesale.
                    "--tools", "",
                    "--strict-mcp-config",
                    "--disallowedTools", "mcp__*",
                ],
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=THINK_TIMEOUT_SEC,
                shell=False,
            )
        except subprocess.TimeoutExpired:
            raise BrainError(f"Claude CLI timeout ({THINK_TIMEOUT_SEC}s)")
        except OSError as e:
            raise BrainError(f"Claude CLI launch fail: {e}")

        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()[:400]
            raise BrainError(f"Claude CLI exit {proc.returncode}: {err}")

        raw = (proc.stdout or "").strip()
        try:
            payload = json.loads(raw)
            result = payload.get("result", "")
        except json.JSONDecodeError:
            # CLI printed plain text (older versions / config differences)
            result = raw

        if not result or not str(result).strip():
            raise BrainError("Claude CLI ne khali jawab diya")
        return str(result).strip()

    @staticmethod
    def _build_prompt(transcript: list[dict]) -> str:
        """Flatten history into the user prompt (system goes via flag)."""
        parts = []
        for msg in transcript:
            role = msg.get("role", "user")
            label = "Pichla step" if role == "assistant" else "Input"
            parts.append(f"{label}: {msg.get('content', '')}")
        parts.append("")
        parts.append("Agla step kya hai? Sirf ek JSON object do.")
        return "\n".join(parts)


class LLMBrain:
    """Brain backed by the server's existing fast LLM stack (Groq → Cerebras
    → OpenRouter → Gemini fallback). Default brain: NO new API key needed,
    and ~10x faster per step than the Claude CLI (sub-second vs 15-20s), which
    is what makes the engine actually usable interactively.
    """

    def __init__(self) -> None:
        from app.core.config import ServerConfig
        from app.core.llm import LLMClient
        self._config = ServerConfig()
        self._client = LLMClient(self._config)

    def is_available(self) -> bool:
        # Available if any provider key is configured.
        return bool(
            self._config.groq_api_keys or self._config.cerebras_api_keys
            or self._config.openrouter_api_keys or self._config.gemini_api_keys
        )

    def think(self, system: str, transcript: list[dict]) -> str:
        messages = [{"role": "system", "content": system}, *transcript]
        resp = self._client.chat.completions.create(
            model=self._config.groq_model,
            messages=messages,
            temperature=0.2,   # planning — keep it deterministic
            max_tokens=900,
            timeout=40,
        )
        text = (resp.choices[0].message.content or "").strip()
        if not text:
            raise BrainError("LLM ne khali jawab diya")
        return text


_brain = None


def get_brain():
    """Singleton brain.

    Default = LLMBrain (fast Groq stack, already configured). Falls back to the
    Claude CLI only if no LLM keys are set. To force the API/CLI later, swap
    the instance created here — nothing else in the engine changes.
    """
    global _brain
    if _brain is None:
        llm = LLMBrain()
        _brain = llm if llm.is_available() else ClaudeCLIBrain()
    return _brain
