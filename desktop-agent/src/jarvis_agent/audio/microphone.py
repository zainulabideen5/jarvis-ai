"""Microphone capture using PyAudioWPatch."""

from __future__ import annotations

import numpy as np
import pyaudiowpatch as pyaudio

from jarvis_agent.config import AgentConfig
from jarvis_agent.utils.logging import get_logger

log = get_logger(__name__)


class MicrophoneCapture:
    """Captures audio from the default microphone."""

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

    def _find_mic_device(self) -> dict:
        """Find the microphone device."""
        if self._config.mic_device_index is not None:
            device = self._pa.get_device_info_by_index(
                self._config.mic_device_index
            )
            log.info("mic_device_specified", name=device["name"])
            return device

        device = self._pa.get_default_input_device_info()
        log.info(
            "mic_device_default",
            name=device["name"],
            rate=device["defaultSampleRate"],
        )
        return device

    def start(self) -> None:
        device = self._find_mic_device()
        self._native_rate = int(device["defaultSampleRate"])
        self._channels = max(1, int(device["maxInputChannels"]))

        log.info(
            "starting_mic",
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

        # Downmix to mono if stereo
        if self._channels >= 2:
            audio = audio.reshape(-1, self._channels).mean(axis=1).astype(np.int16)

        try:
            self._queue.put_nowait(
                (audio, self._native_rate)
            )
        except Exception:
            # Queue full means the consumer (mixer/uploader) is too slow and
            # we're dropping audio frames. Surface this so it's debuggable
            # instead of silently losing parts of the conversation.
            self._dropped_frames = getattr(self, "_dropped_frames", 0) + 1
            if self._dropped_frames % 50 == 1:
                log.warning("mic_frame_dropped_queue_full", dropped=self._dropped_frames)

        return (None, pyaudio.paContinue)

    @property
    def native_rate(self) -> int:
        return self._native_rate

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop_stream()
            self._stream.close()
            self._stream = None
        log.info("mic_stopped")

    def close(self) -> None:
        self.stop()
        self._pa.terminate()
