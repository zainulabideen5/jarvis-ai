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

# A sent file shows up as an attachment in the chat with text like
# "...has an attachment". A typed filename does NOT — so verifying this string
# (and that its count INCREASED) is how we tell a real attachment from a model
# that merely typed the file's name.
_ATTACH_INDICATOR = "attachment"

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

INTENT SAAF NA HO TO POOCHO — RANDOM ACTION HARGIZ NAHI:
"Dekho mat poocho" rule SIRF screen-discoverable cheezon ke liye hai. Lekin agar
user ka ASAL MAQSAD hi clear na ho — ya request ambiguous ho — to {{"ask":"<chhota
sawal>"}} karo. Koi RANDOM / meaningless action (jaise bina maqsad khaali "New
folder" banana, ya kuch bhi guess karke kar dena) BILKUL MAT karo.
- Misaal: user kahe "folder nahi bana" → yeh COMPLAINT hai, literal "naya folder
  banao" command NAHI. Pehle samjho woh kis cheez ki baat kar rahe hain (pichla
  task?), zaroorat ho to poocho — andha-dhund folder mat bana do.
- User jab pichle kaam pe complain kare ("yeh nahi hua / galat hai / ajeeb"), usko
  naya literal command mat samjho — samajh ke clarify karo ya theek karo.

HONESTY — JHOOTH / FAKE SUCCESS MAT BOLO (sabse zaroori):
{{"done": true}} ke saath "✅ ho gaya" SIRF tab jab kaam SACH MEIN poora hua ho —
aur ho sake to VERIFY karo (file/folder/result actually bana? tool ne ok diya?).
Meaningless, adhoore, ya ghalat result ko KABHI "ho gaya" mat bolo. Agar pakka na
ho ke kaam hua, to honestly batao kya hua aur kya nahi. Fake success se user ka
bharosa toot-ta hai — honest failure usse behtar hai.

AVAILABLE ACTIONS:
{TOOLS_DOC}

PLAYBOOK — chat app (Teams/WhatsApp/Slack) mein message bhejna. EFFICIENT raho —
yeh 5-6 step mein ho jaana chahiye. Fazool steps mat lo. (Naam misaal hain;
asli naam jo user ne diya aur jo ui_tree mein dikhe wohi use karo.)

1. App ki window dhoondo: list_windows chalao. Agar app us list mein dikhe to
   uska EXACT title use karo. Agar list mein na bhi dikhe, GIVE UP MAT KARO —
   seedhe ui_tree {{"window_title":"<app ka naam, e.g. Teams>"}} try karo; window
   aksar khuli hoti hai chahe list mein na aaye. Sirf jab ui_tree bhi "window
   nahi mili" kahe TAB open_app + wait 2s. (User keh chuka app khula hai to
   uspe yaqeen karo — pehle ui_tree se dhoondo, user se mat poocho.)
2. ui_tree BINA filter (sirf window_title). Ek hi dafa — poori window dekho.
   NOTE: contact aksar [TreeItem]/[ListItem] "Chat <NAAM> Available"; message box
   aksar [Edit]/[Document] "Type a message"/"Type a new message"; search [ComboBox].
3. Contact KHOLNA — sabse reliable tareeqa SEARCH hai (har chat app mein chalta
   hai, contact-list click se behtar — kyunki list click aksar chat switch nahi
   karta):
   a. Search box dhoondo (jaise "Search" / "Search or start a new chat" — Edit
      ya ComboBox). type_in_window se usme contact ka naam likho (submit false).
   b. wait 1s, phir press_keys {{"keys":["enter"], "window_title":"<app>"}} —
      isse top result khul jaata hai.
   c. ui_tree se CONFIRM karo ke chat khul gayi (header pe us shaks ka naam
      aaye). Agar header pe purana/galat naam ho to chat switch NAHI hui —
      message mat bhejo, dobara search se kholo. (Galat banday ko bhejna sabse
      bari ghalti hai — pehle confirm karo sahi chat khuli hai.)
4. Message likho aur bhejo — chat apps (Teams/WhatsApp/Slack/Discord/koi bhi)
   ka box WebView hota hai jismein set_text kaam NAHI karta, isliye:
     type_in_window {{"window_title":"<app naam>", "element_name":"<box ka naam,
                      e.g. Type a message>", "text":"<msg>", "submit":true}}
   (submit:true type ke baad Enter dabata hai. window_title ke liye app ka SHORT
   naam do jaise "Teams"/"WhatsApp" — poora title mat do kyunki woh contact ke
   saath badalta rehta hai.)
5. EK verify: ui_tree {{"window_title":"<app>", "name_contains":"<message ke
   pehle 3-4 lafz>"}}. Message nazar aaye → FAURAN done "bhej diya". (Filter
   zaroori hai.) Na dikhe → DOBARA mat bhejo — honestly batao "confirm nahi hua".

WINDOW TITLE: hamesha app ka SHORT/STABLE naam use karo ("Teams", "WhatsApp",
"Notepad") — poora title (jaise "Chat | Ahmed | Microsoft Teams") contact badalne
par badal jaata hai aur agla step fail ho jaata hai.

INTENT: casual/typo app names samjho ("msteams"→Teams, "wts app"→WhatsApp).
After a user answer, continue from where you left off.

PLAYBOOK — chat app mein FILE / DOCUMENT / PDF / image bhejna:
1. File ka FULL path nikalo: agar sirf naam diya to find_files se poora path lo.
   (Task mein full path diya ho to wahi use karo.)
2. Contact kholo (Search box → naam → Enter → header confirm, message playbook
   ki tarah).
3. PASTE tareeqa PEHLE (sabse seedha, har app pe same):
   attach_file {{"window_title":"<app>", "element_name":"Type a message",
                 "file_path":"<poora path>"}}
   — yeh file clipboard se compose box mein paste kar deta hai (real click + Ctrl+V).
4. wait {{"seconds":2}} (upload hone do).
5. ui_tree {{"window_title":"<app>", "name_contains":"<file ka naam>"}} se dekho
   ke file compose/preview mein nazar aa rahi hai.
   - Agar nazar AAYE → press_keys {{"keys":["enter"], "window_title":"<app>"}} se bhejo.
   - Agar NA aaye (paste na chala) → FALLBACK: attach button (jaise Teams "Attach
     files") click_element → menu mein "Upload from this device" click_element →
     pick_file_in_dialog {{"file_path":"<path>"}} → wait → Send.
6. VERIFY: ui_tree {{"window_title":"<app>", "name_contains":"<file ka naam>"}} —
   file CHAT (message list) mein nazar aaye TABHI done "bhej diya". Na dikhe to
   honestly batao "confirm nahi kar saka".

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

    async def _run_tool(self, tool_name: str, args: dict) -> dict:
        """Execute one tool call safely (with a timeout so a hung UIA call on a
        WebView window can never freeze the whole task forever). Shared by the
        normal loop + recipe replay."""
        fn = TOOLS.get(tool_name)
        if fn is None:
            return {"ok": False, "error": f"unknown tool: {tool_name}. Valid: {', '.join(TOOLS)}"}
        call_args = _filter_args(fn, args)
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(lambda: fn(**call_args)), timeout=40
            )
        except asyncio.TimeoutError:
            return {"ok": False, "error": f"{tool_name} 40s mein poora nahi hua (hang) — skip"}
        except TypeError as e:
            return {"ok": False, "error": f"galat args: {e}"}
        except Exception as e:
            return {"ok": False, "error": str(e)[:300]}

    @staticmethod
    async def _bounded(fn, *args, timeout: float = 15, default=None):
        """Run a sync helper in a thread with a hard timeout — pre-count and
        verify scans were hanging unbounded on huge WebView trees, freezing
        the task at 'Type kar raha hun…' forever."""
        try:
            return await asyncio.wait_for(asyncio.to_thread(fn, *args), timeout=timeout)
        except asyncio.TimeoutError:
            log.warning("bounded_helper_timeout", fn=getattr(fn, "__name__", "?"))
            return default
        except Exception:
            return default

    async def _try_replay(self, task: str, intent: str, recipe: list[dict]) -> dict | None:
        """FAST PATH: adapt a saved recipe to this task in ONE planning call,
        then run the steps without per-step thinking. Returns a result dict on
        success, or None to signal 'fall back to normal step-by-step'."""
        # Safety: never replay an unsafe recipe (a send without a contact-click
        # would dump the message into whatever chat is open → wrong person).
        if not _is_sane_send_recipe(recipe):
            log.info("replay_skipped_unsafe_recipe", intent=intent)
            return None
        brain = get_brain()
        sys_prompt = (
            "Tum ek desktop-automation planner ho. Neeche ek SAVED RECIPE hai "
            "(steps jo pichli dafa is tarah ke kaam mein chale). Usme PLACEHOLDERS "
            "hain: <CONTACT> = jis shaks ko bhejna, <MESSAGE> = message text, "
            "<EMAIL> = email address, <FILE> = file ka poora path. NAYE TASK ke "
            "hisaab se in placeholders ko "
            "ASAL value se replace karke poora step-plan EK JSON ARRAY mein do — "
            "baqi structure aur UI labels (jaise 'Type a message') waise hi rakho. "
            "<CONTACT> ke liye sirf naam likho (system khud match kar lega). "
            "Sirf JSON array do, aur kuch nahi.\n"
            "Har step: {\"tool\":\"<name>\",\"args\":{...}}.\n\n"
            f"SAVED RECIPE:\n{json.dumps(recipe, ensure_ascii=False)}"
        )
        try:
            raw = await asyncio.to_thread(
                brain.think, sys_prompt, [{"role": "user", "content": f"NAYA TASK: {task}"}]
            )
        except BrainError:
            return None
        plan = _parse_step_array(raw)
        if not plan:
            return None

        # Safety: if the planner left any placeholder unfilled, don't run it
        # (would send literal "<MESSAGE>") — fall back to normal mode.
        if any(ph in json.dumps(plan, ensure_ascii=False)
               for ph in ("<CONTACT>", "<MESSAGE>", "<EMAIL>", "<FILE>")):
            log.info("replay_placeholder_unfilled_fallback", intent=intent)
            return None

        steps: list[dict] = []
        typed: dict | None = None
        for i, st in enumerate(plan[:12], 1):
            tool_name = str(st.get("tool") or "")
            args = st.get("args") or {}
            if tool_name not in TOOLS:
                continue
            _set_progress(True, _TOOL_PROGRESS.get(tool_name, "Kaam kar raha hun…"), i)
            # Verify target: message text, or — for files — a real "attachment"
            # indicator (a typed filename can't be told apart from an attachment
            # by the filename alone). Count BEFORE so we detect a NEW one.
            is_file_tool = tool_name in ("attach_file", "pick_file_in_dialog")
            verify_needle = None
            if tool_name in ("set_text", "type_in_window"):
                verify_needle = _basename_if_path(str(args.get("text") or ""))
            elif is_file_tool:
                verify_needle = _ATTACH_INDICATOR
            pre_count = None
            if verify_needle:
                pre_count = await self._bounded(
                    _count_text_in_window, str(args.get("window_title") or ""),
                    verify_needle, timeout=15, default=None)
            result = await self._run_tool(tool_name, args)
            steps.append({"step": i, "tool": tool_name, "args": args, "ok": bool(result.get("ok"))})
            if result.get("ok") and verify_needle:
                win = str(args.get("window_title") or "")
                scan = is_file_tool or (verify_needle != str(args.get("text") or ""))
                typed = {"window": "" if scan else win, "text": verify_needle,
                         "before": pre_count, "is_file": is_file_tool}
            # An action step failing means the recipe didn't fit → relearn.
            if not result.get("ok") and tool_name in (
                "open_app", "click_element", "set_text", "type_in_window",
                "pick_file_in_dialog", "press_keys"
            ):
                log.info("replay_step_failed_fallback", intent=intent, tool=tool_name)
                return None

        # Verify the send actually landed before claiming success.
        if typed:
            present = await self._bounded(
                _verify_present, typed["window"], typed["text"], typed.get("before"), typed.get("is_file", False),
                timeout=20, default=False)
            if not present:
                return None  # couldn't confirm via replay → let normal mode try
            log.info("engine_replay_done", intent=intent, steps=len(steps))
            return {
                "ok": True,
                "reply": f"⚡ {typed['text'][:50]} bhej diya (yaad tha, jaldi kiya).",
                "steps": steps,
                "replayed": True,
            }
        # No typing involved (e.g. open app) — trust completion if all ok.
        log.info("engine_replay_done", intent=intent, steps=len(steps))
        return {"ok": True, "reply": "⚡ Ho gaya (yaad tha, jaldi kiya).", "steps": steps, "replayed": True}

    async def _try_direct_plan(self, task: str, intent: str | None) -> dict | None:
        """FAST PATH for NEW tasks (no recipe yet): ask the brain ONCE for the
        full step plan (using the playbook), then execute without per-step
        thinking. One ~10s CLI call instead of 6-8. Falls back to the
        interactive loop (returns None) if planning/execution doesn't pan out."""
        brain = get_brain()
        sys_prompt = (
            SYSTEM_PROMPT
            + "\n\nMODE: DIRECT PLAN. Is task ka POORA step-plan EK JSON ARRAY "
            "mein do — koi prose nahi, sirf array. Har step: "
            '{"tool":"<name>","args":{...}}. Chat-app send ke liye playbook ke '
            "mutabiq: type_in_window(Search box, naam) → press_keys enter → "
            "type_in_window(message box, text, submit:true). ui_tree/ask steps "
            "MAT do (yeh plan blind chalega) — sirf action steps."
        )
        try:
            raw = await asyncio.to_thread(
                brain.think, sys_prompt, [{"role": "user", "content": f"TASK: {task}"}]
            )
        except BrainError:
            return None
        plan = _parse_step_array(raw)
        if not plan or len(plan) > 10:
            return None

        steps: list[dict] = []
        typed: dict | None = None
        for i, st in enumerate(plan, 1):
            tool_name = str(st.get("tool") or "")
            args = st.get("args") or {}
            if tool_name not in TOOLS or tool_name in ("screenshot_check",):
                continue
            _set_progress(True, _TOOL_PROGRESS.get(tool_name, "Kaam kar raha hun…"), i)
            pre_count = None
            if tool_name in ("set_text", "type_in_window", "attach_file", "pick_file_in_dialog"):
                _vtxt = _basename_if_path(str(args.get("file_path") or args.get("text") or ""))
                if _vtxt:
                    pre_count = await self._bounded(
                        _count_text_in_window, str(args.get("window_title") or ""), _vtxt,
                        timeout=15, default=None)
            result = await self._run_tool(tool_name, args)
            steps.append({"step": i, "tool": tool_name, "args": args, "ok": bool(result.get("ok"))})
            if not result.get("ok"):
                log.info("direct_plan_step_failed_fallback", tool=tool_name)
                return None  # plan didn't fit reality — interactive loop will handle
            if tool_name in ("set_text", "type_in_window", "attach_file", "pick_file_in_dialog"):
                txt = str(args.get("text") or args.get("file_path") or "")
                # the LAST send-ish step is what we verify (message, not search)
                art = _basename_if_path(txt)
                if art and not ("search" in str(args.get("element_name", "")).lower()):
                    typed = {"window": str(args.get("window_title") or ""), "text": art,
                             "before": pre_count,
                             "is_file": tool_name in ("attach_file", "pick_file_in_dialog")}

        if typed:
            present = await self._bounded(
                _verify_present, typed["window"], typed["text"], typed.get("before"), typed.get("is_file", False),
                timeout=20, default=False)
            if not present:
                return None  # not confirmed — let the interactive loop retry properly
            _maybe_save_recipe(intent, steps)
            log.info("engine_direct_plan_done", steps=len(steps))
            return {"ok": True, "reply": f"✅ {typed['text'][:50]} bhej diya (confirm ho gaya).", "steps": steps}
        return None  # nothing verifiable — use interactive loop

    async def run(
        self,
        task: str,
        max_steps: int = MAX_STEPS,
        transcript: list[dict] | None = None,
        origin: str | None = None,
    ) -> dict:
        """Run a task, then tidy up: minimize whatever app it used and return
        the user to where they were (e.g. the dashboard). Wrapper around the
        actual loop so cleanup runs no matter which path returns.

        origin: the user's window (dashboard) to return to. Pass it in when the
        caller already changed the foreground (e.g. a deterministic path focused
        an app first) — otherwise we'd capture the WRONG window here and end up
        minimizing the dashboard instead of the app."""
        # Fresh task → clear any stale STOP flag from a previous run.
        if not transcript:
            from app.services.task_control import clear_stop
            clear_stop()
        # Remember where the user was BEFORE we start grabbing windows — unless
        # the caller already told us (more reliable).
        if origin is None and not transcript:
            try:
                origin = await asyncio.to_thread(
                    LaptopNative_active_title
                )
            except Exception:
                origin = None

        result = await self._run_impl(task, max_steps, transcript)

        # Auto-minimize the apps we touched + refocus the origin — UNLESS we're
        # pausing to ask the user something (then keep things as-is).
        if not result.get("needs_input"):
            try:
                await asyncio.to_thread(_post_task_cleanup, result.get("steps", []), origin)
            except Exception as e:
                log.debug("post_task_cleanup_failed", error=str(e))
        return result

    async def _run_impl(
        self,
        task: str,
        max_steps: int = MAX_STEPS,
        transcript: list[dict] | None = None,
    ) -> dict:
        """The actual think→act→observe loop (and recipe replay)."""
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

        # FAST PATH: if we've done this kind of task before, try replaying the
        # saved recipe (1 planning call instead of ~6 think-act rounds).
        intent = None
        if not transcript:
            from app.services.universal_engine.recipes import RecipeStore, intent_key
            intent = intent_key(task)
            # NEVER replay a FILE send: the generic "attachment" verify is
            # unreliable (it matches stray 'attachment' text in the tree → false
            # "bhej diya" even when the file never attached). File sends always
            # run the real flow with real verification. (intent kept for learning.)
            if intent and not intent.startswith("sendfile:"):
                recipe = RecipeStore.get(intent)
                if recipe:
                    _set_progress(True, "Yaad kiya hua tareeqa chala raha hun…", 0)
                    replayed = await self._try_replay(task, intent, recipe)
                    if replayed is not None:
                        return replayed
                    log.info("replay_fell_back_to_normal", intent=intent)
                # NOTE: direct-plan (blind one-shot) is intentionally NOT used for
                # sends — it can't confirm the right chat opened, risking a
                # wrong-recipient message. New sends go through the interactive
                # loop (opens contact via search, CONFIRMS the header, then
                # sends + count-verifies) — slower on CLI but RELIABLE — and the
                # learned recipe makes every repeat fast.

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
        typed: dict | None = None   # last text typed + its window (for verify)

        for step_no in range(1, max_steps + 1):
            # STOP check — user can abort a running task ("ruk ja"/stop).
            from app.services.task_control import is_stopped
            if is_stopped():
                log.info("engine_stopped_by_user", step=step_no)
                _set_progress(False, "", step_no)
                return {"ok": False, "reply": "🛑 Boss, rok diya — aapne stop bola.", "steps": steps}
            raw = None
            last_err: BrainError | None = None
            # Show "thinking" so the UI doesn't look frozen during the (slow,
            # for CLI) brain call — otherwise the previous step's line lingers.
            _set_progress(True, "Soch raha hun…", step_no)
            # The CLI's safety classifier is flaky on borderline automation
            # transcripts — a straight retry usually passes. 2 attempts (each
            # capped at THINK_TIMEOUT_SEC) so a hang doesn't freeze too long.
            for attempt in range(2):
                try:
                    raw = await asyncio.to_thread(brain.think, SYSTEM_PROMPT, transcript)
                    break
                except BrainError as e:
                    last_err = e
                    log.warning("engine_brain_error", attempt=attempt + 1, error=str(e)[:200])
                    await asyncio.sleep(1.0)
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
                # ANTI-LIE GUARD: if the model claims it sent/typed something,
                # verify the text actually shows up in the window before we
                # report success. Catches the "bhej diya" lie when typing
                # silently failed (e.g. focus didn't land on a WebView box).
                if typed and _claims_success(reply):
                    present = await self._bounded(
                        _verify_present, typed["window"], typed["text"], typed.get("before"), typed.get("is_file", False),
                        timeout=20, default=False)
                    if not present:
                        log.warning("engine_unverified_send", window=typed["window"])
                        return {
                            "ok": False,
                            "reply": (
                                f"⚠️ Boss, \"{typed['text'][:50]}\" type to kiya lekin "
                                "confirm NAHI kar paya ke woh actually chat/box mein "
                                "gaya — ho sakta hai na gaya ho. Zara khud dekh lein. "
                                "(Main jhoot nahi bolunga ke ho gaya jab pakka na ho.)"
                            ),
                            "steps": steps,
                        }
                    # RIGHT-RECIPIENT GUARD: the message sent — but did it go to
                    # the intended person? The open chat window's title carries
                    # the contact name. If it doesn't match the recipient from
                    # the task, it likely went to the WRONG chat.
                    recipient = _extract_recipient(task)
                    if not recipient and transcript:
                        # RESUME path: `task` is the user's clarifying answer, not
                        # the original command — pull the recipient from the
                        # original command in the transcript so the guard still runs.
                        # SIRF user messages scan karo — assistant ke JSON/playbook
                        # se galat recipient na nikle.
                        for _m in transcript:
                            if _m.get("role") != "user":
                                continue
                            _r = _extract_recipient(str(_m.get("content", "")))
                            if _r:
                                recipient = _r
                                break
                    if recipient and not await self._bounded(
                        _active_chat_is, recipient, timeout=10, default=True):
                        log.warning("engine_wrong_recipient", recipient=recipient)
                        return {
                            "ok": False,
                            "reply": (
                                f"⚠️ Boss, message bhej to diya lekin lagta hai woh "
                                f"'{recipient}' ke chat mein NAHI gaya (galat chat khuli "
                                "thi). Zara khud dekh lein — main pakka galat banday ko "
                                "bhejne se rok raha hoon."
                            ),
                            "steps": steps,
                        }
                log.info("engine_done", task=task[:80], steps=len(steps))
                # LEARN: save the working step sequence as a recipe so next
                # time this kind of task replays fast.
                _maybe_save_recipe(intent, steps)
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

            # BEFORE a send, count how many times this text already appears in
            # the chat (old identical msgs like "hello"). After sending we need
            # to see a NEW occurrence — otherwise verify falsely matches an old one.
            # What to verify after this step. For a TEXT message: the message
            # text. For a FILE: an actual "attachment" indicator (NOT the
            # filename — a typed filename also shows the name, so the filename
            # can't tell a real attachment from text that just mentions it).
            is_file_tool = tool_name in ("attach_file", "pick_file_in_dialog")
            verify_needle = None
            if tool_name in ("set_text", "type_in_window"):
                _t = str(args.get("text") or "")
                verify_needle = _basename_if_path(_t)
            elif is_file_tool:
                verify_needle = _ATTACH_INDICATOR   # "has an attachment"
            pre_count = None
            if verify_needle:
                pre_count = await self._bounded(
                    _count_text_in_window, str(args.get("window_title") or ""),
                    verify_needle, timeout=15, default=None)

            result = await self._run_tool(tool_name, args)

            # Track what to verify later.
            if result.get("ok") and verify_needle:
                win = str(args.get("window_title") or "")
                # path-typed-into-dialog or file tool → scan all chat windows
                scan = is_file_tool or (verify_needle != str(args.get("text") or ""))
                typed = {"window": "" if scan else win, "text": verify_needle,
                         "before": pre_count, "is_file": is_file_tool}

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

        # Ran out of steps. But if we typed a message and it's actually present
        # in the window, the send DID succeed — don't report a false failure.
        if typed:
            present = await self._bounded(
                _verify_present, typed["window"], typed["text"], typed.get("before"), typed.get("is_file", False),
                timeout=20, default=False)
            if present:
                return {
                    "ok": True,
                    "reply": f"✅ Boss, \"{typed['text'][:50]}\" bhej diya (chat mein confirm ho gaya).",
                    "steps": steps,
                }
        return {
            "ok": False,
            "reply": (
                "Boss, poori tarah confirm nahi kar paya — task thoda lamba tha. "
                "Zara dekh lein, ya dobara bolein."
            ),
            "steps": steps,
        }


def LaptopNative_active_title() -> str:
    from app.services.laptop_control.laptop_native import LaptopNative
    return LaptopNative.get().active_window_title()


def _post_task_cleanup(steps: list[dict], origin: str | None) -> None:
    """After a task: minimize the app windows the engine used, then bring the
    user back to where they were (e.g. the JARVIS dashboard). Generic — works
    for any app, not just Teams."""
    import time as _t
    from app.services.laptop_control.laptop_native import LaptopNative
    nat = LaptopNative.get()
    origin_low = (origin or "").lower().strip()

    # FAST cleanup: the app just used IS the current foreground window — minimize
    # THAT (win32, instant) + the task's step windows. We deliberately DROP the
    # old UIA list_open_windows() scan here — it enumerated the whole desktop and
    # was the main reason minimize felt "bohot late". (All-rounder: current
    # foreground covers any app — chat/calc/browser/editor.)
    targets: list[str] = []
    try:
        cur = nat.active_window_title() or ""
        if cur:
            targets.append(cur)
    except Exception:
        pass
    for s in steps:
        w = (s.get("args") or {}).get("window_title")
        if w and w.lower() not in [x.lower() for x in targets]:
            targets.append(w)

    _t.sleep(0.1)  # let the send settle a moment before hiding the window
    for w in targets:
        if origin_low and (w.lower() in origin_low or origin_low in w.lower()):
            continue  # never minimize the user's own window (dashboard)
        try:
            nat.minimize_window(w)   # win32 SW_MINIMIZE fallback = fast on UWP
        except Exception:
            pass
    # Return focus to where the user was (dashboard)
    if origin_low:
        try:
            nat.focus_window(origin)
        except Exception:
            pass


def _parse_step_array(raw: str) -> list[dict] | None:
    """Extract a JSON array of {tool,args} steps from the replay planner output."""
    s = (raw or "").strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:]
        s = s.strip()
    start = s.find("[")
    if start == -1:
        return None
    try:
        arr = json.JSONDecoder().raw_decode(s[start:])[0]
    except json.JSONDecodeError:
        return None
    if not isinstance(arr, list):
        return None
    out = []
    for item in arr:
        if isinstance(item, dict) and (item.get("tool") or item.get("action")):
            out.append({"tool": item.get("tool") or item.get("action"),
                        "args": item.get("args") if isinstance(item.get("args"), dict) else {}})
    return out or None


def _scrub_recipe_step(tool: str, args: dict) -> dict:
    """Replace person-specific / message values with PLACEHOLDERS before saving
    a recipe — so we NEVER bake a specific name/email/message into memory. The
    recipe stays a generic template; replay fills <CONTACT>/<MESSAGE>/<EMAIL>
    from the new task. (Honors the no-hardcoded-names rule.)"""
    out = dict(args or {})
    if tool in ("type_in_window", "set_text") and "text" in out:
        # Typing into a SEARCH box = the contact name (<CONTACT>); any other
        # box = the message (<MESSAGE>). Distinguishing these is what lets a
        # search-based recipe replay correctly for a new name.
        el = str(out.get("element_name", "")).lower()
        out["text"] = "<CONTACT>" if "search" in el else "<MESSAGE>"
    if tool == "click_element" and str(out.get("control_type", "")) in ("TreeItem", "ListItem", "DataItem", "Text"):
        out["element_name"] = "<CONTACT>"     # the person clicked in a list
    if tool == "pick_file_in_dialog" and "file_path" in out:
        out["file_path"] = "<FILE>"
    if tool == "attach_file" and "file_path" in out:
        out["file_path"] = "<FILE>"
    if tool == "send_email":
        for k in ("to", "recipient"):
            if k in out:
                out[k] = "<EMAIL>"
        if "body" in out:
            out["body"] = "<MESSAGE>"
    return out


def _is_sane_send_recipe(clean: list[dict]) -> bool:
    """A messaging/file recipe is only safe if it NAVIGATES to a recipient
    before sending — otherwise replay dumps the message into whatever chat is
    open (wrong person). Navigation = either click_element (clicked the contact
    in the list) OR press_keys (opened the searched contact via Enter). Generic
    — no app/name specifics. The bad recipe that caused the wrong-send had
    neither (it just typed)."""
    msg_tools = ("type_in_window", "set_text", "pick_file_in_dialog", "attach_file")
    has_msg = any(s["tool"] in msg_tools for s in clean)
    if not has_msg:
        return True  # not a send recipe (e.g. open app) — fine
    # Some recipient navigation must be present.
    return any(s["tool"] in ("click_element", "press_keys") for s in clean)


def _dedupe_consecutive_sends(clean: list[dict]) -> list[dict]:
    """Drop a repeated identical send right after itself (messy runs sometimes
    type the same message twice)."""
    out: list[dict] = []
    for s in clean:
        if (out and s["tool"] in ("type_in_window", "set_text")
                and out[-1].get("tool") == s["tool"]
                and out[-1].get("args") == s.get("args")):
            continue
        out.append(s)
    return out


def _maybe_save_recipe(intent, steps: list[dict]) -> None:
    """Save the ok automation steps of a successful run as a reusable recipe.
    Values are scrubbed to placeholders — no real names/emails/messages stored.
    Only saved if the sequence is SANE (selects a recipient before sending)."""
    clean = [
        {"tool": s["tool"], "args": _scrub_recipe_step(s["tool"], s.get("args", {}))}
        for s in steps
        if s.get("ok") and s.get("tool") in TOOLS
        and s.get("tool") not in ("wait", "list_windows", "ui_tree")
    ]
    clean = _dedupe_consecutive_sends(clean)
    if len(clean) < 2:  # not a real multi-step flow
        return
    if not _is_sane_send_recipe(clean):
        log.info("recipe_rejected_unsafe", reason="no contact-click before send")
        return
    from app.services.universal_engine.recipes import RecipeStore, intent_from_steps
    # Prefer the task-derived intent; if the task text was too typo'd to parse,
    # fall back to deriving intent from what actually happened.
    key = intent or intent_from_steps(steps)
    if key:
        RecipeStore.save(key, clean)


_SUCCESS_WORDS = ("bhej diya", "bhej di", "send kar diya", "send kr diya", "ho gaya",
                  "kar diya", "sent", "type kar diya", "likh diya", "daal diya")


def _claims_success(reply: str) -> bool:
    low = (reply or "").lower()
    # If it already hedges ("confirm nahi", "shayad", "nahi kar"), don't double-flag.
    if any(h in low for h in ("confirm nahi", "nahi kar paya", "nahi kar saka", "shayad", "pakka nahi")):
        return False
    return any(w in low for w in _SUCCESS_WORDS)


def _basename_if_path(s: str) -> str:
    """If the string is a file path, return just the filename; else unchanged."""
    s = (s or "").strip().strip('"')
    if "\\" in s or "/" in s:
        seg = s.replace("/", "\\").rstrip("\\").split("\\")[-1]
        return seg or s
    return s


_RECIPIENT_STOP = {
    "teams", "team", "whatsapp", "whats", "app", "slack", "discord", "telegram",
    "pe", "pa", "par", "pr", "mein", "mei", "me", "ma", "mai", "may", "mn", "men",
    "ko", "se", "ka", "ki", "kar", "karo", "kr",
    "send", "bhej", "bhejo", "bhaj", "message", "msg", "yeh", "ye", "ek",
}


def _extract_recipient(task: str) -> str:
    """Pull the likely recipient name from a send task. 'teams pe zaid ko hello
    bhej' -> 'zaid'. Best-effort — used only to GUARD against wrong-chat sends."""
    low = (task or "").lower()
    for sep in (" ko ", " to "):
        if sep in low:
            before = low.split(sep)[0]
            words = [w for w in before.replace(",", " ").split() if w not in _RECIPIENT_STOP and len(w) > 1]
            if words:
                return " ".join(words[-2:])  # last 1-2 words = the name
    return ""


def _active_chat_is(recipient: str) -> bool:
    """True if the intended recipient's chat looks open (their name is in the
    active chat-app window's title, e.g. 'Chat | Zaid Moeen | Microsoft Teams').
    Conservative: returns True when unsure so it never false-blocks a good send."""
    rec = (recipient or "").strip().lower()
    if not rec:
        return True
    first = rec.split()[0]  # match on first name too (zaid ~ Zaid Moeen)
    try:
        from app.services.laptop_control.laptop_native import LaptopNative
        from app.services.universal_engine.recipes import _detect_app
        nat = LaptopNative.get()
        act = (nat.active_window_title() or "").lower()
        if _detect_app(act):                 # active window is a chat app
            return rec in act or first in act
        for w in nat.list_open_windows().get("windows", []):
            t = (w.get("title", "") or "").lower()
            if _detect_app(t) and (rec in t or first in t):
                return True
        return False
    except Exception:
        return True


def _count_text_in_window(window_title: str, text: str) -> int:
    """How many times `text` appears across the chat-app window(s). Used to
    detect a NEWLY sent message vs old identical ones."""
    needle = (text or "").strip()[:40].lower()
    if not needle:
        return 0
    total = 0
    try:
        from app.services.universal_engine import tools
        from app.services.universal_engine.recipes import _detect_app
        windows = []
        if window_title:
            windows.append(window_title)
        for w in tools.list_windows().get("windows", []):
            t = w.get("title", "")
            if _detect_app(t) and t not in windows:
                windows.append(t)
        for wt in windows:
            tree = tools.ui_tree(wt, name_contains=needle)
            hay = (tree.get("elements", "") or "").lower()
            total = max(total, hay.count(needle))  # max across windows (avoid double-count)
    except Exception:
        pass
    return total


def _verify_present(window_title: str, text: str, before: int | None = None,
                    is_file: bool = False) -> bool:
    """Confirm a send actually happened.

    For a TEXT message the MOST reliable signal is the COMPOSE BOX: after Enter,
    a sent message leaves the box empty. If the box no longer holds the text →
    it sent (works even for common words like 'hello' where counting old copies
    is unreliable). If the box STILL holds the text → not sent.
    For files / when the box can't be read, fall back to count-increase, then
    plain presence."""
    if not is_file and window_title:
        try:
            from app.services.laptop_control.laptop_native import LaptopNative
            box = LaptopNative.get().get_element_value(window_title)
            if box.get("ok"):
                needle = text.strip()[:30].lower()
                box_val = (box.get("value") or "").lower()
                # box cleared (message gone) → sent; still there → not sent
                return bool(needle) and needle not in box_val
        except Exception:
            pass
    # Count-increase (good for files / unique text)
    if before is not None:
        return _count_text_in_window(window_title, text) > before
    # Last resort — plain presence anywhere in a chat-app window
    if window_title and _text_present_in_window(window_title, text):
        return True
    try:
        from app.services.universal_engine import tools
        from app.services.universal_engine.recipes import _detect_app
        for w in tools.list_windows().get("windows", []):
            t = w.get("title", "")
            if _detect_app(t) and _text_present_in_window(t, text):
                return True
    except Exception:
        pass
    return False


def _text_present_in_window(window_title: str, text: str) -> bool:
    """Check the typed text actually appears in the window after sending.

    Uses a name_contains FILTER so it scans ALL elements (a plain ui_tree caps
    at ~120, and a chat app's just-sent message sits past that cap → false
    negative). The filter matches across the whole tree.
    """
    needle = text.strip()[:40]
    if not needle:
        return False
    try:
        from app.services.universal_engine import tools
        # Filtered scan — finds the message even in a long conversation.
        tree = tools.ui_tree(window_title, name_contains=needle)
        if needle.lower() in (tree.get("elements", "") or "").lower():
            return True
        # Fallback: full scan (covers short lists / native fields)
        tree2 = tools.ui_tree(window_title)
        return needle.lower() in (tree2.get("elements", "") or "").lower()
    except Exception:
        return False


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
