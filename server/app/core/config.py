"""Server configuration."""

from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


def _split_keys(raw: str) -> list[str]:
    """Split a comma- or semicolon-separated key string into a clean list.

    Used for multi-key support — when a provider's daily quota is hit on one
    key, JARVIS rotates to the next. The user just appends new keys with
    commas in `.env`:
        JARVIS_GROQ_API_KEY=gsk_new,gsk_older,gsk_oldest
    """
    if not raw:
        return []
    parts: list[str] = []
    for chunk in raw.replace(";", ",").split(","):
        s = chunk.strip()
        if s:
            parts.append(s)
    return parts


class ServerConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="JARVIS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Server
    host: str = "0.0.0.0"
    port: int = 8000

    # Database
    database_url: str = "sqlite+aiosqlite:///./data/jarvis.db"

    # Audio storage
    audio_chunks_dir: Path = Path("./data/audio_chunks")

    # Whisper
    whisper_model: str = "base"  # tiny, base, small, medium, large-v3
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"

    # Groq
    groq_api_key: str = ""
    # 70b for chat quality — Roman Urdu nuances (e.g. "chai" = need vs tea)
    # need the larger model. With 4 keys in rotation, 4×1000=4000 req/day
    # available, plus Cerebras fallback when Groq exhausts. Worth the quota
    # cost for the quality difference.
    groq_model: str = "llama-3.3-70b-versatile"

    # Gemini (fallback #1)
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"

    # Cerebras (fallback #2 — super fast, 8000 msgs/day free)
    cerebras_api_key: str = ""
    # Free-tier available model. The 70b ("llama-3.3-70b") 404s on free
    # accounts ("does not exist or you do not have access"), so use 8b — it's
    # the reliable fast fallback when Groq's daily quota is exhausted.
    cerebras_model: str = "llama3.1-8b"

    # OpenRouter (fallback #3 — free models pool)
    openrouter_api_key: str = ""
    # gpt-oss-120b — strong 120B param model, currently available on free tier.
    # Falls back to a smaller free model if rate-limited. Alternatives if this
    # tier saturates: google/gemma-4-31b-it:free, liquid/lfm-2.5-1.2b-instruct:free
    openrouter_model: str = "openai/gpt-oss-120b:free"

    # HuggingFace (for pyannote speaker diarization)
    hf_token: str = ""
    diarization_enabled: bool = False

    # Gmail SMTP (server-side email send — multi-recipient + attachments)
    # Generate app password at: https://myaccount.google.com/apppasswords
    gmail_address: str = ""
    gmail_app_password: str = ""

    # General
    log_level: str = "INFO"

    # ---- Multi-key helpers ----
    # Convenience properties that split the comma-separated env var into a
    # rotation list. Backward compat: a single-key env var still works (yields
    # a 1-element list).

    @property
    def groq_api_keys(self) -> list[str]:
        return _split_keys(self.groq_api_key)

    @property
    def gemini_api_keys(self) -> list[str]:
        return _split_keys(self.gemini_api_key)

    @property
    def cerebras_api_keys(self) -> list[str]:
        return _split_keys(self.cerebras_api_key)

    @property
    def openrouter_api_keys(self) -> list[str]:
        return _split_keys(self.openrouter_api_key)
