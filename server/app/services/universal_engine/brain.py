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
THINK_TIMEOUT_SEC = 45   # CLI cold-start ~8-15s; if it hangs, fail fast & retry


class BrainError(Exception):
    """Brain could not produce a usable response."""


class ClaudeCLIBrain:
    """Talks to the locally installed Claude Code CLI in headless mode.

    Each think() call sends the full conversation as one prompt and gets
    a single completion back (no CLI-side tools, no agentic turns).
    """

    def __init__(self, model: str = "opus") -> None:
        self._exe = self._find_cli()
        self._model = model

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
        """Engine completion — JSON-protocol suffix lagta hai (engine JSON parse
        karta hai). transcript = [{role, content}, ...]."""
        return self._run_cli(system, self._build_prompt(transcript, json_protocol=True))

    def ask(self, system: str, transcript: list[dict]) -> str:
        """PLAIN PROSE answer — koi JSON-protocol suffix NAHI. Chat replies aur
        web-search synthesis isko use karte hain taake Claude natural text de,
        na ke {"reply":...} / {"next_step":...} JSON."""
        return self._run_cli(system, self._build_prompt(transcript, json_protocol=False))

    def _run_cli(self, system: str, prompt: str) -> str:
        if not self._exe:
            raise BrainError(
                "Claude CLI nahi mila. Install: npm install -g @anthropic-ai/claude-code"
            )
        # The system prompt (full playbook) is large — passing it on the command
        # line hits Windows' "command line too long" limit. Write it to a temp
        # file and use --system-prompt-file instead.
        import os
        import tempfile
        sys_file = None
        try:
            fd, sys_file = tempfile.mkstemp(suffix=".txt", prefix="jarvis_sys_")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(system)
        except Exception as e:
            raise BrainError(f"system prompt temp file fail: {e}")

        try:
            proc = subprocess.run(
                [
                    self._exe, "-p",
                    "--model", self._model,           # Opus 4.8 (smart)
                    "--output-format", "json",
                    # 3 turns: if the model slips and calls a (denied) tool,
                    # the denial bounces it back to plain text within budget.
                    "--max-turns", "3",
                    "--system-prompt-file", sys_file,
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
        finally:
            if sys_file:
                try:
                    os.remove(sys_file)
                except OSError:
                    pass

        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()[:400]
            raise BrainError(f"Claude CLI exit {proc.returncode}: {err}")

        raw = (proc.stdout or "").strip()
        try:
            payload = json.loads(raw)
            # CLI exit 0 but API/quota error (e.g. 429 "out of extra usage") →
            # treat as failure so the fallback brain takes over.
            if payload.get("is_error") or payload.get("api_error_status"):
                msg = (payload.get("result") or payload.get("api_error_status")
                       or "unknown error")
                raise BrainError(f"Claude CLI API error: {str(msg)[:200]}")
            result = payload.get("result", "")
        except json.JSONDecodeError:
            # CLI printed plain text (older versions / config differences)
            result = raw

        if not result or not str(result).strip():
            raise BrainError("Claude CLI ne khali jawab diya")
        return str(result).strip()

    @staticmethod
    def _build_prompt(transcript: list[dict], json_protocol: bool = True) -> str:
        """Flatten history into the user prompt (system goes via flag).
        json_protocol=True → engine ko "ek JSON object do" suffix (think). False →
        plain prose (ask), koi JSON forcing nahi."""
        parts = []
        for msg in transcript:
            role = msg.get("role", "user")
            label = "Pichla step" if (role == "assistant" and json_protocol) else (
                "Assistant" if role == "assistant" else "Input")
            parts.append(f"{label}: {msg.get('content', '')}")
        parts.append("")
        if json_protocol:
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

    # Plain-prose answer — chat replies/web-synthesis. LLMBrain ke liye think aur
    # ask same hain (format system prompt se aata hai, koi JSON forcing nahi).
    def ask(self, system: str, transcript: list[dict]) -> str:
        return self.think(system, transcript)


class FallbackBrain:
    """Claude CLI PRIMARY (smartest); runtime pe woh fail/429/timeout ho to
    SILENTLY Groq/Cerebras/Gemini stack pe gir jaata hai — taake brain ka quota
    khatam hone par bhi JARVIS chalta rahe. Policy: Claude primary, baqi backup.
    """

    def __init__(self) -> None:
        self._primary = ClaudeCLIBrain(model="opus")
        self._backup = None          # lazy init (key na ho to None)
        self._backup_tried = False

    def _get_backup(self):
        if not self._backup_tried:
            self._backup_tried = True
            try:
                b = LLMBrain()
                self._backup = b if b.is_available() else None
            except Exception as e:
                log.warning("backup_brain_init_fail", error=str(e)[:150])
                self._backup = None
        return self._backup

    def is_available(self) -> bool:
        if self._primary.is_available():
            return True
        b = self._get_backup()
        return bool(b and b.is_available())

    def _call(self, method: str, system: str, transcript: list[dict]) -> str:
        # 1) Primary: Claude CLI (agar installed)
        if self._primary.is_available():
            try:
                return getattr(self._primary, method)(system, transcript)
            except BrainError as e:
                log.warning("brain_primary_failed_fallback",
                            method=method, error=str(e)[:200])
        # 2) Backup: Groq/Cerebras/Gemini stack
        b = self._get_backup()
        if b and b.is_available():
            fn = getattr(b, method, None) or b.think
            return fn(system, transcript)
        raise BrainError(
            "Claude CLI fail (quota/429?) aur koi backup LLM key configured nahi. "
            "Backup ke liye .env mein GROQ/CEREBRAS/GEMINI key daalo."
        )

    def think(self, system: str, transcript: list[dict]) -> str:
        return self._call("think", system, transcript)

    def ask(self, system: str, transcript: list[dict]) -> str:
        return self._call("ask", system, transcript)


# Which brain to use. Flip this one line to switch — nothing else changes.
#   "cli"  → Claude CLI (Opus 4.8): smartest, but ~10-15s/step cold start
#   "groq" → fast Groq/Cerebras stack (~1s/step), slightly less smart
#   "api"  → (future) Anthropic API: fast + smart, needs paid key
BRAIN_CHOICE = "cli"

_brain = None


def get_brain():
    """Singleton brain. BRAIN_CHOICE selects the implementation."""
    global _brain
    if _brain is None:
        if BRAIN_CHOICE == "cli":
            # Claude CLI primary + automatic Groq/Cerebras backup on runtime
            # failure (429/quota/timeout) — not just when CLI is missing.
            _brain = FallbackBrain()
        else:
            llm = LLMBrain()
            _brain = llm if llm.is_available() else ClaudeCLIBrain(model="opus")
    return _brain
