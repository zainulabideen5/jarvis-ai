"""Transcription service using Groq Whisper API — strong and accurate."""

from __future__ import annotations

import io
import time
import wave

import numpy as np
from groq import Groq

from app.core.config import ServerConfig
from app.core.logging import get_logger

log = get_logger(__name__)


class TranscriptionService:
    """Transcribes audio using Groq Whisper API. Never crashes."""

    def __init__(self, config: ServerConfig):
        self._config = config
        self._client = None

    def _get_client(self) -> Groq:
        if self._client is None:
            if not self._config.groq_api_key:
                raise ValueError("JARVIS_GROQ_API_KEY not set")
            self._client = Groq(api_key=self._config.groq_api_key)
        return self._client

    @staticmethod
    def _pcm_to_wav_bytes(audio_array: "np.ndarray", sample_rate: int) -> bytes:
        """Pack int16 PCM samples into a WAV byte string (mono, 16-bit)."""
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(audio_array.tobytes())
        return buf.getvalue()

    def _transcribe_via_cloud(self, wav_bytes: bytes) -> dict | None:
        """POST WAV to self-hosted Cloud Whisper. Returns result dict or None
        on any failure (so caller falls back to Groq). Never raises."""
        url = getattr(self._config, "whisper_cloud_url", "") or ""
        if not url:
            return None
        token = getattr(self._config, "whisper_cloud_token", "") or ""
        timeout = getattr(self._config, "whisper_cloud_timeout", 30.0)
        try:
            import requests  # local import — keeps module import light

            headers = {}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            files = {"file": ("audio.wav", wav_bytes, "audio/wav")}
            start = time.perf_counter()
            resp = requests.post(url, headers=headers, files=files, timeout=timeout)
            elapsed = time.perf_counter() - start
            if resp.status_code != 200:
                log.warning("cloud_whisper_http_error", status=resp.status_code)
                return None
            data = resp.json()
            text = (data.get("text") or "").strip()
            if self._is_garbage(text):
                log.debug("cloud_whisper_garbage_filtered", text=text[:50])
                return {"text": "", "language": data.get("language", "unknown"),
                        "confidence": 0, "time_sec": elapsed}
            log.info("cloud_whisper_done", text_len=len(text),
                     language=data.get("language"), time=f"{elapsed:.2f}s")
            return {
                "text": text,
                "language": data.get("language", "unknown"),
                "confidence": 0.95,
                "time_sec": elapsed,
            }
        except Exception as e:
            log.warning("cloud_whisper_failed", error=str(e)[:160])
            return None

    def transcribe_pcm(
        self, pcm_bytes: bytes, sample_rate: int = 16000
    ) -> dict:
        """Transcribe raw PCM audio. Never crashes — returns empty on error.

        Order: self-hosted Cloud Whisper (zero cost) → Groq Whisper fallback.
        """
        # Convert PCM to WAV (shared by both cloud and Groq paths)
        audio_array = np.frombuffer(pcm_bytes, dtype=np.int16)

        # Skip if too short (less than 0.5 sec)
        if len(audio_array) < sample_rate // 2:
            return {"text": "", "language": "unknown", "confidence": 0, "time_sec": 0}

        wav_bytes = self._pcm_to_wav_bytes(audio_array, sample_rate)

        # ----- PRIMARY: self-hosted Cloud Whisper (zero per-use cost) -----
        cloud_url = getattr(self._config, "whisper_cloud_url", "") or ""
        if cloud_url:
            cloud_result = self._transcribe_via_cloud(wav_bytes)
            if cloud_result is not None and cloud_result.get("text"):
                return cloud_result
            # cloud returned empty/None → fall through to Groq
            log.info("cloud_whisper_empty_or_down_falling_to_groq")

        # ----- FALLBACK: Groq Whisper -----
        try:
            client = self._get_client()

            wav_buffer = io.BytesIO(wav_bytes)
            wav_buffer.name = "audio.wav"

            start = time.perf_counter()

            transcription = client.audio.transcriptions.create(
                file=wav_buffer,
                model="whisper-large-v3",
                response_format="verbose_json",
                language="en",
                # Whisper "prompt" is an in-domain style example, not an instruction.
                # Instructions ("Transcribe in English letters") get echoed verbatim
                # when audio is silent or noisy — observed bug: prompt leak into output.
                # Pure example text steers style without that risk.
                prompt=(
                    "Yaar Ahmed ko kal tak logo bhej do urgent hai. "
                    "Sara meeting setup karo client ke saath parson. "
                    "Hamza invoice bhejo aur design ready karo. "
                    "Theek hai bhai abhi karta hoon."
                ),
            )

            elapsed = time.perf_counter() - start

            text = (transcription.text or "").strip()
            language = getattr(transcription, "language", "hi") or "hi"

            # Filter garbage transcriptions
            if self._is_garbage(text):
                log.debug("transcription_garbage_filtered", text=text[:50])
                return {"text": "", "language": language, "confidence": 0, "time_sec": elapsed}

            log.info(
                "transcription_done",
                text_len=len(text),
                language=language,
                time=f"{elapsed:.2f}s",
            )

            return {
                "text": text,
                "language": language,
                "confidence": 0.95,
                "time_sec": elapsed,
            }

        except Exception as e:
            log.warning("transcription_failed", error=str(e))
            return {"text": "", "language": "unknown", "confidence": 0, "time_sec": 0}

    # Whisper sometimes echoes its own prompt back when audio is silent/noisy.
    # If any of these fragments appear in the output, treat the whole transcription
    # as garbage. Keep the set in sync with the prompt fed to Whisper above.
    _PROMPT_LEAK_MARKERS = (
        "transcribe everything",
        "english letters only",
        "roman alphabet",
        "do not use devanagari",
        "yaar ahmed ko kal tak logo bhej do urgent hai",
        "sara meeting setup karo client ke saath parson",
        "hamza invoice bhejo aur design ready karo",
    )

    @staticmethod
    def _is_garbage(text: str) -> bool:
        """Filter out garbage/noise transcriptions."""
        text = text.strip()

        # Too short
        if len(text) < 3:
            return True

        # Just punctuation or numbers
        cleaned = text.replace(".", "").replace(",", "").replace(" ", "").replace("।", "")
        if not cleaned or cleaned.isdigit():
            return True

        # Repeated words like "Thank you. Thank you. Thank you."
        words = text.split()
        if len(words) >= 3 and len(set(words)) <= 2:
            return True

        # Whisper prompt leakage — silent/noisy audio causes the model to echo the prompt
        low = text.lower()
        for marker in TranscriptionService._PROMPT_LEAK_MARKERS:
            if marker in low:
                return True

        # Common noise transcriptions
        garbage_phrases = [
            "thank you", "thanks", "gracias", "obrigado", "dhanyavaad",
            "subtitles", "subscribe", "like and subscribe",
        ]
        if text.lower().strip().rstrip(".!") in garbage_phrases:
            return True

        return False
