"""Notion driver — creates pages/tasks via Notion API."""

from __future__ import annotations

import asyncio
import json
import urllib.request

from jarvis_agent.config import AgentConfig
from jarvis_agent.executor.drivers.base import ActionResult, BaseDriver
from jarvis_agent.utils.logging import get_logger

log = get_logger(__name__)


class NotionDriver(BaseDriver):
    """Create Notion pages/tasks via the Notion API.

    Env vars:
        JARVIS_NOTION_TOKEN — Notion integration token
        JARVIS_NOTION_DATABASE_ID — Default database to add pages to
    """

    def __init__(self, config: AgentConfig):
        self._config = config

    async def execute(self, task: dict, payload: dict) -> ActionResult:
        title = payload.get("title") or task.get("title", "Untitled")
        content = payload.get("content") or task.get("description") or ""
        database_id = payload.get("database_id") or self._config.notion_database_id

        token = self._config.notion_token
        if not token:
            return ActionResult(
                success=False,
                error="Notion not configured. Set JARVIS_NOTION_TOKEN",
            )
        if not database_id:
            return ActionResult(
                success=False,
                error="No Notion database ID. Set JARVIS_NOTION_DATABASE_ID or include in payload",
            )

        try:
            return await asyncio.to_thread(
                self._create_page, token, database_id, title, content
            )
        except Exception as e:
            return ActionResult(success=False, error=f"Notion create failed: {e}")

    @staticmethod
    def _create_page(
        token: str, database_id: str, title: str, content: str
    ) -> ActionResult:
        body = json.dumps({
            "parent": {"database_id": database_id},
            "properties": {
                "Name": {
                    "title": [{"text": {"content": title}}]
                },
            },
            "children": [
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [{"text": {"content": content}}]
                    },
                }
            ] if content else [],
        }).encode()

        req = urllib.request.Request(
            "https://api.notion.com/v1/pages",
            data=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Notion-Version": "2022-06-28",
            },
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())

        page_id = data.get("id", "")
        log.info("notion_page_created", page_id=page_id, title=title)
        return ActionResult(
            success=True,
            message=f"Notion page created: {title}",
            details={"page_id": page_id},
        )
