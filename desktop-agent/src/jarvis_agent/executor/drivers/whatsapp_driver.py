"""WhatsApp Web driver — sends messages via Playwright browser automation."""

from __future__ import annotations

import asyncio

from jarvis_agent.config import AgentConfig
from jarvis_agent.executor.drivers.base import ActionResult, BaseDriver
from jarvis_agent.utils.logging import get_logger

log = get_logger(__name__)


class WhatsAppDriver(BaseDriver):
    """Automate WhatsApp Web via Playwright.

    Uses a persistent browser profile so you only scan the QR code once.
    The browser stays open in the background between actions.
    """

    def __init__(self, config: AgentConfig):
        self._config = config
        self._browser = None
        self._context = None
        self._page = None

    async def _ensure_browser(self):
        """Launch browser if not already running."""
        if self._page and not self._page.is_closed():
            return

        from playwright.async_api import async_playwright

        pw = await async_playwright().start()
        user_data = str(self._config.data_dir / "whatsapp_profile")

        self._browser = await pw.chromium.launch_persistent_context(
            user_data_dir=user_data,
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        # Use first page or create one
        if self._browser.pages:
            self._page = self._browser.pages[0]
        else:
            self._page = await self._browser.new_page()

        # Navigate to WhatsApp Web if not already there
        if "web.whatsapp.com" not in (self._page.url or ""):
            await self._page.goto("https://web.whatsapp.com", wait_until="networkidle")
            # Wait for the app to load (user may need to scan QR first time)
            log.info("whatsapp_waiting_for_load")
            await self._page.wait_for_selector(
                'div[contenteditable="true"][data-tab="3"]',
                timeout=120_000,
            )
            log.info("whatsapp_ready")

    async def execute(self, task: dict, payload: dict) -> ActionResult:
        to = payload.get("to")
        message = payload.get("message") or task.get("description") or task.get("title", "")

        if not to:
            return ActionResult(success=False, error="No recipient ('to') specified")
        if not message:
            return ActionResult(success=False, error="No message to send")

        try:
            await self._ensure_browser()
            return await self._send_message(to, message)
        except Exception as e:
            log.error("whatsapp_failed", error=str(e))
            return ActionResult(success=False, error=f"WhatsApp send failed: {e}")

    async def _send_message(self, contact: str, message: str) -> ActionResult:
        page = self._page

        # Use WhatsApp's search to find the contact
        search_box = page.locator('div[contenteditable="true"][data-tab="3"]')
        await search_box.click()
        await search_box.fill("")
        await search_box.type(contact, delay=50)
        await asyncio.sleep(1.5)

        # Click the first matching contact
        contact_el = page.locator(f'span[title*="{contact}"]').first
        try:
            await contact_el.click(timeout=10_000)
        except Exception:
            return ActionResult(
                success=False,
                error=f"Contact '{contact}' not found in WhatsApp",
            )

        await asyncio.sleep(0.5)

        # Type message in the message input
        msg_box = page.locator(
            'div[contenteditable="true"][data-tab="10"]'
        ).last
        await msg_box.click()
        await msg_box.fill(message)

        # Press Enter to send
        await page.keyboard.press("Enter")
        await asyncio.sleep(1)

        log.info("whatsapp_sent", to=contact)
        return ActionResult(success=True, message=f"WhatsApp message sent to {contact}")

    async def close(self):
        """Close the browser."""
        if self._browser:
            await self._browser.close()
            self._browser = None
            self._page = None
