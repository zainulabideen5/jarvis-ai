"""Speaker diarization service using pyannote.audio."""

from __future__ import annotations

import io
import time
import wave

import numpy as np

from app.core.config import ServerConfig
from app.core.logging import get_logger

log = get_logger(__name__)


class DiarizationService:
    """Identifies who is speaking in each audio segment using pyannote.audio.

    Requires a HuggingFace token with access to pyannote models.
    Set JARVIS_HF_TOKEN in your .env file.
    """

    def __init__(self, config: ServerConfig):
        self._config = config
        self._pipeline = None

    def load_model(self) -> None:
        """Load the pyannote speaker diarization pipeline (one-time, ~10 sec)."""
        from pyannote.audio import Pipeline

        token = self._config.hf_token
        if not token:
            raise ValueError(
                "JARVIS_HF_TOKEN not set. Get one from https://huggingface.co/settings/tokens "
                "and accept pyannote model terms at https://huggingface.co/pyannote/speaker-diarization-3.1"
            )

        log.info("loading_diarization_model")

        # Fix: pyannote 3.x passes use_auth_token to newer huggingface_hub which removed it
        import huggingface_hub
        _original_download = huggingface_hub.hf_hub_download
        def _patched_download(*args, **kwargs):
            kwargs.pop("use_auth_token", None)
            return _original_download(*args, **kwargs)
        huggingface_hub.hf_hub_download = _patched_download

        from huggingface_hub import login
        login(token=token, add_to_git_credential=False)

        self._pipeline = Pipeline.from_pretrained(
            "pyannote/speaker-diarization-3.1",
        )
        log.info("diarization_model_loaded")

    def diarize_pcm(
        self, pcm_bytes: bytes, sample_rate: int = 16000
    ) -> list[dict]:
        """Diarize raw PCM audio bytes.

        Args:
            pcm_bytes: 16-bit mono PCM audio data.
            sample_rate: Sample rate (default 16000).

        Returns:
            List of segments: [{"speaker": "SPEAKER_00", "start": 0.5, "end": 3.2}, ...]
        """
        if self._pipeline is None:
            self.load_model()

        # Convert PCM to WAV in memory (pyannote needs a file-like object)
        audio_array = np.frombuffer(pcm_bytes, dtype=np.int16)
        wav_buffer = io.BytesIO()
        with wave.open(wav_buffer, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # 16-bit
            wf.setframerate(sample_rate)
            wf.writeframes(audio_array.tobytes())
        wav_buffer.seek(0)

        start = time.perf_counter()
        diarization = self._pipeline(wav_buffer)
        elapsed = time.perf_counter() - start

        segments = []
        for turn, _, speaker in diarization.itertracks(yield_label=True):
            segments.append({
                "speaker": speaker,
                "start": round(turn.start, 2),
                "end": round(turn.end, 2),
            })

        log.info(
            "diarization_done",
            segments=len(segments),
            speakers=len(set(s["speaker"] for s in segments)),
            time=f"{elapsed:.2f}s",
        )

        return segments

    def merge_with_transcription(
        self, segments: list[dict], transcription_text: str,
        speaker_names: dict[str, str] | None = None,
    ) -> str:
        """Merge diarization segments with transcription text.

        Args:
            segments: Diarization output
            transcription_text: Full transcription
            speaker_names: Optional map of SPEAKER_XX -> client name
                e.g. {"SPEAKER_00": "Ahmed", "SPEAKER_01": "Boss"}

        Produces formatted output like:
            Ahmed: Hello, how are you?
            Boss: I'm good, thanks.
        """
        if not segments:
            return transcription_text

        words = transcription_text.split()
        if not words:
            return transcription_text

        speaker_names = speaker_names or {}
        total_duration = max(s["end"] for s in segments) if segments else 1
        result_lines = []
        word_idx = 0

        for seg in segments:
            seg_duration = seg["end"] - seg["start"]
            word_count = max(1, int(len(words) * seg_duration / total_duration))
            seg_words = words[word_idx : word_idx + word_count]
            word_idx += word_count

            if seg_words:
                # Replace SPEAKER_XX with client name if available
                label = speaker_names.get(seg["speaker"], seg["speaker"])
                result_lines.append(f"{label}: {' '.join(seg_words)}")

        if word_idx < len(words) and segments:
            remaining = " ".join(words[word_idx:])
            label = speaker_names.get(segments[-1]["speaker"], segments[-1]["speaker"])
            result_lines.append(f"{label}: {remaining}")

        return "\n".join(result_lines)
