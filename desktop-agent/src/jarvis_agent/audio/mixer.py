"""Audio resampler — reads from capture queues, resamples to 16kHz mono."""

from __future__ import annotations

import asyncio
from math import gcd

import numpy as np

from jarvis_agent.utils.logging import get_logger

log = get_logger(__name__)

TARGET_RATE = 16000
# Max 1 sec of audio at a time (safe for any sample rate)
MAX_FRAMES = 48000


def resample_audio(audio_int16: np.ndarray, source_rate: int) -> np.ndarray:
    """Resample int16 audio to 16kHz float32 [-1, 1]. Memory safe."""
    if len(audio_int16) == 0:
        return np.array([], dtype=np.float32)

    # Limit size
    if len(audio_int16) > MAX_FRAMES:
        audio_int16 = audio_int16[:MAX_FRAMES]

    audio_float = audio_int16.astype(np.float32) / 32768.0

    if source_rate == TARGET_RATE:
        return audio_float

    # Simple decimation instead of resample_poly (much less memory)
    ratio = TARGET_RATE / source_rate
    output_len = int(len(audio_float) * ratio)
    if output_len <= 0:
        return np.array([], dtype=np.float32)

    indices = np.linspace(0, len(audio_float) - 1, output_len).astype(int)
    return audio_float[indices]


async def mixer_task(
    sync_queue,
    output_queue: asyncio.Queue,
    source_name: str,
) -> None:
    """Async task: drains sync_queue, resamples, pushes to output_queue."""
    log.info("mixer_started", source=source_name)

    while True:
        try:
            audio_int16, source_rate = await sync_queue.get()

            # Skip if output queue backing up
            if output_queue.qsize() > 100:
                continue

            resampled = resample_audio(audio_int16, source_rate)
            if len(resampled) > 0:
                await output_queue.put((resampled, source_name))

        except MemoryError:
            log.warning("mixer_memory_skip", source=source_name)
            continue
        except Exception as e:
            log.debug("mixer_error", source=source_name, error=str(e))
            continue
