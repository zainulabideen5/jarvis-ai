"""Configuration management using Pydantic Settings."""

import re
import socket
from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_agent_id() -> str:
    """Derive a stable agent_id from the host so each laptop registers uniquely without manual config."""
    raw = socket.gethostname() or ""
    cleaned = re.sub(r"[^a-z0-9-]", "-", raw.lower()).strip("-")
    return cleaned or "unknown-host"


class AgentConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="JARVIS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Audio
    audio_sample_rate: int = 16000
    audio_chunk_duration_sec: float = 30.0
    audio_buffer_frames: int = 512
    loopback_device_name: str | None = None
    mic_device_index: int | None = None

    # VAD
    vad_threshold: float = 0.5
    vad_min_speech_ms: int = 250
    vad_min_silence_ms: int = 1000

    # Agent identity — auto-derived from hostname if JARVIS_AGENT_ID is not set in .env
    agent_id: str = Field(default_factory=_default_agent_id)
    agent_name: str = ""

    # Transport
    server_ws_url: str = "ws://localhost:8000/ws/agent"
    server_api_url: str = "http://localhost:8000/api"
    upload_timeout_sec: int = 30
    reconnect_delay_sec: float = 5.0

    # Context
    window_poll_interval_sec: float = 1.0

    # Task execution
    task_poll_interval_sec: float = 5.0

    # Gmail driver
    gmail_address: str = ""
    gmail_app_password: str = ""

    # Telegram driver
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Slack driver
    slack_webhook_url: str = ""
    slack_bot_token: str = ""
    slack_default_channel: str = ""

    # Discord driver
    discord_webhook_url: str = ""

    # Notion driver
    notion_token: str = ""
    notion_database_id: str = ""

    # Groq (for vision driver)
    groq_api_key: str = ""

    # General
    log_level: str = "INFO"
    data_dir: Path = Path("./data")
