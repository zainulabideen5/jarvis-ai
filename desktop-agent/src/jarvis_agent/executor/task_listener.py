"""Task Listener — polls server for approved tasks, shows approval popup, executes."""

from __future__ import annotations

import asyncio
import json
import urllib.request

from jarvis_agent.config import AgentConfig
from jarvis_agent.executor.router import ActionRouter
from jarvis_agent.ui.approval import show_approval_popup
from jarvis_agent.utils.logging import get_logger

log = get_logger(__name__)


class TaskListener:
    """Polls the server for approved tasks and executes them.

    Flow:
        1. GET /api/tasks?status=approved → list of tasks
        2. If task.requires_approval → show desktop popup, reject if denied
        3. Execute via ActionRouter
        4. PATCH result back to server
        5. Sleep, repeat
    """

    def __init__(self, config: AgentConfig):
        self._config = config
        self._router = ActionRouter(config)
        self._poll_interval = config.task_poll_interval_sec
        self._api_url = config.server_api_url

    async def run(self) -> None:
        """Main polling loop — runs forever."""
        log.info("task_listener_started", poll_interval=self._poll_interval)

        while True:
            try:
                tasks = await asyncio.to_thread(self._fetch_approved_tasks)

                for task in tasks:
                    # Show desktop approval popup if task requires it
                    if task.get("requires_approval", True):
                        approved = await show_approval_popup(task)
                        if not approved:
                            await asyncio.to_thread(
                                self._update_task_status, task["id"], "rejected"
                            )
                            log.info("task_rejected_by_user", task_id=task["id"])
                            continue

                    log.info(
                        "executing_task",
                        task_id=task["id"],
                        title=task.get("title", "")[:60],
                        action=task.get("action_type"),
                    )

                    # Mark as in_progress
                    await asyncio.to_thread(
                        self._update_task_status, task["id"], "in_progress"
                    )

                    # Execute
                    result = await self._router.execute(task)

                    # Report result back
                    if result.success:
                        await asyncio.to_thread(
                            self._update_task_status, task["id"], "completed"
                        )
                        log.info(
                            "task_completed",
                            task_id=task["id"],
                            message=result.message,
                        )
                    else:
                        await asyncio.to_thread(
                            self._update_task_status, task["id"], "failed",
                            result.error,
                        )
                        log.warning(
                            "task_failed",
                            task_id=task["id"],
                            error=result.error,
                        )

            except Exception as e:
                log.error("task_listener_error", error=str(e))

            await asyncio.sleep(self._poll_interval)

    def _fetch_approved_tasks(self) -> list[dict]:
        """GET /api/tasks?status=approved"""
        url = f"{self._api_url}/tasks?status=approved&limit=10"
        req = urllib.request.Request(url)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())
        except Exception as e:
            log.debug("fetch_tasks_failed", error=str(e))
            return []

    def _update_task_status(
        self, task_id: int, status: str, error_msg: str = ""
    ) -> None:
        """PATCH /api/tasks/{id}/status"""
        url = f"{self._api_url}/tasks/{task_id}/status"
        body = json.dumps({"status": status, "error": error_msg}).encode()
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="PATCH",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
        except Exception as e:
            log.warning("status_update_failed", task_id=task_id, error=str(e))

    async def close(self) -> None:
        await self._router.close()
