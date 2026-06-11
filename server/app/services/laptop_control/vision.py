"""Screenshot + vision — Gemini Vision (saves Groq quota).

Why Gemini for vision: Groq's text quota (100k tokens/day) is shared between
chat AND vision. Vision calls are huge (image + prompt = 5-10k tokens each)
and quickly exhaust the daily budget. Gemini's free tier vision is generous
(1500 req/day) and independent of Groq's quota, so chat keeps working even
when vision is being heavily used.
"""

from __future__ import annotations

import io

from app.core.config import ServerConfig
from app.core.logging import get_logger

log = get_logger(__name__)

VISION_PROMPT = """Tu ek screen analyzer hai. Ye laptop screen ka screenshot hai. User ne kuch pucha hai — screen dekh ke jawab de.

User ka sawaal: __QUERY__

Roman Urdu + English mein jawab de. Chhota, kaam ka. Agar click karna hai to exact (x, y) coordinates do."""


class VisionDriver:
    """Uses Gemini Vision (gemini-2.0-flash-exp) to analyze screenshots."""

    def __init__(self, config: ServerConfig):
        self._config = config
        self._gemini = None

    def _get_gemini(self):
        """Lazy-init the Gemini client. Raises if API key missing."""
        if self._gemini is None:
            if not self._config.gemini_api_key:
                raise ValueError("JARVIS_GEMINI_API_KEY not set — vision needs Gemini")
            import google.generativeai as genai
            genai.configure(api_key=self._config.gemini_api_key)
            # Flash 2.0 — fast, vision-capable, free tier-friendly
            self._gemini = genai.GenerativeModel("gemini-2.0-flash-exp")
        return self._gemini

    @staticmethod
    def capture_screen() -> bytes:
        """Capture the current screen as PNG bytes."""
        try:
            import mss
            from PIL import Image

            with mss.mss() as sct:
                monitor = sct.monitors[1]  # Primary monitor
                img = sct.grab(monitor)
                pil = Image.frombytes("RGB", img.size, img.bgra, "raw", "BGRX")

                # Resize to keep payloads small. Gemini accepts up to 3072 but
                # 1600 is plenty for screen analysis and keeps the call fast.
                max_dim = 1600
                if max(pil.size) > max_dim:
                    scale = max_dim / max(pil.size)
                    new_size = (int(pil.size[0] * scale), int(pil.size[1] * scale))
                    pil = pil.resize(new_size, Image.LANCZOS)

                buf = io.BytesIO()
                pil.save(buf, format="PNG", optimize=True)
                return buf.getvalue()
        except Exception as e:
            log.warning("screenshot_failed", error=str(e))
            return b""

    def analyze(self, query: str) -> tuple[bool, str]:
        """Analyze current screen with a query via Gemini Vision."""
        img_bytes = self.capture_screen()
        if not img_bytes:
            return False, "Screenshot nahi le saka"

        try:
            model = self._get_gemini()
            prompt = VISION_PROMPT.replace("__QUERY__", query or "Screen pe kya dikh raha hai?")

            # Gemini accepts a list of parts — text + image. PIL Image works directly.
            from PIL import Image
            img = Image.open(io.BytesIO(img_bytes))

            response = model.generate_content(
                [prompt, img],
                generation_config={
                    "temperature": 0.2,
                    "max_output_tokens": 600,
                },
            )
            reply = (response.text or "").strip()
            if not reply:
                return False, "Gemini vision: khali response"
            return True, reply

        except Exception as e:
            log.warning("vision_analyze_failed", error=str(e))
            return False, f"Vision fail: {e}"
