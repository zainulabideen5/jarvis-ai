"""Recipe store — the engine's "skill memory".

The first time the engine completes a task on an app it figures out the steps
the slow way (think → act, repeated). On success we save that working step
sequence keyed by intent (e.g. "send:teams"). Next time the same kind of task
comes, the engine REPLAYS the recipe: one planning call adapts the variable
bits (contact, message) and the steps run without per-step thinking — ~3x
faster. If replay fails (UI changed), the engine relearns and overwrites.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from app.core.logging import get_logger

log = get_logger(__name__)

# App name VARIANTS (casual Roman Urdu / typos) → canonical name. Both the
# task-text lookup and the step-based fallback map to the SAME canonical, so a
# recipe saved from "team pa" replays for "teams pe" and vice versa. (Key fix:
# "team" and "teams" must resolve to the same recipe.)
_APP_VARIANTS = {
    "teams": "teams", "team": "teams", "ms teams": "teams", "msteams": "teams",
    "whatsapp": "whatsapp", "whats app": "whatsapp", "wattsapp": "whatsapp",
    "watsapp": "whatsapp", "whatsap": "whatsapp",
    "slack": "slack", "discord": "discord", "telegram": "telegram",
    "gmail": "gmail", "outlook": "outlook",
    "excel": "excel", "word": "word", "powerpoint": "powerpoint", "ppt": "powerpoint",
    "notepad": "notepad", "chrome": "chrome", "edge": "edge", "explorer": "explorer",
    "spotify": "spotify", "vscode": "vscode", "vs code": "vscode",
    "calculator": "calculator",
}
# Longest variants first so "ms teams" matches before "teams"/"team".
_APP_KEYS_SORTED = sorted(_APP_VARIANTS, key=len, reverse=True)


def _detect_app(text: str) -> str | None:
    low = (text or "").lower()
    for variant in _APP_KEYS_SORTED:
        if variant in low:
            return _APP_VARIANTS[variant]
    return None
# Action verbs (Roman Urdu + English, with common typo/spelling variants) →
# grouped to a canonical action. Users type casual Roman Urdu ("bhaj", "bhj"),
# so keep this generous.
_ACTIONS = {
    "send": ("message", "msg", "send", "text",
             "bhej", "bhejo", "bhej", "bhaj", "bej", "bhj", "bhejna", "bhejdo",
             "bhejde", "bhejdena", "likho", "likh", "likhdo", "bhajo", "bhaaj"),
    "open": ("kholo", "khol", "open", "launch", "chalao", "chala", "kholdo"),
    "create": ("banao", "bana", "create", "naya", "new", "banado"),
    "search": ("dhoondo", "search", "find", "dhundo", "dhund"),
}


def _db_path() -> Path | None:
    for c in (Path("data/jarvis.db"), Path("server/data/jarvis.db"),
              Path(__file__).resolve().parents[3] / "data" / "jarvis.db"):
        if c.exists():
            return c
    # default location even if not yet created
    return Path(__file__).resolve().parents[3] / "data" / "jarvis.db"


def intent_key(task: str) -> str | None:
    """Derive a stable key like 'send:teams' from a task. None if we can't
    confidently group it (then the engine just runs normally — no recipe)."""
    low = f" {(task or '').lower()} "
    app = _detect_app(low)
    if not app:
        return None
    # File/attachment sends are a DIFFERENT flow than plain messages — keep
    # their recipe separate so a text-only recipe never replays for a file task.
    file_words = ("file", "document", "doc ", "docx", "pdf", "image", "photo",
                  "tasveer", "attach", "attachment", ".xls", ".png", ".jpg", ".zip")
    if any(w in low for w in file_words):
        return f"sendfile:{app}"
    action = None
    for canon, words in _ACTIONS.items():
        if any(w in low for w in words):
            action = canon
            break
    if not action:
        return None
    return f"{action}:{app}"


def intent_from_steps(steps: list[dict]) -> str | None:
    """Robust fallback: derive intent from what actually happened, so a
    typo-ridden task ('bhaj') still gets remembered. Looks at the app in the
    window titles + whether a type/send vs open happened."""
    app = None
    typed = False
    for s in steps:
        args = s.get("args", {}) or {}
        win = str(args.get("window_title") or "")
        app = app or _detect_app(win)
        if s.get("tool") in ("type_in_window", "set_text"):
            typed = True
        if s.get("tool") == "open_app":
            app = app or _detect_app(str(args.get("name") or ""))
    if not app:
        return None
    return f"{'send' if typed else 'open'}:{app}"


# Built-in recipes for common apps — make the FIRST run fast (these are the
# validated Teams flows). Placeholders get filled at replay time. open_app is
# safe even if the app is already open (it just focuses).
_DEFAULT_RECIPES = {
    # Teams' compose box is a WebView that does NOT accept set_text
    # (ValuePattern) — only keyboard works, so this is the reliable flow.
    # window_title is the STABLE "Teams" substring (full title changes per chat).
    "send:teams": [
        {"tool": "open_app", "args": {"name": "teams"}},
        {"tool": "wait", "args": {"seconds": 2}},
        {"tool": "click_element", "args": {"window_title": "Teams", "element_name": "<CONTACT>", "control_type": "TreeItem"}},
        {"tool": "type_in_window", "args": {"window_title": "Teams", "element_name": "Type a message", "text": "<MESSAGE>", "submit": True}},
    ],
    # NOTE: no sendfile:teams default — clipboard-paste attach doesn't work in
    # Teams' WebView. File-sends explore the real "Attach files" button flow
    # (normal mode) and learn it; then replay fast.
}


class RecipeStore:
    """Learned step sequences, one per intent key."""

    @staticmethod
    def ensure_table() -> None:
        p = _db_path()
        with sqlite3.connect(str(p)) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS engine_recipes (
                    intent_key TEXT PRIMARY KEY,
                    steps_json TEXT,
                    use_count  INTEGER DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()
        RecipeStore._seed_defaults()

    @staticmethod
    def _seed_defaults() -> None:
        """Ship built-in recipes for common apps so even the FIRST send is fast
        (no per-machine learning needed). Only inserted if missing — never
        overwrites a recipe the user's own runs have refined."""
        for key, steps in _DEFAULT_RECIPES.items():
            try:
                with sqlite3.connect(str(_db_path())) as conn:
                    exists = conn.execute(
                        "SELECT 1 FROM engine_recipes WHERE intent_key = ?", (key,)
                    ).fetchone()
                    if not exists:
                        conn.execute(
                            "INSERT INTO engine_recipes (intent_key, steps_json, use_count) "
                            "VALUES (?, ?, 0)",
                            (key, json.dumps(steps, ensure_ascii=False)),
                        )
                        conn.commit()
            except Exception as e:
                log.debug("seed_recipe_failed", intent=key, error=str(e))

    @staticmethod
    def get(key: str) -> list[dict] | None:
        if not key:
            return None
        try:
            with sqlite3.connect(str(_db_path())) as conn:
                row = conn.execute(
                    "SELECT steps_json FROM engine_recipes WHERE intent_key = ?", (key,)
                ).fetchone()
            if not row:
                return None
            steps = json.loads(row[0])
            return steps if isinstance(steps, list) and steps else None
        except Exception as e:
            log.debug("recipe_get_failed", error=str(e))
            return None

    @staticmethod
    def save(key: str, steps: list[dict]) -> None:
        """Save/replace the recipe for an intent. steps = [{tool, args}, ...]."""
        if not key or not steps:
            return
        try:
            payload = json.dumps(steps[:12], ensure_ascii=False)  # cap size
            with sqlite3.connect(str(_db_path())) as conn:
                conn.execute("""
                    INSERT INTO engine_recipes (intent_key, steps_json, use_count, updated_at)
                    VALUES (?, ?, 1, CURRENT_TIMESTAMP)
                    ON CONFLICT(intent_key) DO UPDATE SET
                        steps_json = excluded.steps_json,
                        use_count = engine_recipes.use_count + 1,
                        updated_at = CURRENT_TIMESTAMP
                """, (key, payload))
                conn.commit()
            log.info("recipe_saved", intent=key, steps=len(steps))
        except Exception as e:
            log.debug("recipe_save_failed", error=str(e))

    @staticmethod
    def list_all() -> list[dict]:
        try:
            with sqlite3.connect(str(_db_path())) as conn:
                rows = conn.execute(
                    "SELECT intent_key, use_count, updated_at FROM engine_recipes "
                    "ORDER BY use_count DESC"
                ).fetchall()
            return [{"intent": r[0], "use_count": r[1], "updated_at": r[2]} for r in rows]
        except Exception:
            return []
