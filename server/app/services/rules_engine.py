"""Rules Engine — evaluates rules against new tasks and applies actions."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.rule import Rule
from app.models.task import Task

log = get_logger(__name__)


class RulesEngine:
    """Evaluates all enabled rules against a task and applies matching actions.

    Called by the ChunkProcessor after task extraction.

    Rule matching logic:
        - Each condition field (match_action_type, match_priority, etc.) is optional
        - If a condition is set, the task must match it
        - ALL set conditions must match (AND logic)
        - If all conditions match → apply the rule's actions
    """

    async def evaluate(self, task: Task, db: AsyncSession) -> dict:
        """Evaluate all rules against a task. Returns applied actions summary."""
        result = await db.execute(
            select(Rule).where(Rule.enabled == True)
        )
        rules = result.scalars().all()

        applied = []

        for rule in rules:
            if self._matches(rule, task):
                self._apply(rule, task)
                applied.append(rule.name)
                log.info(
                    "rule_matched",
                    rule=rule.name,
                    task_id=task.id,
                    auto_approve=rule.auto_approve,
                )

        if applied:
            await db.commit()

        return {"applied_rules": applied, "count": len(applied)}

    @staticmethod
    def _matches(rule: Rule, task: Task) -> bool:
        """Check if all set conditions on the rule match the task."""
        if rule.match_action_type and task.action_type != rule.match_action_type:
            return False

        if rule.match_priority and task.priority != rule.match_priority:
            return False

        if rule.match_assigned_to:
            if not task.assigned_to or rule.match_assigned_to.lower() not in task.assigned_to.lower():
                return False

        if rule.match_keyword:
            keyword = rule.match_keyword.lower()
            title = (task.title or "").lower()
            desc = (task.description or "").lower()
            source = (task.source_text or "").lower()
            if keyword not in title and keyword not in desc and keyword not in source:
                return False

        if rule.match_window:
            # match_window checks against the source chunk's window context
            source = (task.source_text or "").lower()
            if rule.match_window.lower() not in source:
                return False

        return True

    @staticmethod
    def _apply(rule: Rule, task: Task) -> None:
        """Apply rule actions to the task."""
        if rule.auto_approve:
            task.status = "approved"
            task.requires_approval = False

        if rule.override_priority:
            task.priority = rule.override_priority

        if rule.override_assigned_to:
            task.assigned_to = rule.override_assigned_to
