"""Generic browser automation driver for social media platforms.

Handles LinkedIn, Twitter/X, Instagram, Facebook via Playwright.
Each platform has its own send logic using platform-specific selectors.
"""

from __future__ import annotations

import asyncio

from jarvis_agent.config import AgentConfig
from jarvis_agent.executor.drivers.base import ActionResult, BaseDriver
from jarvis_agent.utils.logging import get_logger

log = get_logger(__name__)


class BrowserDriver(BaseDriver):
    """Automate social media actions via Playwright.

    Uses a persistent Chromium profile so you stay logged in.
    Supports: LinkedIn, Twitter/X, Instagram, Facebook.
    """

    def __init__(self, config: AgentConfig):
        self._config = config
        self._browser = None
        self._page = None

    async def _ensure_browser(self):
        if self._page and not self._page.is_closed():
            return

        from playwright.async_api import async_playwright

        pw = await async_playwright().start()
        user_data = str(self._config.data_dir / "browser_profile")

        self._browser = await pw.chromium.launch_persistent_context(
            user_data_dir=user_data,
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        if self._browser.pages:
            self._page = self._browser.pages[0]
        else:
            self._page = await self._browser.new_page()

    async def execute(self, task: dict, payload: dict) -> ActionResult:
        platform = payload.get("platform", "").lower()
        action = payload.get("action", "post")

        handlers = {
            "linkedin": self._linkedin,
            "twitter": self._twitter,
            "x": self._twitter,
            "instagram": self._instagram,
            "facebook": self._facebook,
            "fb": self._facebook,
        }

        handler = handlers.get(platform)
        if not handler:
            return ActionResult(
                success=False,
                error=f"Unsupported browser platform: {platform}",
            )

        try:
            await self._ensure_browser()
            return await handler(task, payload, action)
        except Exception as e:
            log.error("browser_action_failed", platform=platform, error=str(e))
            return ActionResult(success=False, error=f"{platform} action failed: {e}")

    # ── LinkedIn ──────────────────────────────────────────

    async def _linkedin(self, task: dict, payload: dict, action: str) -> ActionResult:
        page = self._page
        message = payload.get("message") or task.get("description") or task.get("title", "")
        to = payload.get("to", "")

        if action == "message" and to:
            # Send a DM
            await page.goto("https://www.linkedin.com/messaging/", wait_until="networkidle")
            await asyncio.sleep(2)

            # Search for the person
            search = page.locator('input[placeholder*="Search messages"]').first
            await search.click()
            await search.fill(to)
            await asyncio.sleep(2)

            # Click first result
            await page.locator(f'text="{to}"').first.click(timeout=10_000)
            await asyncio.sleep(1)

            # Type and send message
            msg_box = page.locator('div[role="textbox"]').last
            await msg_box.click()
            await msg_box.fill(message)
            await page.keyboard.press("Enter")
            await asyncio.sleep(1)

            log.info("linkedin_message_sent", to=to)
            return ActionResult(success=True, message=f"LinkedIn message sent to {to}")

        else:
            # Create a post
            await page.goto("https://www.linkedin.com/feed/", wait_until="networkidle")
            await asyncio.sleep(2)

            # Click "Start a post"
            await page.locator('button:has-text("Start a post")').first.click(timeout=10_000)
            await asyncio.sleep(1)

            # Type the post
            editor = page.locator('div[role="textbox"]').first
            await editor.click()
            await editor.fill(message)
            await asyncio.sleep(0.5)

            # Click Post button
            await page.locator('button:has-text("Post")').last.click()
            await asyncio.sleep(2)

            log.info("linkedin_post_created")
            return ActionResult(success=True, message="LinkedIn post created")

    # ── Twitter / X ───────────────────────────────────────

    async def _twitter(self, task: dict, payload: dict, action: str) -> ActionResult:
        page = self._page
        message = payload.get("message") or task.get("description") or task.get("title", "")
        to = payload.get("to", "")

        if action == "message" and to:
            await page.goto(f"https://x.com/messages", wait_until="networkidle")
            await asyncio.sleep(2)

            # New message
            await page.locator('[data-testid="NewDM_Button"]').click(timeout=10_000)
            await asyncio.sleep(1)

            # Search user
            search = page.locator('input[placeholder*="Search"]').first
            await search.fill(to)
            await asyncio.sleep(2)
            await page.locator(f'text="{to}"').first.click(timeout=10_000)
            await page.locator('button[data-testid="nextButton"]').click()
            await asyncio.sleep(1)

            # Type message
            msg_box = page.locator('[data-testid="dmComposerTextInput"]')
            await msg_box.fill(message)
            await page.locator('[data-testid="dmComposerSendButton"]').click()
            await asyncio.sleep(1)

            log.info("twitter_dm_sent", to=to)
            return ActionResult(success=True, message=f"Twitter DM sent to {to}")

        else:
            # Post a tweet
            await page.goto("https://x.com/compose/post", wait_until="networkidle")
            await asyncio.sleep(2)

            editor = page.locator('[data-testid="tweetTextarea_0"]')
            await editor.click()
            await editor.fill(message)
            await asyncio.sleep(0.5)

            await page.locator('[data-testid="tweetButton"]').click()
            await asyncio.sleep(2)

            log.info("tweet_posted")
            return ActionResult(success=True, message="Tweet posted")

    # ── Instagram ─────────────────────────────────────────

    async def _instagram(self, task: dict, payload: dict, action: str) -> ActionResult:
        page = self._page
        message = payload.get("message") or task.get("description") or task.get("title", "")
        to = payload.get("to", "")

        if not to:
            return ActionResult(success=False, error="Instagram requires a 'to' recipient for DMs")

        # Instagram DM
        await page.goto("https://www.instagram.com/direct/inbox/", wait_until="networkidle")
        await asyncio.sleep(2)

        # New message button
        try:
            await page.locator('svg[aria-label="New message"]').click(timeout=5_000)
        except Exception:
            await page.locator('[aria-label="New message"]').click(timeout=5_000)
        await asyncio.sleep(1)

        # Search for recipient
        search = page.locator('input[placeholder="Search..."]')
        await search.fill(to)
        await asyncio.sleep(2)

        # Select first result
        await page.locator(f'text="{to}"').first.click(timeout=10_000)
        await asyncio.sleep(0.5)

        # Click Chat / Next
        await page.locator('button:has-text("Chat")').or_(page.locator('button:has-text("Next")')).first.click()
        await asyncio.sleep(1)

        # Type and send
        msg_box = page.locator('textarea[placeholder="Message..."]').or_(
            page.locator('[contenteditable="true"]')
        ).first
        await msg_box.fill(message)
        await page.keyboard.press("Enter")
        await asyncio.sleep(1)

        log.info("instagram_dm_sent", to=to)
        return ActionResult(success=True, message=f"Instagram DM sent to {to}")

    # ── Facebook ──────────────────────────────────────────

    async def _facebook(self, task: dict, payload: dict, action: str) -> ActionResult:
        page = self._page
        message = payload.get("message") or task.get("description") or task.get("title", "")
        to = payload.get("to", "")

        if action == "message" and to:
            await page.goto("https://www.facebook.com/messages/", wait_until="networkidle")
            await asyncio.sleep(2)

            # New message
            await page.locator('[aria-label="New message"]').click(timeout=10_000)
            await asyncio.sleep(1)

            # Search recipient
            search = page.locator('input[placeholder*="Search"]').first
            await search.fill(to)
            await asyncio.sleep(2)
            await page.locator(f'text="{to}"').first.click(timeout=10_000)
            await asyncio.sleep(1)

            # Type and send
            msg_box = page.locator('[aria-label="Message"]').or_(
                page.locator('div[contenteditable="true"]')
            ).last
            await msg_box.click()
            await msg_box.fill(message)
            await page.keyboard.press("Enter")
            await asyncio.sleep(1)

            log.info("facebook_message_sent", to=to)
            return ActionResult(success=True, message=f"Facebook message sent to {to}")

        else:
            # Create a post
            await page.goto("https://www.facebook.com/", wait_until="networkidle")
            await asyncio.sleep(2)

            await page.locator('[aria-label*="What\'s on your mind"]').or_(
                page.locator('div[role="button"]:has-text("What\'s on your mind")')
            ).first.click(timeout=10_000)
            await asyncio.sleep(1)

            editor = page.locator('div[contenteditable="true"]').first
            await editor.fill(message)
            await asyncio.sleep(0.5)

            await page.locator('div[aria-label="Post"]').or_(
                page.locator('button:has-text("Post")')
            ).first.click()
            await asyncio.sleep(2)

            log.info("facebook_post_created")
            return ActionResult(success=True, message="Facebook post created")

    async def close(self):
        if self._browser:
            await self._browser.close()
            self._browser = None
            self._page = None
