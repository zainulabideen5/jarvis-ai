"""Task extraction service using Groq LLM — bulletproof, no crashes."""

from __future__ import annotations

import json
import re

from app.core.llm import LLMClient

from app.core.config import ServerConfig
from app.core.logging import get_logger

log = get_logger(__name__)

EXTRACTION_PROMPT = """Tu ek task extraction AI hai. Neeche conversation transcript hai. Ismein se actionable tasks nikal.

Har task ke liye JSON object bana:
- "title": Chhota title (max 100 chars)
- "description": Detail
- "assigned_to": Kis ko karna hai (ya null)
- "priority": "low", "medium", "high", ya "urgent"
- "action_type": "send_message", "create_doc", "schedule_meeting", "reminder", ya "other"

Rules:
- Sirf CLEAR tasks nikal — "Ahmed ko bol do logo banao" = task
- Casual baat se task mat bana — "acha theek hai" = no task
- Roman Urdu / Hindi / English sab samajh
- "kal tak" = urgent, "next week" = medium
- Agar koi task nahi to empty array [] return kar
- SIRF valid JSON array return kar, koi aur text nahi

Transcript:
__TRANSCRIPT__

Context: __WINDOW__

JSON array:"""


class TaskExtractor:
    """Extracts tasks from transcriptions using Groq LLM."""

    def __init__(self, config: ServerConfig):
        self._config = config
        self._client = None

    def _get_client(self) -> LLMClient:
        if self._client is None:
            if not self._config.groq_api_key:
                raise ValueError("JARVIS_GROQ_API_KEY not set")
            self._client = LLMClient(self._config)
        return self._client

    # Phrases that indicate a transcript is Whisper noise / prompt leak rather
    # than real meeting content. If a chunk's transcript matches, we skip
    # task extraction entirely to avoid hallucinated tasks.
    _NOISE_MARKERS = (
        "transcribe everything",
        "english letters only",
        "roman alphabet",
        ".com.",
        ".com,",
    )

    @staticmethod
    def _looks_like_noise(text: str) -> bool:
        low = (text or "").lower()
        if any(m in low for m in TaskExtractor._NOISE_MARKERS):
            return True
        # If half or more of the "words" are very short or non-alphabetic, treat as noise
        words = [w for w in re.findall(r"[A-Za-z]+", low) if w]
        if len(words) >= 4 and sum(1 for w in words if len(w) <= 2) / len(words) > 0.5:
            return True
        return False

    def extract_tasks(
        self, transcript: str, window_context: str | None = None
    ) -> list[dict]:
        """Extract tasks from transcript. Never crashes — returns [] on any error."""
        if not transcript or len(transcript.strip()) < 25:
            return []

        # Skip clearly-noisy transcripts so the LLM doesn't hallucinate tasks
        # from garbled Whisper output.
        if TaskExtractor._looks_like_noise(transcript):
            log.debug("task_extraction_skipped_noise", text=transcript[:60])
            return []

        client = self._get_client()
        prompt = EXTRACTION_PROMPT.replace(
            "__TRANSCRIPT__", transcript[:2000]
        ).replace(
            "__WINDOW__", window_context or "Unknown"
        )

        try:
            response = client.chat.completions.create(
                model=self._config.groq_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=2000,
            )

            content = response.choices[0].message.content.strip()
            tasks = self._parse_json(content)

            if tasks:
                log.info("tasks_extracted", count=len(tasks))
            return tasks

        except Exception as e:
            log.warning("task_extraction_error", error=str(e))
            return []

    @staticmethod
    def _parse_json(content: str) -> list[dict]:
        """Parse JSON from LLM response — handles all edge cases."""
        # Remove markdown code blocks
        if "```" in content:
            # Extract content between ``` blocks
            match = re.search(r'```(?:json)?\s*(.*?)```', content, re.DOTALL)
            if match:
                content = match.group(1).strip()

        # Try direct parse
        try:
            result = json.loads(content)
            if isinstance(result, list):
                return [t for t in result if isinstance(t, dict) and t.get("title")]
            if isinstance(result, dict) and result.get("title"):
                return [result]
            return []
        except json.JSONDecodeError:
            pass

        # Try finding JSON array in text
        match = re.search(r'\[.*\]', content, re.DOTALL)
        if match:
            try:
                result = json.loads(match.group())
                if isinstance(result, list):
                    return [t for t in result if isinstance(t, dict) and t.get("title")]
            except json.JSONDecodeError:
                pass

        # Try finding JSON object in text
        match = re.search(r'\{.*\}', content, re.DOTALL)
        if match:
            try:
                result = json.loads(match.group())
                if isinstance(result, dict) and result.get("title"):
                    return [result]
            except json.JSONDecodeError:
                pass

        return []
