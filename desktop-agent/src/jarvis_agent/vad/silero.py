"""Silero VAD wrapper for speech detection — bulletproof, no crashes."""

from __future__ import annotations

import numpy as np
import torch

from jarvis_agent.config import AgentConfig
from jarvis_agent.utils.logging import get_logger

log = get_logger(__name__)

# Process max 5 sec at a time (80000 samples at 16kHz)
MAX_CHUNK = 80000


class SileroVAD:
    """Wraps Silero VAD model for speech detection on 16kHz audio."""

    def __init__(self, config: AgentConfig):
        self._threshold = config.vad_threshold
        self._min_speech_ms = config.vad_min_speech_ms
        self._min_silence_ms = config.vad_min_silence_ms
        self._sample_rate = config.audio_sample_rate
        self._model = None
        self._get_speech_timestamps = None
        self._fallback_mode = False

        # torch.hub.load downloads from GitHub the first time. After it succeeds
        # the model is cached locally (~/.cache/torch/hub/...). On a fresh
        # laptop with no internet — or behind a firewall — this raises and used
        # to crash the entire agent startup. Now we fall back to "always treat
        # as speech" so the rest of the pipeline keeps working (audio still
        # gets captured and shipped; only VAD filtering is degraded).
        log.info("loading_vad_model")
        try:
            self._model, self._utils = torch.hub.load(
                repo_or_dir="snakers4/silero-vad",
                model="silero_vad",
                trust_repo=True,
            )
            (
                self._get_speech_timestamps,
                _, _, _, _,
            ) = self._utils
            log.info("vad_model_loaded")
        except Exception as e:
            log.warning(
                "vad_model_load_failed_fallback_mode",
                error=str(e),
                hint="VAD unavailable — all chunks will be treated as speech (no filtering).",
            )
            self._fallback_mode = True

    def get_speech_timestamps(self, audio: np.ndarray) -> list[dict[str, int]]:
        """Run VAD on float32 audio. Memory safe — processes in small chunks."""
        if len(audio) == 0:
            return []

        # Fallback: model failed to load (no internet on first run). Treat the
        # entire buffer as speech so chunks still get uploaded — the server's
        # whisper + LLM-side garbage filter will reject silence anyway.
        if self._fallback_mode or self._model is None:
            return [{"start": 0, "end": len(audio)}]

        all_timestamps = []
        offset = 0

        while offset < len(audio):
            end = min(offset + MAX_CHUNK, len(audio))
            chunk = audio[offset:end]

            try:
                tensor = torch.from_numpy(chunk.copy()).float()
                timestamps = self._get_speech_timestamps(
                    tensor,
                    self._model,
                    sampling_rate=self._sample_rate,
                    threshold=self._threshold,
                    min_speech_duration_ms=self._min_speech_ms,
                    min_silence_duration_ms=self._min_silence_ms,
                )
                for t in timestamps:
                    all_timestamps.append({
                        "start": t["start"] + offset,
                        "end": t["end"] + offset,
                    })
                self._model.reset_states()
                del tensor
            except (MemoryError, RuntimeError, Exception) as e:
                log.debug("vad_chunk_skip", offset=offset, error=str(e))
                self._model.reset_states()

            offset = end

        return all_timestamps

    def has_speech(self, audio: np.ndarray) -> bool:
        """Check if audio contains speech."""
        try:
            return len(self.get_speech_timestamps(audio)) > 0
        except Exception:
            return False

    def speech_ratio(self, audio: np.ndarray) -> float:
        """Calculate ratio of speech in audio (0.0 to 1.0)."""
        if len(audio) == 0:
            return 0.0
        try:
            timestamps = self.get_speech_timestamps(audio)
            speech_samples = sum(t["end"] - t["start"] for t in timestamps)
            return min(speech_samples / len(audio), 1.0)
        except Exception:
            return 0.0

    def reset(self) -> None:
        """Reset model states."""
        try:
            self._model.reset_states()
        except Exception:
            pass
