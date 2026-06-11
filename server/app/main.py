"""Jarvis Server — FastAPI application."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router as api_router
from app.core.config import ServerConfig
from app.core.database import init_db
from app.core.logging import get_logger, setup_logging
from app.services.daily_report import DailyReportService
from app.services.processor import ChunkProcessor
from app.services.smart_notify import SmartNotifier
from app.ws.handler import agent_ws_handler

config = ServerConfig()
setup_logging(config.log_level)
log = get_logger("jarvis.server")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown."""
    log.info("server_starting", host=config.host, port=config.port)

    # Create data directories
    config.audio_chunks_dir.mkdir(parents=True, exist_ok=True)

    # Initialize database
    await init_db()
    log.info("database_initialized")

    # Ensure laptop activity log table
    from app.services.laptop_control.activity_log import ActivityLogger
    await ActivityLogger.ensure_table()

    # Ensure verification log table
    from app.services.verification import VerificationService
    await VerificationService.ensure_table()

    # Ensure AI memory table
    from app.services.memory import MemoryService
    await MemoryService.ensure_table()

    # Ensure consent table
    from app.services.consent import ConsentService
    await ConsentService.ensure_table()

    # Ensure contact-routing (learned channel per contact) table
    from app.services.contact_routing import ContactRouting
    await ContactRouting.ensure_table()

    # Start background processor
    processor = ChunkProcessor(config)
    processor_task = asyncio.create_task(processor.run())

    # Start smart notifier (runs every 5 minutes)
    notifier = SmartNotifier(config)
    reporter = DailyReportService(config)

    async def background_loop():
        while True:
            try:
                await notifier.check_and_notify()
                if await reporter.should_generate():
                    await reporter.generate_report()
            except Exception as e:
                log.warning("background_loop_error", error=str(e))
            await asyncio.sleep(300)  # 5 minutes

    notifier_task = asyncio.create_task(background_loop())

    log.info("jarvis_server_ready")

    yield

    # Shutdown
    processor_task.cancel()
    notifier_task.cancel()
    log.info("server_stopped")


app = FastAPI(
    title="Jarvis Server",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS for dashboard
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# REST API
app.include_router(api_router)


# WebSocket endpoint
@app.websocket("/ws/agent")
async def ws_agent(websocket: WebSocket):
    await agent_ws_handler(websocket)
