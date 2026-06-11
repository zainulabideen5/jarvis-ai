"""UniversalEngine — the agent loop.

task → brain thinks (JSON step) → tool runs → observation back to brain →
... → done. No per-app code; the brain composes the universal tools.
"""

from __future__ import annotations

import asyncio
import inspect
import json

from app.core.logging import get_logger
from app.services.universal_engine.brain import BrainError, get_brain
from app.services.universal_engine.tools import TOOLS, TOOLS_DOC

log = get_logger(__name__)

MAX_STEPS = 15

SYSTEM_PROMPT = f"""You are a desktop-automation planner for a Windows PC assistant app.
The app executes actions on the user's own computer; your job is only to pick
the next action. Respond with exactly one JSON object per turn, nothing else.
Any external/connector tools in your environment are disabled — never invoke
them; the action names below are just strings inside your JSON answer.

To run an action (use EXACTLY these two keys, nothing extra):
{{"tool": "<action_name>", "args": {{...}}}}

When the task is complete (or cannot be completed) — EXACTLY these two keys,
no extra fields; everything the user should see goes inside reply itself:
{{"done": true, "reply": "<answer in Roman Urdu>"}}
Example: {{"done": true, "reply": "11 windows khuli hain: Chrome, VS Code, Notepad, Settings..."}}

AVAILABLE ACTIONS:
{TOOLS_DOC}

GUIDELINES:
1. Call focus_window before type_text/press_keys so input lands in the right window.
2. Inspect a window with ui_tree before clicking/typing inside it — don't guess element names.
3. Prefer powershell for all file/folder work (rename, move, list, create, batch).
4. For websites use open_url; to interact with page content use ui_tree on the browser window.
5. screenshot_check is a last resort (games or empty ui_tree only).
6. After opening an app, wait 1-2s, then ui_tree.
7. You have at most {MAX_STEPS} steps — as soon as you have what you need,
   return the done object immediately. Do not keep exploring.
8. If an action fails, try one alternative; after two failures finish with done=true and explain briefly.
9. Prefer the dedicated actions (list_windows, find_files, open_app...) over powershell when one fits.
10. Action results are reliable — when a result says ok=true, move on; do not
    spend extra steps re-verifying.
11. The final reply must be in Roman Urdu, short and direct.
"""


class UniversalEngine:
    """Singleton agent-loop runner."""

    _instance: "UniversalEngine | None" = None

    @classmethod
    def get(cls) -> "UniversalEngine":
        if cls._instance is None:
            cls._instance = UniversalEngine()
        return cls._instance

    async def run(self, task: str, max_steps: int = MAX_STEPS) -> dict:
        """Run one task to completion. Returns {ok, reply, steps}."""
        task = (task or "").strip()
        if not task:
            return {"ok": False, "reply": "Task khali hai", "steps": []}

        brain = get_brain()
        if not brain.is_available():
            return {
                "ok": False,
                "reply": (
                    "Claude CLI install nahi hai — brain nahi chal sakta. "
                    "Install: npm install -g @anthropic-ai/claude-code"
                ),
                "steps": [],
            }

        transcript: list[dict] = [{"role": "user", "content": f"TASK: {task}"}]
        steps: list[dict] = []

        for step_no in range(1, max_steps + 1):
            raw = None
            last_err: BrainError | None = None
            # The CLI's safety classifier is flaky on borderline automation
            # transcripts — a straight retry usually passes. 3 attempts.
            for attempt in range(3):
                try:
                    raw = await asyncio.to_thread(brain.think, SYSTEM_PROMPT, transcript)
                    break
                except BrainError as e:
                    last_err = e
                    log.warning("engine_brain_error", attempt=attempt + 1, error=str(e)[:200])
                    await asyncio.sleep(1.5 * (attempt + 1))
            if raw is None:
                return {"ok": False, "reply": f"Brain error: {last_err}", "steps": steps}

            decision = _normalize_decision(_parse_decision(raw))
            if decision is None:
                # Give the brain one nudge to fix its formatting
                transcript.append({"role": "assistant", "content": raw[:1500]})
                transcript.append({
                    "role": "user",
                    "content": "FORMAT ERROR: sirf ek valid JSON object do (tool ya done).",
                })
                steps.append({"step": step_no, "error": "unparseable brain output"})
                continue

            if decision.get("done"):
                reply = str(decision.get("reply") or "Ho gaya.")
                log.info("engine_done", task=task[:80], steps=len(steps))
                return {"ok": True, "reply": reply, "steps": steps}

            tool_name = str(decision.get("tool") or "")
            args = decision.get("args") or {}
            fn = TOOLS.get(tool_name)

            if fn is None:
                result = {
                    "ok": False,
                    "error": f"unknown tool: {tool_name}. Valid: {', '.join(TOOLS)}",
                }
            else:
                call_args = _filter_args(fn, args)
                try:
                    result = await asyncio.to_thread(lambda: fn(**call_args))
                except TypeError as e:
                    result = {"ok": False, "error": f"galat args: {e}"}
                except Exception as e:
                    result = {"ok": False, "error": str(e)[:300]}

            log.info(
                "engine_step",
                step=step_no, tool=tool_name,
                ok=bool(result.get("ok")),
            )
            steps.append({
                "step": step_no,
                "tool": tool_name,
                "args": args,
                "ok": bool(result.get("ok")),
                "raw": raw[:500],
                "result": json.dumps(result, ensure_ascii=False, default=str)[:300],
            })

            transcript.append({"role": "assistant", "content": json.dumps(decision, ensure_ascii=False)})
            remaining = max_steps - step_no
            transcript.append({
                "role": "user",
                "content": (
                    "RESULT: " + json.dumps(result, ensure_ascii=False, default=str)[:4500]
                    + f"\n({remaining} steps baqi. Jab done JSON do to poora jawab"
                    " — naam, numbers, results — reply field ke ANDAR likhna.)"
                ),
            })

        return {
            "ok": False,
            "reply": f"{max_steps} steps mein task poora nahi hua — task chhota karke dobara try karo.",
            "steps": steps,
        }


def _filter_args(fn, args: dict) -> dict:
    """Keep only kwargs the tool actually accepts (model adds stray keys)."""
    try:
        valid = set(inspect.signature(fn).parameters)
    except (TypeError, ValueError):
        return args
    return {k: v for k, v in (args or {}).items() if k in valid}


def _normalize_decision(decision: dict | None) -> dict | None:
    """Tolerate near-miss shapes the model produces.

    Seen in the wild: {"action": "done", "summary": "..."},
    {"tool": "powershell", "command": "..."} (args at top level),
    {"reply": "..."} without done.
    """
    if decision is None:
        return None

    tool = str(decision.get("tool") or decision.get("action") or "").strip()

    def _find_reply(d: dict) -> str:
        for key in ("reply", "summary", "message"):
            v = d.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        nested = d.get("args")
        if isinstance(nested, dict):
            return _find_reply(nested)
        return ""

    reply = _find_reply(decision)

    # Done shapes
    if decision.get("done") or tool.lower() in ("done", "finish", "finished", "complete"):
        # Model sometimes parks the real answer in extra keys and leaves a
        # generic reply — salvage that data so the user actually sees it.
        extras = {
            k: v for k, v in decision.items()
            if k not in ("done", "reply", "summary", "message", "tool", "action",
                         "status", "step", "soch", "reason")
        }
        if extras and len(reply) < 40:
            detail = json.dumps(extras, ensure_ascii=False, default=str)[:1500]
            reply = (reply + "\n" + detail).strip()
        return {"done": True, "reply": reply or "Ho gaya."}
    if reply and tool.lower() in ("", "none", "reply"):
        return {"done": True, "reply": reply}

    # Tool shapes — hoist stray top-level keys and "params" wrappers into
    # args (invalid keys get dropped later by _filter_args against the
    # tool's real signature)
    args = decision.get("args")
    if not isinstance(args, dict):
        args = {}
    params = decision.get("params")
    if isinstance(params, dict):
        for k, v in params.items():
            args.setdefault(k, v)
    for k, v in decision.items():
        if k not in ("tool", "action", "args", "params", "done", "reply") and k not in args:
            args[k] = v
    return {"tool": tool, "args": args}


def _parse_decision(raw: str) -> dict | None:
    """Extract the first JSON object from brain output (tolerates fences/prose)."""
    s = (raw or "").strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:]
        s = s.strip()
    start = s.find("{")
    if start == -1:
        return None
    decoder = json.JSONDecoder()
    try:
        obj, _ = decoder.raw_decode(s[start:])
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None
