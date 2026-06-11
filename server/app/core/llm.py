"""Unified LLM client — Groq primary, Gemini fallback. Drop-in replacement for Groq client."""

from __future__ import annotations

from typing import Any

from groq import Groq

from app.core.config import ServerConfig
from app.core.logging import get_logger

log = get_logger(__name__)


class _Message:
    def __init__(self, content: str):
        self.content = content


class _Choice:
    def __init__(self, content: str):
        self.message = _Message(content)


class _Response:
    """Mimics Groq/OpenAI response shape so existing code works unchanged."""
    def __init__(self, content: str, provider: str = "groq"):
        self.choices = [_Choice(content)]
        self.provider = provider


class _ChatCompletions:
    def __init__(self, parent: "LLMClient"):
        self._parent = parent

    def create(
        self,
        model: str,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int = 1000,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> _Response:
        return self._parent._create_completion(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
        )


class _Chat:
    def __init__(self, parent: "LLMClient"):
        self.completions = _ChatCompletions(parent)


class LLMClient:
    """
    Drop-in replacement for Groq client. 3-tier fallback:
    1. Groq (fastest, 100k tokens/day)
    2. Cerebras (super-fast Llama, 8000 msgs/day)
    3. Gemini (last resort, ~200 req/day)
    Same interface as Groq, services don't need changes.
    """

    def __init__(self, config: ServerConfig):
        self._config = config
        # Multi-key pools. Each entry: {"key": str, "client": cached or None,
        # "disabled_until": float (monotonic seconds; 0 = available)}.
        # First key in the list is preferred; we walk forward on quota errors.
        self._groq_pool = [
            {"key": k, "client": None, "disabled_until": 0.0}
            for k in (config.groq_api_keys or [])
        ]
        self._cerebras_pool = [
            {"key": k, "client": None, "disabled_until": 0.0}
            for k in (config.cerebras_api_keys or [])
        ]
        self._openrouter_pool = [
            {"key": k, "client": None, "disabled_until": 0.0}
            for k in (config.openrouter_api_keys or [])
        ]
        self._gemini_pool = [
            {"key": k, "client": None, "disabled_until": 0.0}
            for k in (config.gemini_api_keys or [])
        ]
        self.chat = _Chat(self)

    @property
    def audio(self):
        """Whisper audio transcription — only Groq supports this. Returns the
        audio API of the first available Groq client."""
        client = self._first_available_groq()
        if client is None:
            raise ValueError("No Groq key available for audio")
        return client.audio

    def _first_available_groq(self) -> Groq | None:
        """Return the next non-cooled Groq client, materializing it on demand."""
        import time
        now = time.monotonic()
        for slot in self._groq_pool:
            if now < slot["disabled_until"]:
                continue
            if slot["client"] is None:
                slot["client"] = Groq(api_key=slot["key"])
            return slot["client"]
        return None

    def _first_available_cerebras(self):
        import time
        now = time.monotonic()
        for slot in self._cerebras_pool:
            if now < slot["disabled_until"]:
                continue
            if slot["client"] is None:
                try:
                    from openai import OpenAI
                    slot["client"] = OpenAI(
                        api_key=slot["key"],
                        base_url="https://api.cerebras.ai/v1",
                    )
                except ImportError:
                    return None
            return slot["client"]
        return None

    def _first_available_gemini_key(self) -> str | None:
        """Returns the next non-cooled Gemini API key (raw string). The
        google.generativeai module is configured per-call rather than per-client.
        """
        import time
        now = time.monotonic()
        for slot in self._gemini_pool:
            if now < slot["disabled_until"]:
                continue
            return slot["key"]
        return None

    def _is_quota_error(self, err: Exception) -> bool:
        s = str(err).lower()
        return any(x in s for x in ("rate", "quota", "429", "limit", "exceeded"))

    def _create_completion(
        self,
        model: str,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
        timeout: float | None,
    ) -> _Response:
        import time

        # Tier 1: Groq — walk the key pool, skipping cooled-down keys
        for slot in self._groq_pool:
            now = time.monotonic()
            if now < slot["disabled_until"]:
                continue
            if slot["client"] is None:
                slot["client"] = Groq(api_key=slot["key"])
            try:
                kwargs = {
                    "model": model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                }
                if timeout is not None:
                    kwargs["timeout"] = timeout
                response = slot["client"].chat.completions.create(**kwargs)
                content = response.choices[0].message.content or ""
                return _Response(content, provider="groq")
            except Exception as e:
                err_str = str(e).lower()
                if self._is_quota_error(e):
                    # Distinguish per-minute rate limit (short) vs daily quota
                    # exhaustion (long). Per-minute messages mention "minute"
                    # or "requests per". Daily ones mention "daily" or just
                    # the standard rate-limit text. Default to short cooldown
                    # — we'd rather retry a key that was briefly limited than
                    # mark it dead for 24h.
                    is_daily = ("daily" in err_str or "tokens per day" in err_str)
                    cooldown = (24 * 3600) if is_daily else 70
                    slot["disabled_until"] = time.monotonic() + cooldown
                    log.info(
                        "groq_key_rate_limited",
                        key_suffix=slot["key"][-6:],
                        cooldown_seconds=cooldown,
                        error=str(e)[:100],
                    )
                    continue  # try next key in the pool
                log.warning("groq_request_failed", error=str(e)[:200])
                break  # non-quota error — don't burn through all keys

        # Tier 2: Cerebras — same multi-key walk
        for slot in self._cerebras_pool:
            now = time.monotonic()
            if now < slot["disabled_until"]:
                continue
            try:
                return self._cerebras_completion_with_slot(
                    slot, messages, temperature, max_tokens, timeout,
                )
            except Exception as e:
                err_str = str(e).lower()
                if "high traffic" in err_str or "try again" in err_str:
                    slot["disabled_until"] = time.monotonic() + 30
                    log.info("cerebras_key_busy_short_cooldown", key_suffix=slot["key"][-6:])
                    continue
                if self._is_quota_error(e):
                    is_daily = ("daily" in err_str or "tokens per day" in err_str)
                    slot["disabled_until"] = time.monotonic() + ((24 * 3600) if is_daily else 70)
                    log.info("cerebras_key_rate_limited", key_suffix=slot["key"][-6:])
                    continue
                log.warning("cerebras_request_failed", error=str(e)[:200])
                break

        # Tier 3: OpenRouter — free models pool
        for slot in self._openrouter_pool:
            now = time.monotonic()
            if now < slot["disabled_until"]:
                continue
            try:
                return self._openrouter_completion_with_slot(
                    slot, messages, temperature, max_tokens, timeout,
                )
            except Exception as e:
                err_str = str(e).lower()
                if self._is_quota_error(e):
                    is_daily = ("daily" in err_str or "tokens per day" in err_str)
                    slot["disabled_until"] = time.monotonic() + ((24 * 3600) if is_daily else 70)
                    log.info("openrouter_key_rate_limited", key_suffix=slot["key"][-6:])
                    continue
                log.warning("openrouter_request_failed", error=str(e)[:200])
                break

        # Tier 4: Gemini — multi-key walk
        for slot in self._gemini_pool:
            now = time.monotonic()
            if now < slot["disabled_until"]:
                continue
            try:
                return self._gemini_completion_with_key(
                    slot["key"], messages, temperature, max_tokens,
                )
            except Exception as e:
                err_str = str(e).lower()
                if self._is_quota_error(e):
                    is_daily = ("daily" in err_str or "tokens per day" in err_str)
                    slot["disabled_until"] = time.monotonic() + ((24 * 3600) if is_daily else 70)
                    log.info("gemini_key_rate_limited", key_suffix=slot["key"][-6:])
                    continue
                log.warning("gemini_request_failed", error=str(e)[:200])
                break

        raise RuntimeError(
            "All LLM providers exhausted (Groq, Cerebras, Gemini) — "
            "add more keys or wait for quota reset."
        )

    def _openrouter_completion_with_slot(
        self,
        slot: dict,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
        timeout: float | None,
    ) -> _Response:
        if slot["client"] is None:
            try:
                from openai import OpenAI
                slot["client"] = OpenAI(
                    api_key=slot["key"],
                    base_url="https://openrouter.ai/api/v1",
                    default_headers={
                        # OpenRouter likes these for free-tier identification
                        "HTTP-Referer": "http://localhost:8000",
                        "X-Title": "JARVIS",
                    },
                )
            except ImportError:
                raise RuntimeError("openai package missing for OpenRouter")
        client = slot["client"]

        kwargs = {
            "model": self._config.openrouter_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": min(max_tokens, 4096),
        }
        if timeout is not None:
            kwargs["timeout"] = timeout

        response = client.chat.completions.create(**kwargs)
        content = response.choices[0].message.content or ""
        log.info("openrouter_completion_used", model=self._config.openrouter_model, chars=len(content))
        return _Response(content, provider="openrouter")

    def _cerebras_completion_with_slot(
        self,
        slot: dict,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
        timeout: float | None,
    ) -> _Response:
        if slot["client"] is None:
            try:
                from openai import OpenAI
                slot["client"] = OpenAI(
                    api_key=slot["key"],
                    base_url="https://api.cerebras.ai/v1",
                )
            except ImportError:
                raise RuntimeError("openai package missing for Cerebras")
        client = slot["client"]
        capped_tokens = min(max_tokens, 8000)

        # Try primary model first, fallback to smaller models on "high traffic"
        models = [self._config.cerebras_model]
        if self._config.cerebras_model != "llama3.1-8b":
            models.append("llama3.1-8b")  # Always-available smaller fallback

        last_error = None
        for model in models:
            try:
                kwargs = {
                    "model": model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": capped_tokens,
                }
                if timeout is not None:
                    kwargs["timeout"] = timeout

                response = client.chat.completions.create(**kwargs)
                content = response.choices[0].message.content or ""
                log.info("cerebras_completion_used", model=model, chars=len(content))
                return _Response(content, provider="cerebras")
            except Exception as e:
                err_str = str(e).lower()
                last_error = e
                if "high traffic" in err_str or "try again" in err_str:
                    log.info("cerebras_model_busy_trying_fallback", model=model)
                    continue
                # Non-traffic error — don't retry
                raise

        raise last_error if last_error else RuntimeError("Cerebras all models failed")

    def _gemini_completion_with_key(
        self,
        api_key: str,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
    ) -> _Response:
        try:
            import google.generativeai as genai
            genai.configure(api_key=api_key)

            # Convert OpenAI-style messages to Gemini format
            system_text = ""
            user_parts = []
            for m in messages:
                role = m.get("role", "user")
                content = m.get("content", "")
                if not content:
                    continue
                if role == "system":
                    system_text += content + "\n\n"
                elif role == "user":
                    user_parts.append(content)
                elif role == "assistant":
                    # Treat as context
                    user_parts.append(f"[Previous response]: {content}")

            combined = system_text + "\n".join(user_parts)

            model = genai.GenerativeModel(
                model_name=self._config.gemini_model,
                generation_config={
                    "temperature": temperature,
                    "max_output_tokens": max_tokens,
                    "response_mime_type": "text/plain",
                },
            )
            result = model.generate_content(combined)
            text = (result.text or "").strip() if hasattr(result, "text") else ""

            if not text and hasattr(result, "candidates"):
                for cand in result.candidates:
                    if hasattr(cand, "content") and hasattr(cand.content, "parts"):
                        for p in cand.content.parts:
                            if hasattr(p, "text"):
                                text += p.text

            log.info("gemini_completion_used", chars=len(text))
            return _Response(text, provider="gemini")

        except Exception as e:
            log.error("gemini_completion_failed", error=str(e)[:200])
            raise


def get_llm_client(config: ServerConfig) -> LLMClient:
    """Factory — returns the unified LLM client."""
    return LLMClient(config)
