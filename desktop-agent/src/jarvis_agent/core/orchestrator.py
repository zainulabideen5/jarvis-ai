"""Orchestrator — the central async coordinator that ties all modules together."""

from __future__ import annotations

import asyncio
from datetime import datetime

import janus

from jarvis_agent.audio.chunker import AudioChunker
from jarvis_agent.audio.loopback import LoopbackCapture
from jarvis_agent.audio.microphone import MicrophoneCapture
from jarvis_agent.audio.mixer import mixer_task
from jarvis_agent.config import AgentConfig
from jarvis_agent.context.active_window import ActiveWindowMonitor
from jarvis_agent.core.events import AudioChunk
from jarvis_agent.executor.task_listener import TaskListener
from jarvis_agent.transport.ws_client import AgentWebSocketClient
from jarvis_agent.ui.tray import TrayIcon
from jarvis_agent.utils.logging import get_logger
from jarvis_agent.vad.silero import SileroVAD

log = get_logger(__name__)


class Orchestrator:
    """Manages the lifecycle of all agent components.

    Architecture:
        PortAudio threads -> janus queues -> async mixer tasks -> chunkers -> upload queue -> WS client
        Window monitor runs independently, attaches context to chunks.
    """

    def __init__(self, config: AgentConfig):
        self._config = config
        self.listening = False  # Boss controls this via dashboard

    async def run(self) -> None:
        log.info("orchestrator_starting")

        # Load VAD model (one-time, ~2 sec)
        vad = SileroVAD(self._config)

        # Create janus queues for thread-safe PortAudio -> asyncio bridging
        loopback_janus: janus.Queue = janus.Queue(maxsize=200)
        mic_janus: janus.Queue = janus.Queue(maxsize=200)

        # Resampled audio queues (async-only)
        loopback_resampled: asyncio.Queue = asyncio.Queue(maxsize=200)
        mic_resampled: asyncio.Queue = asyncio.Queue(maxsize=200)

        # Raw chunk queue (from chunkers) and final upload queue
        raw_chunk_queue: asyncio.Queue[AudioChunk] = asyncio.Queue(maxsize=100)
        upload_queue: asyncio.Queue[AudioChunk] = asyncio.Queue(maxsize=100)

        # Initialize capture devices
        loopback = LoopbackCapture(self._config, loopback_janus.sync_q)
        mic = MicrophoneCapture(self._config, mic_janus.sync_q)

        # Initialize chunkers (emit to raw_chunk_queue)
        loopback_chunker = AudioChunker(
            self._config, vad, "loopback", raw_chunk_queue
        )
        mic_chunker = AudioChunker(self._config, vad, "mic", raw_chunk_queue)

        # Window monitor
        window_monitor = ActiveWindowMonitor(self._config)

        # WebSocket client (reads from upload_queue with window context attached)
        ws_client = AgentWebSocketClient(self._config, upload_queue)

        # Task listener (polls server for approved tasks, executes them)
        task_listener = TaskListener(self._config)

        # Start system tray icon
        tray = TrayIcon(dashboard_url="http://localhost:3000")
        tray.start()

        # Start audio capture (PortAudio threads). Both are tried independently —
        # if one device is unavailable (no speakers, mic in use by another app)
        # the other still captures. Hard-failing on loopback used to crash the
        # entire agent for users with no loopback device.
        loopback_ok = False
        mic_ok = False
        try:
            loopback.start()
            loopback_ok = True
            log.info("loopback_capture_started")
        except Exception as e:
            log.warning(
                "loopback_start_failed_continuing_mic_only",
                error=str(e),
                hint="System-audio capture unavailable; mic alone will record.",
            )

        try:
            mic.start()
            mic_ok = True
            log.info("mic_capture_started")
        except Exception as e:
            log.warning("mic_start_failed", error=str(e))

        if not loopback_ok and not mic_ok:
            # Neither device is usable — there's no point continuing.
            log.error("no_audio_devices_available", hint="Check mic permissions and speaker drivers.")
            raise RuntimeError("Both mic and loopback failed to start — no audio capture possible.")

        log.info("jarvis_agent_ready", loopback=loopback_ok, mic=mic_ok)

        try:
            async with asyncio.TaskGroup() as tg:
                # Mixer tasks: read from janus async side, resample, push
                tg.create_task(
                    mixer_task(
                        loopback_janus.async_q,
                        loopback_resampled,
                        "loopback",
                    )
                )
                tg.create_task(
                    mixer_task(
                        mic_janus.async_q,
                        mic_resampled,
                        "mic",
                    )
                )

                # Chunker feeder tasks
                tg.create_task(
                    self._chunker_feeder(
                        loopback_resampled, loopback_chunker, window_monitor
                    )
                )
                tg.create_task(
                    self._chunker_feeder(
                        mic_resampled, mic_chunker, window_monitor
                    )
                )

                # Window context enricher: reads raw chunks, attaches window info, forwards to upload queue
                tg.create_task(
                    self._enrich_chunks(
                        raw_chunk_queue, upload_queue, window_monitor
                    )
                )

                # Window monitor
                tg.create_task(window_monitor.run())

                # WebSocket upload
                tg.create_task(ws_client.run())

                # Task executor (polls for approved tasks)
                tg.create_task(task_listener.run())

                # Listening state poller (checks server every 3 sec)
                tg.create_task(self._poll_listening_state())

        finally:
            log.info("orchestrator_shutting_down")
            loopback.close()
            mic.close()
            await loopback_chunker.flush()
            await mic_chunker.flush()
            loopback_janus.close()
            mic_janus.close()

    async def _poll_listening_state(self) -> None:
        """Poll server for listening ON/OFF state every 3 seconds."""
        import json
        import urllib.request

        api_url = self._config.server_api_url
        while True:
            try:
                req = urllib.request.Request(f"{api_url}/listening")
                with urllib.request.urlopen(req, timeout=5) as resp:
                    data = json.loads(resp.read())
                    new_state = data.get("listening", "off") == "on"
                    if new_state != self.listening:
                        self.listening = new_state
                        log.info("listening_state_changed", listening=self.listening)
            except Exception:
                pass
            await asyncio.sleep(3)

    async def _chunker_feeder(
        self,
        resampled_queue: asyncio.Queue,
        chunker: AudioChunker,
        window_monitor: ActiveWindowMonitor,
    ) -> None:
        """Read resampled audio and feed it to the chunker. Never crashes."""
        while True:
            try:
                audio_float32, source = await resampled_queue.get()
                # Boss ka control — sirf listening ON ho to process kare
                if not self.listening:
                    continue
                now = datetime.now()
                await chunker.feed(audio_float32, now)
            except MemoryError:
                log.warning("chunker_feeder_memory_reset")
                continue
            except Exception as e:
                log.debug("chunker_feeder_error", error=str(e))
                continue

    async def _enrich_chunks(
        self,
        raw_queue: asyncio.Queue,
        upload_queue: asyncio.Queue,
        window_monitor: ActiveWindowMonitor,
    ) -> None:
        """Attach active window context to chunks before upload."""
        while True:
            chunk: AudioChunk = await raw_queue.get()
            window = window_monitor.current
            if window:
                chunk.active_window = window.process_name
                chunk.active_window_title = window.window_title
            await upload_queue.put(chunk)
