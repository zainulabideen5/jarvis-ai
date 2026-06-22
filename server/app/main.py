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

    # Ensure engine recipe (skill memory) table
    from app.services.universal_engine.recipes import RecipeStore
    RecipeStore.ensure_table()

    # Multi-agent: restart se pehle ke adhoore (running/queued) agents ko failed
    # mark kar do (process mar chuka) — taake dashboard mein hamesha-running na rahein.
    try:
        from app.services.agent_manager import AgentManager
        await AgentManager.cleanup_stale()
    except Exception as e:
        log.warning("agent_cleanup_failed", error=str(e))

    # Detect + cache the runtime environment (PC/laptop, OS, apps) so JARVIS
    # adapts per platform (PC → web/desktop, phone → app). Self-adaptive.
    try:
        from app.services.environment import detect_environment
        detect_environment(refresh=True)
    except Exception as e:
        log.warning("environment_detect_failed", error=str(e))

    # Start background processor
    processor = ChunkProcessor(config)
    processor_task = asyncio.create_task(processor.run())

    # Start smart notifier (runs every 5 minutes)
    notifier = SmartNotifier(config)
    reporter = DailyReportService(config)

    async def background_loop():
        while True:
            try:
                # NOTE: notifier.check_and_notify() was injecting "🔔 N tasks
                # pending" messages into the chat every 5 min — Zain found this
                # cluttered the chat (pending tasks belong on the Tasks page,
                # not spammed into every conversation). Disabled. The daily
                # report still runs below.
                if await reporter.should_generate():
                    await reporter.generate_report()
            except Exception as e:
                log.warning("background_loop_error", error=str(e))
            await asyncio.sleep(300)  # 5 minutes

    notifier_task = asyncio.create_task(background_loop())

    # JARVIS ka DEDICATED browser startup pe launch (visible) — user isme ek baar
    # WhatsApp login kar le, phir JARVIS chat se background mein use karta hai.
    # Login persistent profile mein bachta hai (clean-close se restart pe bhi).
    # Non-blocking: alag daemon thread, startup ko block na kare.
    def _launch_jarvis_browser():
        try:
            from app.services.web_browser import WebBrowser
            # VISIBLE (headed) rakho taake user JARVIS chrome dekh sake (kaam hote
            # hue), PAR foreground pe force nahi karte (focus nahi cheenta — bring_
            # to_front/SetForegroundWindow nahi). Login persistent profile mein bacha
            # rehta hai (clean-close). reuse-always: saare sends isi visible window
            # mein hote hain.
            WebBrowser.get().goto("https://web.whatsapp.com", headless=False)
            log.info("jarvis_browser_launched_visible")
        except Exception as e:
            log.warning("jarvis_browser_launch_failed", error=str(e))

    import threading
    threading.Thread(target=_launch_jarvis_browser, daemon=True).start()

    log.info("jarvis_server_ready")

    yield

    # Shutdown — cancel AND await so cleanup runs and no "task destroyed while
    # pending" warnings leak.
    processor_task.cancel()
    notifier_task.cancel()
    await asyncio.gather(processor_task, notifier_task, return_exceptions=True)
    # Clean-close the web browser so logged-in sessions (WhatsApp/Gmail/etc.)
    # flush to disk and survive a restart (graceful shutdowns only).
    try:
        from app.services.web_browser import WebBrowser
        await asyncio.to_thread(WebBrowser.get().close)
    except Exception as e:
        log.warning("web_browser_close_failed", error=str(e))
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
