"""Entry point: python -m jarvis_agent"""

from __future__ import annotations

import asyncio
import sys

from jarvis_agent.config import AgentConfig
from jarvis_agent.core.orchestrator import Orchestrator
from jarvis_agent.utils.logging import setup_logging, get_logger


def main() -> None:
    config = AgentConfig()
    setup_logging(config.log_level)
    log = get_logger("jarvis")

    log.info(
        "jarvis_starting",
        version="0.1.0",
        sample_rate=config.audio_sample_rate,
        chunk_duration=config.audio_chunk_duration_sec,
        server=config.server_ws_url,
    )

    orchestrator = Orchestrator(config)

    try:
        asyncio.run(orchestrator.run())
    except KeyboardInterrupt:
        log.info("jarvis_stopped_by_user")
        sys.exit(0)


if __name__ == "__main__":
    main()
