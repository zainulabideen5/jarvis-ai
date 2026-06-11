"""WASAPI loopback capture for system audio on Windows."""

from __future__ import annotations

import numpy as np
import pyaudiowpatch as pyaudio

from jarvis_agent.config import AgentConfig
from jarvis_agent.utils.logging import get_logger

log = get_logger(__name__)


class LoopbackCapture:
    """Captures system audio via WASAPI loopback."""

    def __init__(self, config: AgentConfig, buffer_queue):
        """
        Args:
            config: Agent configuration.
            buffer_queue: A janus.SyncQueue (sync side) to push audio into.
        """
        self._config = config
        self._queue = buffer_queue
        self._pa = pyaudio.PyAudio()
        self._stream = None
        self._native_rate: int = 0
        self._channels: int = 0

    def _find_loopback_device(self) -> dict:
        """Find the WASAPI loopback device for default speakers."""
        wasapi_info = self._pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        default_speakers = self._pa.get_device_info_by_index(
            wasapi_info["defaultOutputDevice"]
        )

        if not default_speakers["isLoopbackDevice"]:
            for loopback in self._pa.get_loopback_device_info_generator():
                if default_speakers["name"] in loopback["name"]:
                    log.info(
                        "loopback_device_found",
                        name=loopback["name"],
                        rate=loopback["defaultSampleRate"],
                    )
                    return loopback

        raise RuntimeError(
            "No WASAPI loopback device found. "
            "Make sure audio output is available."
        )

    def start(self) -> None:
        device = self._find_loopback_device()
        self._native_rate = int(device["defaultSampleRate"])
        self._channels = device["maxInputChannels"]

        log.info(
            "starting_loopback",
            rate=self._native_rate,
            channels=self._channels,
            device=device["name"],
        )

        self._stream = self._pa.open(
            format=pyaudio.paInt16,
            channels=self._channels,
            rate=self._native_rate,
            frames_per_buffer=self._config.audio_buffer_frames,
            input=True,
            input_device_index=device["index"],
            stream_callback=self._callback,
        )

    def _callback(self, in_data, frame_count, time_info, status):
        """PortAudio callback — runs on audio thread, must be fast."""
        audio = np.frombuffer(in_data, dtype=np.int16)

        # Downmix stereo to mono
        if self._channels >= 2:
            audio = audio.reshape(-1, self._channels).mean(axis=1).astype(np.int16)

        try:
            self._queue.put_nowait(
                (audio, self._native_rate)
            )
        except Exception:
            # Queue full = consumer too slow, frames lost. Log periodically so
            # we know the system is dropping audio instead of failing silently.
            self._dropped_frames = getattr(self, "_dropped_frames", 0) + 1
            if self._dropped_frames % 50 == 1:
                log.warning("loopback_frame_dropped_queue_full", dropped=self._dropped_frames)

        return (None, pyaudio.paContinue)

    @property
    def native_rate(self) -> int:
        return self._native_rate

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop_stream()
            self._stream.close()
            self._stream = None
        log.info("loopback_stopped")

    def close(self) -> None:
        self.stop()
        self._pa.terminate()
