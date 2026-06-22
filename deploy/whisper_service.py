"""JARVIS Cloud Whisper service — self-hosted faster-whisper on GCP VM.

Zero per-transcription cost. Runs on the dedicated jarvis-vm (e2-standard-2).
JARVIS (laptop) POSTs audio bytes here; we transcribe with faster-whisper
(CPU, int8 quantized) and return text. Replaces the paid Groq Whisper API.

Endpoints:
  GET  /health           → service + model status (no auth)
  POST /transcribe       → multipart audio file → {text, language, duration}
                           Requires header: Authorization: Bearer <token>

Env vars:
  WHISPER_MODEL   model size: tiny | base | small | medium  (default: small)
  WHISPER_TOKEN   bearer token required on /transcribe (set by systemd unit)
  WHISPER_DEVICE  cpu | cuda  (default: cpu)
  WHISPER_COMPUTE int8 | float16 | float32  (default: int8 for CPU)
"""

from __future__ import annotations

import os
import tempfile
import time

from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from faster_whisper import WhisperModel

MODEL_SIZE = os.environ.get("WHISPER_MODEL", "small")
AUTH_TOKEN = os.environ.get("WHISPER_TOKEN", "")
DEVICE = os.environ.get("WHISPER_DEVICE", "cpu")
COMPUTE = os.environ.get("WHISPER_COMPUTE", "int8")
# Use all available CPU cores for transcription (default = os.cpu_count()).
# On the 2-vCPU jarvis-vm this roughly halves latency vs single-thread.
CPU_THREADS = int(os.environ.get("WHISPER_CPU_THREADS", str(os.cpu_count() or 2)))

app = FastAPI(title="JARVIS Whisper", version="1.0")

# Load model once at startup — kept warm in memory for fast subsequent calls.
# int8 on CPU keeps RAM ~1GB for small, with good accuracy for mixed
# Roman-Urdu + English voice commands.
_model: WhisperModel | None = None


def _get_model() -> WhisperModel:
    global _model
    if _model is None:
        _model = WhisperModel(
            MODEL_SIZE,
            device=DEVICE,
            compute_type=COMPUTE,
            cpu_threads=CPU_THREADS,
        )
    return _model


@app.on_event("startup")
def _warmup() -> None:
    # Pre-load the model so the first real request isn't slow.
    _get_model()


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "model": MODEL_SIZE,
        "device": DEVICE,
        "compute": COMPUTE,
        "cpu_threads": CPU_THREADS,
        "loaded": _model is not None,
    }


@app.post("/transcribe")
async def transcribe(
    file: UploadFile = File(...),
    authorization: str | None = Header(default=None),
    x_language: str | None = Header(default=None),
) -> dict:
    # Bearer token auth — reject if token configured and header doesn't match.
    if AUTH_TOKEN:
        expected = f"Bearer {AUTH_TOKEN}"
        if authorization != expected:
            raise HTTPException(status_code=401, detail="unauthorized")

    raw = await file.read()
    if not raw:
        return {"text": "", "language": "unknown", "duration": 0.0, "time_sec": 0.0}

    # Write to a temp file — faster-whisper decodes via PyAV/ffmpeg.
    suffix = os.path.splitext(file.filename or "audio.wav")[1] or ".wav"
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(raw)
            tmp_path = tmp.name

        start = time.perf_counter()
        model = _get_model()
        # language=None → auto-detect. Caller can force via X-Language header.
        segments, info = model.transcribe(
            tmp_path,
            language=(x_language or None),
            beam_size=1,          # beam_size=1 is fastest; good enough for commands
            vad_filter=True,      # voice-activity-detection trims silence → faster
        )
        text = " ".join(seg.text.strip() for seg in segments).strip()
        elapsed = round(time.perf_counter() - start, 2)
        return {
            "text": text,
            "language": getattr(info, "language", "unknown"),
            "duration": round(getattr(info, "duration", 0.0), 2),
            "time_sec": elapsed,
        }
    except Exception as e:  # never crash — return empty on error
        return {"text": "", "language": "error", "duration": 0.0, "time_sec": 0.0, "error": str(e)[:200]}
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
