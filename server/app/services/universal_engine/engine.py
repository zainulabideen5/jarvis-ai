"""UniversalEngine — the agent loop.

task → brain thinks (JSON step) → tool runs → observation back to brain →
... → done. No per-app code; the brain composes the universal tools.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time

from app.core.logging import get_logger
from app.services.universal_engine.brain import BrainError, get_brain
from app.services.universal_engine.tools import TOOLS, TOOLS_DOC

log = get_logger(__name__)

MAX_STEPS = 15

SYSTEM_PROMPT = f"""You are the planning core of a premium personal Windows assistant
(JARVIS). The user talks casually in Roman Urdu / English mix — often short,
informal, or with the real intent implied rather than spelled out. Your job is
to understand what they REALLY want and get it done smoothly on their own PC.
Respond with exactly one JSON object per turn, nothing else. Any external/
connector tools in your environment are disabled — never invoke them; the
action names below are just strings inside your JSON answer.

You can return one of THREE shapes:

1. Run an action (EXACTLY these keys):
{{"tool": "<action_name>", "args": {{...}}}}

2. Finish (task done OR truly impossible) — everything the user should see goes
   inside reply:
{{"done": true, "reply": "<answer in Roman Urdu>"}}

3. ASK — only as a LAST resort, for info that is NOT on screen and you cannot
   discover yourself (e.g. the message text wasn't given). Format:
{{"ask": "<ek chhota sawal Roman Urdu mein>"}}

GOLDEN RULE — DEKHO, MAT POOCHO:
Tum khud screen dekh sakte ho via ui_tree. So NEVER ask the user things you can
find yourself. These are FORBIDDEN questions (find them with ui_tree instead):
  ✗ "App khula hai ya nahi?" → list_windows / ui_tree se khud dekho
  ✗ "Contact ka naam sahi hai?" → ui_tree mein contact list khud parho
  ✗ "Search box kahan hai?" → ui_tree mein "Search" ComboBox/Edit khud dhoondo
  ✗ "Sab contacts dekhoon?" → haan, ui_tree chalao aur khud dekho
Sirf tab ask karo jab cheez screen par hai hi nahi (jaise message ka text user
ne diya hi nahi). Warna ACT karo.

AVAILABLE ACTIONS:
{TOOLS_DOC}

PLAYBOOK — kisi chat app (Teams/WhatsApp/Slack) mein message bhejna.
(Naam yahan SIRF misaal hain — asli naam jo user ne diya woh use karo, aur jo
ui_tree mein dikhe usi exact text se. Kuch hardcode mat samjho.)
1. PEHLA ui_tree BINA kisi filter ke chalao (sirf window_title do, control_type
   aur name_contains KHALI). Poori window ek dafa dekho — control_type guess
   mat karo, jo asli hai woh list mein nazar aayega.
   NOTE: contact aksar [TreeItem]/[ListItem] hota hai jaise "Chat <NAAM> Available",
   aur search aksar [ComboBox] "Search" hota hai.
2. User ke diye naam ka contact list mein dhoondo (jo bhi naam ho). Dikhe to
   SEEDHE click_element karo us par — EXACT name jo ui_tree mein likha hai
   (jaise element_name="Chat <NAAM> Available", control_type="TreeItem").
   Na dikhe TO "Search" ComboBox par click_element, phir type_in_window se naam
   likho, ui_tree se result dekho, sahi contact click_element karo.
3. Click ke baad ui_tree DOBARA (bina filter) → message likhne wala box dhoondo
   (Edit/Document, aksar "Type a message" / "Type a new message" / "Message").
4. MESSAGE BOX MEIN LIKHNA — chat apps (Teams/WhatsApp/Slack) WebView hote hain,
   inke compose box par set_text aksar KAAM NAHI karta. Isliye:
     a. Pehle us message box par click_element karo (focus ke liye).
     b. Phir type_in_window {{"window_title":"<asli title>", "text":"<message>"}}.
   (set_text sirf NATIVE fields ke liye — Notepad, dialogs, normal Edit boxes.)
5. Send: press_keys {{"keys":["enter"], "window_title":"<window ka asli title>"}}
   (ya "Send" button ho to click_element).
6. ui_tree se CONFIRM karo ke bheja gaya message, message-list mein nazar aa raha
   hai. Agar EK baar bhej kar verify fail ho, DOBARA-DOBARA mat bhejo (spam ho
   jayega) — honestly batao ke confirm nahi hua. "bhej diya" tabhi likho jab
   verify ho jaye.

INTENT: casual/typo app names samjho ("msteams"→Teams, "wts app"→WhatsApp).
After a user answer, continue from where you left off.

EXECUTION GUIDELINES:
1. ui_tree before interacting inside a window — read real element names, don't guess.
2. To put text in a field, ALWAYS prefer set_text (UIA — safe, no keystrokes, no
   focus stealing, not antivirus-flagged). Use type_in_window only if set_text
   fails (e.g. rich editors). NEVER type into the foreground blindly.
3. To click/activate a control, use click_element (UIA invoke — safe).
4. NEVER use powershell to send keystrokes / mouse input (SendKeys,
   System.Windows.Forms, Add-Type, SendInput) — it's blocked and antivirus
   flags it. powershell is ONLY for files/system (rename, move, list, create).
5. Websites: open_url; to interact with page content use ui_tree on the browser window.
6. screenshot_check is a last resort (games / empty ui_tree only).
7. After opening an app, wait 1-2s, then ui_tree.
8. At most {MAX_STEPS} steps — finish as soon as the goal is met.

VERIFY BEFORE CLAIMING SUCCESS (very important):
- For any send/post/type action (message, email, form), do NOT claim it worked
  unless you VERIFIED it — e.g. re-run ui_tree and see the sent text appear in
  the conversation/list. Tools returning ok=true only means the keystroke/value
  was issued, NOT that the app accepted it.
- If you could not verify, say so honestly: "type kar diya lekin confirm nahi
  kar saka — Boss zara dekh lein." NEVER write a confident "bhej diya" when you
  didn't actually confirm it. A false success is worse than an honest "not sure".

WHEN STUCK:
- If an action fails, try ONE alternative; if still stuck, either `ask` the user
  or finish with a clear, honest explanation — never a vague failure.
- Prefer dedicated actions (list_windows, find_files, open_app...) over powershell when one fits.

TONE: Replies and questions in Roman Urdu — short, professional, calm. Address the user as "Boss".
"""


# Friendly, user-facing label for each tool — powers the live progress line
# so a 30-second task feels like it's working, not frozen.
_TOOL_PROGRESS = {
    "list_windows": "Khuli windows dekh raha hun…",
    "focus_window": "Window saamne la raha hun…",
    "ui_tree": "Screen parh raha hun…",
    "click_element": "Click kar raha hun…",
    "set_text": "Likh raha hun…",
    "type_in_window": "Type kar raha hun…",
    "press_keys": "Bhej raha hun…",
    "open_app": "App khol raha hun…",
    "close_app": "App band kar raha hun…",
    "open_url": "Browser mein khol raha hun…",
    "powershell": "Command chala raha hun…",
    "find_files": "File dhoond raha hun…",
    "screenshot_check": "Screen ka jaiza le raha hun…",
    "wait": "Load hone ka intezar…",
}

# Single-user app → one in-flight engine run. The dashboard polls this to show
# what the engine is doing right now instead of a frozen "Soch raha hun…".
_PROGRESS: dict = {"active": False, "line": "", "step": 0, "ts": 0.0}


def get_progress() -> dict:
    # Auto-expire: if no step update in the last 12s the run is over/stalled,
    # so report inactive — no need to clear progress at every return point.
    p = dict(_PROGRESS)
    if p.get("active") and (time.monotonic() - p.get("ts", 0.0)) > 12:
        p["active"] = False
    return p


def _set_progress(active: bool, line: str = "", step: int = 0) -> None:
    _PROGRESS["active"] = active
    _PROGRESS["line"] = line
    _PROGRESS["step"] = step
    _PROGRESS["ts"] = time.monotonic()


class UniversalEngine:
    """Singleton agent-loop runner."""

    _instance: "UniversalEngine | None" = None

    @classmethod
    def get(cls) -> "UniversalEngine":
        if cls._instance is None:
            cls._instance = UniversalEngine()
        return cls._instance

    async def run(
        self,
        task: str,
        max_steps: int = MAX_STEPS,
        transcript: list[dict] | None = None,
    ) -> dict:
        """Run a task (or resume one) until done or a clarifying question.

        transcript: pass a prior session's transcript to RESUME after the user
            answered an `ask`. In that case `task` is the user's answer.

        Returns {ok, reply, steps} on completion, OR
        {ok: False, needs_input: True, question, transcript} when it asks.
        """
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

        if transcript:
            # Resuming after a clarifying question — the user's reply is the answer.
            transcript = list(transcript)
            transcript.append({"role": "user", "content": f"USER KA JAWAB: {task}"})
        else:
            transcript = [{"role": "user", "content": f"TASK: {task}"}]
        steps: list[dict] = []
        looked = False          # has ui_tree/list_windows been run yet?
        ask_rejections = 0      # how many premature asks we've pushed back on
        action_sig_counts: dict[str, int] = {}  # detect repeated send loops

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

            # Clarifying question — pause and hand control back to the user.
            if decision.get("ask"):
                question = str(decision["ask"]).strip()
                # GUARD: don't let the model ask things it can SEE. If it tries
                # to ask before ever inspecting the screen, push it to look
                # first (up to 2 times) instead of bothering the user.
                if not looked and ask_rejections < 2:
                    ask_rejections += 1
                    transcript.append({"role": "assistant", "content": json.dumps(decision, ensure_ascii=False)})
                    transcript.append({
                        "role": "user",
                        "content": (
                            "RUKO: user se mat poocho. Pehle KHUD dekho — relevant "
                            "window par ui_tree chalao (ya list_windows). Jo cheez "
                            "screen par hai (app khula hai?, contact ka naam, search "
                            "box) woh tum khud parh sakte ho. Ab ek tool action do."
                        ),
                    })
                    steps.append({"step": step_no, "tool": "ask(rejected)", "ok": False})
                    continue
                transcript.append({"role": "assistant", "content": json.dumps(decision, ensure_ascii=False)})
                log.info("engine_ask", question=question[:120], step=step_no)
                return {
                    "ok": False,
                    "needs_input": True,
                    "question": question,
                    "transcript": transcript,
                    "steps": steps,
                }

            tool_name = str(decision.get("tool") or "")
            args = decision.get("args") or {}
            _set_progress(True, _TOOL_PROGRESS.get(tool_name, "Kaam kar raha hun…"), step_no)
            if tool_name in ("ui_tree", "list_windows"):
                looked = True

            # LOOP GUARD: a repeated identical send/type action means verify
            # keeps failing and the model is re-sending — which spams the
            # recipient. After the 2nd repeat, stop and report honestly.
            if tool_name in ("set_text", "type_in_window", "press_keys"):
                sig = f"{tool_name}:{json.dumps(args, ensure_ascii=False, sort_keys=True)}"
                action_sig_counts[sig] = action_sig_counts.get(sig, 0) + 1
                if action_sig_counts[sig] >= 3:
                    log.warning("engine_send_loop_aborted", tool=tool_name, step=step_no)
                    return {
                        "ok": False,
                        "reply": (
                            "Boss, message box mein likhne ki kayi koshish ki lekin "
                            "confirm nahi kar paya ke message gaya — isliye baar-baar "
                            "bhejne se rok diya (spam na ho). Zara khud dekh lein, ya "
                            "dobara bolein to aur tareeqe se try karun."
                        ),
                        "steps": steps,
                    }

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

    # Ask shape — surfaces a question to the user (check before done/tool)
    ask = decision.get("ask") or decision.get("question") or decision.get("clarify")
    if isinstance(ask, str) and ask.strip() and not decision.get("done"):
        return {"ask": ask.strip()}

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
