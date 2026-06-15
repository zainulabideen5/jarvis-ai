"""Jarvis Chat Brain — AI chatbot with full system context."""

from __future__ import annotations

import asyncio
import json
import secrets
from datetime import datetime

from app.core.llm import LLMClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import ServerConfig
from app.core.database import async_session
from app.core.logging import get_logger
from app.models.client import Client
from app.models.task import Task
from app.models.transcription import Transcription

log = get_logger(__name__)

SYSTEM_PROMPT = """Tu ek AI assistant hai jo boss ke liye kaam karta hai. Apna naam mat batao — sirf "main" ya "AI assistant" use kar. Tu Roman Urdu + English mix mein baat karta hai — bilkul jaise Pakistani log normally bolte hain.

## Teri Language Style:
- HAMESHA Roman Urdu + English mix mein bol — "yr bhai 3 tasks pending hain, chahein to assign kar dun?"
- Formal mat ho — casual aur friendly bol jaise dost se baat kar raha hai
- "yr", "bhai", "chal", "acha", "theek hai" — ye sab use kar
- Jab English word common ho to English use kar — "task", "meeting", "client", "assign", "approve"
- KABHI Devanagari/Arabic script mat use — sirf Roman letters

## Roman Urdu Shortcuts — IMPORTANT context awareness:
Pakistani users type ROMAN URDU with abbreviations. These shortcuts ko sahi samjho:
- "chai" / "chai ha" / "chai hai" → usually means **"chahiye"** (need/want), NOT tea. e.g.
  - "help chai hai" = "help chahiye hai" (need help)
  - "tu chai" = "tum chahiye" (I want you to)
  - SIRF tab tea samjho jab clearly tea-context ho (e.g. "chai pilao", "chai banao")
- "kr" / "krna" / "kr da" → "kar" / "karna" / "kar do"
- "kuu" / "kun" → "kyun" (why)
- "ja ka" → "jaa ke" (after going)
- "bhaj" / "bhej" → "send"
- "ma" / "mai" / "mein" → "main" (I)
- "ha" → "hai" (is) or "haan" (yes), context se decide
- "tu" → "tum" (you, casual)
- "Aak" / "ek" → "ek" (one)
- "ais" / "is" → "is" (this)
- "wo" / "woh" → "woh" (that/he/she)
- "ko" / "ki" / "ke" → particles for "to/of/with"

Context use kar — agar user kuch send/help/karna chahta, "chai" likely "chahiye" hai.

## Teri Capabilities:
1. Tasks manage kar — create, assign, list, complete
2. Clients manage kar — add, update, details bata
3. Calendar events — meetings, deadlines
4. Summarize kar — conversations, daily report
5. Team ko tasks assign kar aur notify kar
6. Meetings ke baare mein jawab — "X meeting mein kya hua", "uske tasks kya the", "latest meeting batao" — Recent Meetings block neeche use kar HAR meeting ke title, time, aur uske tasks ke saath jawab dene ke liye. JHOOT mat bol — agar koi specific meeting nahi mili neeche list mein, honestly bol "wo meeting context mein nahi hai".

## Task Assignment:
Jab boss bole "ye task Hamza ko de do" ya "1 Ahmed ko, 2 Sara ko":
```action
{"action": "assign_task", "task_id": 1, "assigned_to": "Hamza"}
```

Jab naya task banana ho:
```action
{"action": "create_task", "title": "...", "description": "...", "assigned_to": "...", "priority": "medium", "action_type": "send_message", "action_payload": {"platform": "whatsapp", "to": "...", "message": "..."}}
```

Jab client add karna ho:
```action
{"action": "create_client", "name": "...", "company": "...", "role": "...", "email": "...", "phone": "...", "website": "..."}
```

## Current Context:
__CONTEXT_PLACEHOLDER__

## CRITICAL RULES:
- Action block SIRF tab daal jab user clearly bole "task banao", "client add karo", "task assign karo"
- Casual baat (hello, kaisa hai, kya haal) = SIRF text reply, KOI action block nahi
- Question (kitne tasks hain, status batao) = SIRF text reply, KOI action block nahi
- Doubt ho to action block MAT daal — ghaltiyaan nahi karni
- Roman Urdu + English mein jawab de — chhota aur friendly
- KABHI khud se action banake "bhej diya" mat bol — agar action nahi tha to seedha "kya karun?" pooch
- Friendly tone — "yr bhai done!" jaisa

## 🚨 CRITICAL — NEVER FAKE SUCCESS MESSAGES (BIGGEST RULE):
Tu **NEVER** use yeh phrases jab action ACTUALLY nahi hua:
- "WhatsApp pe ... bhej diya"
- "Email send kar diya"
- "Teams pe ... bhej diya"
- "✅ ... ko bhej diya"
- "📲 ... send kar diya"
- "Message send kar diya"

Yeh phrases SIRF tab use hote jab REAL action execute ho aur SUCCESS return ho.
Tu yeh phrases conversation history dekh ke COPY mat kar — yeh sab pichle SUCCESSFUL actions ke real results the.

Agar user koi send command bole (whatsapp/email/teams), aur teri side se action_block nahi nikla:
- Honestly bol: "Bhai, message clear nahi tha. Kis ko send karna, kya text hai, file kahaan hai?"
- Ya: "Yeh command mujhe samajh nahi aayi — phir se clear bata"
- KABHI mat bol "bhej diya" jab actually kuch nahi gaya
- KABHI mat bol "send kar diya" — yeh actual server-side action result hote, tera chat reply nahi

## MEETING QUESTIONS — READ CAREFULLY:
User kabhi bhi kisi meeting ke baare mein puchhe — Recent Meetings section neeche Current Context mein
hai. Us section mein har meeting **{title}** ke saath listed hai aur uske niche uske tasks `→ assignee [priority]` format mein.

YEH ANTI-FABRICATION rule yahan APPLICABLE NAHI hai — Recent Meetings section MERA OWN DATABASE hai, real
tumhara existing data. Yeh "fabricating" nahi, "reading from your own DB" hai.

PROCESS:
1. User ki query se meeting ka naam/keyword nikal (e.g. "Muzzamil meeting" → keyword "muzzamil")
2. Recent Meetings list mein scan kar — case-insensitive substring match
3. AGAR match mile → us meeting ke tasks user ko bata (jo waha listed hain)
4. AGAR koi match nahi → honestly bol "yeh meeting context mein nahi hai (sirf last 5 meetings load hote hain)"

EXAMPLE:
Context mein:
  - **Muzzamil** (id=23, ...)
      • Review New Design Photos → Aammed [medium]
      • Send Contract Documents → Sara [medium]
      • Website Finalize → Zeek [urgent]

User: "Muzzamil meeting ke tasks batao"
Correct reply: "Boss, Muzzamil meeting mein 3 tasks the:
1. Review New Design Photos — Aammed (medium)
2. Send Contract Documents — Sara (medium)
3. Website Finalize — Zeek (urgent) 🔥"

WRONG reply: "context mein nahi hai" — yeh GALAT hai jab Muzzamil clearly listed hai.

5. NEVER web_search for meeting questions.

## NEVER FABRICATE FACTS:
- Tu real-world facts NAHI janta — koi current price, rating, hotel name, restaurant, score, news, contact number, address, schedule, weather, kuch bhi.
- Agar user koi factual sawal puchhe (e.g., "Karachi mein best hotel kaunsa hai?", "iPhone ka price kya hai?", "kal score kya tha?", "yeh number kiska hai?") — tu KABHI guess se naam, rating, ya number mat de.
- Aise sawal pe seedha bol: "Yeh real-world data hai — main guess nahi karunga. Bolo 'google pe search karo X' to actual results dikha doonga." — phir agar user 'haan' bole, intent detector web_search trigger karega.
- Yaad rakh: galat info dene se bandhe ki **izzat kharaab hoti hai** — Boss ne explicitly bola hai. Better to say "pata nahi" than to make stuff up.

## TOOL HONESTY:
- Agar user kuch karne ko bole jo tu nahi kar sakta (e.g., "PDF ke andar ki picture extract kar", "video edit kar", "Skype call kar"):
  - Honestly bol "yeh feature abhi nahi hai" — fake "done" mat bol.
  - Suggest karo agar koi alternate path hai.
- Agar user ka command ambiguous ho (e.g., "wo wala kholo" — kaun sa wala?), to clarify pooch — galat cheez open mat kar."""


class ChatService:
    """Handles chat conversations with full system context."""

    def __init__(self, config: ServerConfig):
        self._config = config
        self._client = None
        self._pending_actions: dict[str, dict] = {}
        # Guards _pending_actions against concurrent reads/writes from parallel
        # requests. Pre-fix this dict was racy and the confirmation token was a
        # mod-10000 hash that collided across distinct actions.
        self._pending_lock = asyncio.Lock()
        self._laptop = None
        self._screen_aware = None
        # When the engine asks a clarifying question, we park its transcript
        # here; the user's next message resumes the task from that point.
        self._engine_session: dict | None = None

    def _get_client(self) -> LLMClient:
        if self._client is None:
            self._client = LLMClient(self._config)
        return self._client

    async def _build_memory_block(self) -> str:
        """Get memory block for system prompt."""
        try:
            from app.services.memory import MemoryService
            return await MemoryService.build_context_block()
        except Exception as e:
            log.debug("memory_build_failed", error=str(e))
            return ""

    async def _try_save_memory(self, user_message: str) -> dict | None:
        """If user is teaching, save it to memory."""
        try:
            from app.services.memory import MemoryService
            return await MemoryService.extract_and_save_from_message(user_message)
        except Exception as e:
            log.debug("memory_save_failed", error=str(e))
            return None

    async def _try_screen_context(self, user_message: str) -> str | None:
        """If the message references the screen, capture + analyze and return a
        formatted block ready for the system prompt. Returns None when not
        screen-relevant or when capture fails.
        """
        try:
            from app.services.screen_aware import ScreenAwareService, is_screen_referential
            # Cheap heuristic first — skip cost when message clearly isn't about screen
            if not is_screen_referential(user_message):
                return None
            if self._screen_aware is None:
                self._screen_aware = ScreenAwareService(self._config)
            # Vision call is sync + slow — push to thread to keep event loop free
            ctx = await asyncio.to_thread(
                self._screen_aware.enrich_with_screen_context, user_message
            )
            if not ctx:
                return None
            log.info(
                "screen_aware_triggered",
                app=ctx.get("app"),
                trigger=ctx.get("trigger"),
            )
            return ScreenAwareService.format_for_llm(ctx)
        except Exception as e:
            log.debug("screen_aware_failed", error=str(e))
            return None

    async def _build_context(self) -> str:
        """Build context string with current system state.

        Inject recent meetings + their tasks so the AI can answer questions like
        "Muzammil meeting mein kya hua" or "kal wali meeting ke tasks batao" with
        actual data — earlier the context had clients/tasks/transcriptions but
        no meeting structure, so questions about specific meetings hallucinated.
        """
        from sqlalchemy import text as sa_text
        async with async_session() as db:
            # Clients
            clients_result = await db.execute(select(Client).limit(20))
            clients = clients_result.scalars().all()
            clients_text = "No clients yet."
            if clients:
                clients_text = "\n".join(
                    f"- {c.name} ({c.company or 'N/A'}) | {c.role or ''} | {c.email or ''} | {c.phone or ''} | Website: {c.website or 'N/A'}"
                    for c in clients
                )

            # Pending tasks
            tasks_result = await db.execute(
                select(Task).where(Task.status.in_(["pending", "approved", "in_progress"]))
                .order_by(Task.created_at.desc()).limit(10)
            )
            tasks = tasks_result.scalars().all()
            tasks_text = "No pending tasks."
            if tasks:
                tasks_text = "\n".join(
                    f"- [{t.status}] {t.title} (priority: {t.priority}, assigned: {t.assigned_to or 'N/A'})"
                    for t in tasks
                )

            # Recent meetings (last 5) WITH their tasks — so AI can answer
            # "<name> meeting mein kya hua / kya tasks the".
            mq = await db.execute(sa_text(
                "SELECT id, title, started_at, ended_at, total_tasks, summary "
                "FROM meetings ORDER BY id DESC LIMIT 5"
            ))
            meeting_rows = mq.fetchall()
            meeting_blocks = []
            for m in meeting_rows:
                mid, mtitle, started, ended, mtasks, msummary = m
                # Live tasks list for this meeting
                if started and ended:
                    tq = await db.execute(sa_text("""
                        SELECT title, assigned_to, priority
                        FROM tasks
                        WHERE datetime(created_at) >= datetime(:start)
                          AND datetime(created_at) <= datetime(:end, '+60 seconds')
                        ORDER BY id DESC
                    """), {"start": started, "end": ended})
                elif started:
                    tq = await db.execute(sa_text("""
                        SELECT title, assigned_to, priority
                        FROM tasks
                        WHERE datetime(created_at) >= datetime(:start)
                        ORDER BY id DESC
                    """), {"start": started})
                else:
                    tq = None
                tlist = tq.fetchall() if tq else []
                block = f"- **{mtitle}** (id={mid}, {started})"
                if tlist:
                    for t in tlist:
                        block += f"\n    • {t[0]} → {t[1] or '?'} [{t[2] or 'medium'}]"
                else:
                    block += "\n    (no tasks)"
                if msummary:
                    block += f"\n    Summary: {msummary[:200]}"
                meeting_blocks.append(block)
            meetings_text = "\n".join(meeting_blocks) if meeting_blocks else "No meetings yet."

            # Recent transcriptions
            trans_result = await db.execute(
                select(Transcription).order_by(Transcription.created_at.desc()).limit(5)
            )
            transcriptions = trans_result.scalars().all()
            trans_text = "No recent transcriptions."
            if transcriptions:
                trans_text = "\n".join(
                    f"- [{t.language}] {t.text[:150]}" for t in transcriptions if t.text.strip()
                )

            # Stats
            total_tasks = await db.scalar(select(func.count(Task.id)))
            total_clients = await db.scalar(select(func.count(Client.id)))
            total_meetings = await db.scalar(sa_text("SELECT COUNT(*) FROM meetings"))

        return f"""
### Clients ({total_clients}):
{clients_text}

### Active Tasks:
{tasks_text}

### Recent Meetings ({total_meetings} total, last 5 below — har meeting ke tasks listed):
{meetings_text}

### Recent Conversations:
{trans_text}

### Current Time: {datetime.now().strftime('%Y-%m-%d %H:%M')}
"""

    async def _save_message(self, role: str, content: str, actions: str = "[]") -> None:
        """Save chat message to database."""
        async with async_session() as db:
            await db.execute(
                __import__('sqlalchemy').text(
                    "INSERT INTO chat_messages (role, content, actions) VALUES (:role, :content, :actions)"
                ),
                {"role": role, "content": content, "actions": actions},
            )
            await db.commit()

    async def _load_history(self) -> list[dict]:
        """Load last 20 chat messages from database."""
        async with async_session() as db:
            result = await db.execute(
                __import__('sqlalchemy').text(
                    "SELECT role, content FROM chat_messages ORDER BY id DESC LIMIT 20"
                )
            )
            rows = result.fetchall()
        # Reverse to get chronological order
        return [{"role": r[0], "content": r[1]} for r in reversed(rows)]

    async def get_history(self) -> list[dict]:
        """Get all chat messages for dashboard display."""
        async with async_session() as db:
            result = await db.execute(
                __import__('sqlalchemy').text(
                    "SELECT id, role, content, actions, created_at FROM chat_messages ORDER BY id ASC"
                )
            )
            rows = result.fetchall()
        return [
            {
                "id": r[0], "role": r[1], "content": r[2],
                "actions": json.loads(r[3]) if r[3] else [],
                "created_at": r[4],
            }
            for r in rows
        ]

    async def chat(
        self,
        user_message: str,
        confirm_token: str | None = None,
        attachments: list[str] | None = None,
    ) -> dict:
        """Process a chat message and return response + any actions.

        confirm_token: if set, user has confirmed a previously-pending action.
        attachments: server-side paths to files the user attached via
            `POST /api/chat/upload`. When present AND the message resolves to
            a send_* action, these paths are forced into the action's
            `attachment` param so the LLM doesn't have to search the laptop.
        """
        client = self._get_client()
        attachments = [a for a in (attachments or []) if a]

        # Save user message to DB
        await self._save_message("user", user_message)

        # STOP — let the user halt a running task at any time. Set the flag and
        # return immediately; the running engine/vision loop bails next step.
        if self._is_stop_command(user_message):
            from app.services.task_control import request_stop
            request_stop()
            reply = "🛑 Boss, rok raha hoon — jo task chal raha tha woh agle step pe ruk jayega."
            await self._save_message("assistant", reply, "[]")
            return {"reply": reply, "actions": []}

        # If verification flow active, route through it (typed commands)
        verification_result = await self._try_verification(user_message)
        if verification_result is not None:
            actions_payload = []
            if verification_result.get("verification"):
                actions_payload = [{"verification": verification_result["verification"]}]
            await self._save_message("assistant", verification_result["reply"], json.dumps(actions_payload))
            return {
                "reply": verification_result["reply"],
                "actions": actions_payload,
            }

        # RESUME a paused engine task — if the engine asked a clarifying
        # question last turn, this message is the answer. Continue from there.
        # (A clear "cancel"/"chodo" abandons the paused task.)
        if self._engine_session is not None:
            msg_lower_stripped = user_message.strip().lower()
            if msg_lower_stripped in ("cancel", "chodo", "rehne do", "nahi", "no", "stop"):
                self._engine_session = None
                reply = "Theek hai Boss, woh task chhod diya."
                await self._save_message("assistant", reply, "[]")
                return {"reply": reply, "actions": []}
            session = self._engine_session
            self._engine_session = None
            engine_result = await self._run_engine_task(
                user_message, resume_transcript=session.get("transcript")
            )
            await self._save_message(
                "assistant", engine_result["reply"],
                json.dumps(engine_result.get("actions", [])),
            )
            return engine_result

        # VISION control — if the user asks JARVIS to "dekh ke" / "screen se" /
        # "vision se" do something, run the see→act loop (works on ANY app/web).
        if self._is_vision_request(user_message):
            from app.services.laptop_control.laptop_native import LaptopNative
            nat = LaptopNative.get()
            # remember where the user was (dashboard) so we can return after
            origin = await asyncio.to_thread(nat.active_window_title)

            res = None
            # SPEED: if this is actually a known chat-app send, do it INSTANTLY
            # via the deterministic path — no slow per-step vision (Zain: "bot
            # late message gaya"). Vision is the fallback only if that fails.
            parsed = self._parse_chat_send(user_message)
            if parsed:
                app, contact, message = parsed
                res = await asyncio.to_thread(nat.chat_send, app, contact, message)
                if not res.get("ok"):
                    res = None     # deterministic couldn't — fall to vision
            if res is None:
                res = await asyncio.to_thread(self._run_vision, user_message)

            # cleanup: minimize the app + bring the dashboard back to front
            await asyncio.to_thread(self._post_send_cleanup, origin)

            reply = f"{'✅' if res.get('ok') else '⚠️'} {res.get('reply', res.get('msg', res.get('error','')))}"
            await self._save_message("assistant", reply, "[]")
            return {"reply": reply, "actions": [{
                "action": "vision_task",
                "status": "success" if res.get("ok") else "failed",
                "message": res.get("reply", res.get("msg", "")),
            }]}

        # ENGINE-FIRST for compound / in-app tasks. The legacy intent path is
        # great at atomic commands (open app, find file) but mis-handles
        # multi-step or "type inside an app" tasks — e.g. "notepad kholo AUR
        # usme likho X" used to match the file-open rule and only open a
        # folder named "notepad". Such tasks need the universal engine, which
        # actually drives the app via UIA. Destructive ones still confirm.
        if self._needs_engine_first(user_message):
            # Confirm before sending to a person (chat-app send) or anything
            # destructive — so a wrong message never goes out silently.
            needs_confirm = (
                self._is_chat_app_send(user_message)
                or any(w in user_message.lower() for w in self._DESTRUCTIVE_WORDS)
            )
            engine_result = await self._run_engine_task(
                user_message, attachments, needs_confirm=needs_confirm
            )
            await self._save_message(
                "assistant", engine_result["reply"],
                json.dumps(engine_result.get("actions", [])),
            )
            return engine_result

        # RULE-BASED SEND CHECK FIRST — if user typed an obvious send command
        # (e.g. "PDF send kr da" with phone) we MUST treat it as a send action,
        # NOT as a screen-referential query. Earlier the word "pdf" triggered
        # screen-awareness which bypassed laptop_control entirely, letting
        # the chat LLM hallucinate "already sent it" replies.
        rule_send_intent = self._rule_based_send_intent(user_message)
        rule_file_intent = self._rule_based_file_intent(user_message)

        # Screen-referential queries ("yeh pdf mein kya hai", etc.) bypass
        # laptop_control — but NOT when the same message is clearly a send
        # OR a file/folder open command. Casual phrasings like "X dekh"
        # were getting incorrectly classified as screen-referential.
        from app.services.screen_aware import is_screen_referential
        skip_laptop_control = (
            is_screen_referential(user_message)
            and rule_send_intent is None
            and rule_file_intent is None
        )

        # First — check if this is a laptop control command (unless screen-referential)
        if not skip_laptop_control:
            laptop_result = await self._try_laptop_control(
                user_message, confirm_token, attachments=attachments
            )
            if laptop_result is not None:
                await self._save_message("assistant", laptop_result["reply"], json.dumps(laptop_result.get("actions", [])))
                return laptop_result

        context = await self._build_context()

        # Auto-save memory if user is teaching
        saved_memory = await self._try_save_memory(user_message)

        # Inject saved memories into system prompt
        memory_block = await self._build_memory_block()

        # Screen-Aware AI: DISABLED for now to save vision-LLM quota. Set
        # _SCREEN_AWARE_ENABLED to True to re-enable.
        _SCREEN_AWARE_ENABLED = False
        screen_block = None
        if _SCREEN_AWARE_ENABLED:
            screen_block = await self._try_screen_context(user_message)

        # Load history from DB (persistent)
        history = await self._load_history()

        full_system = SYSTEM_PROMPT.replace("__CONTEXT_PLACEHOLDER__", context)
        if memory_block:
            full_system += f"\n\n{memory_block}"
        if screen_block:
            full_system += f"\n\n{screen_block}"

        messages = [
            {"role": "system", "content": full_system},
            *history,
        ]

        try:
            response = client.chat.completions.create(
                model=self._config.groq_model,
                messages=messages,
                temperature=0.7,
                max_tokens=1000,
            )

            reply = response.choices[0].message.content.strip()

            # Hallucinated-success guard: small LLM models tend to copy phrases
            # from earlier successful sends in conversation history and
            # generate fake "✅ ko bhej diya" replies WITHOUT actually doing
            # anything. If the user's message looks like a send command (whatsapp,
            # email, teams), but the LLM produced no action_block AND its reply
            # contains success-style phrases, we rewrite the reply to be
            # honest. Better to say "I didn't understand" than lie about sending.
            import re as _re
            send_intent_pattern = _re.compile(
                r"\b(whatsapp|whats app|teams|email|gmail|mail|message|msg|send|bhej|bhejo|bhajo|bhaj)\b",
                _re.IGNORECASE,
            )
            fake_success_pattern = _re.compile(
                r"(bhej diya|bhej diy|bhejdiya|send kr diya|send kar diya|send kr di|sent successfully|✅.*?bhej|📲.*?bhej|📧.*?email|💼.*?teams|kar diya)",
                _re.IGNORECASE,
            )
            if (
                send_intent_pattern.search(user_message)
                and "```action" not in reply
                and fake_success_pattern.search(reply)
            ):
                log.warning(
                    "hallucinated_success_blocked",
                    user_msg=user_message[:80],
                    fake_reply=reply[:120],
                )
                reply = (
                    "⚠️ Sorry boss — main yeh send actually nahi kar paya. "
                    "Tumhari command thodi unclear thi.\n\n"
                    "Yeh format mein dobara try karo:\n"
                    "  • `Saif ko whatsapp pe 'kal milte hain' bhej`\n"
                    "  • `+92300... ko whatsapp pe hello bhej`\n"
                    "  • PDF/Image ke liye: dashboard mein 📎 paperclip se file attach karo, "
                    "phir command bhejo (mein automatic file pickup karta)."
                )

            # Check if response contains an action
            actions = []
            if "```action" in reply:
                parts = reply.split("```action")
                clean_reply_parts = [parts[0]]
                for part in parts[1:]:
                    action_json, rest = part.split("```", 1)
                    clean_reply_parts.append(rest)
                    try:
                        action = json.loads(action_json.strip())
                        executed = await self._execute_action(action)
                        actions.append(executed)
                    except (json.JSONDecodeError, Exception) as e:
                        log.warning("chat_action_failed", error=str(e))

                reply = "".join(clean_reply_parts).strip()

            # CLEANUP: the conversational LLM sometimes leaks a bare ```json
            # action block (e.g. {"action":"send_message",...}) — that's raw
            # clutter to the user. Strip ALL fenced blocks; if one is a
            # send/message action, route it to the engine so it actually runs.
            stripped, leaked = self._strip_action_code_blocks(reply)
            if leaked is not None:
                reply = stripped or ""
                engine_res = await self._run_engine_task(self._leaked_action_to_task(leaked))
                reply = (reply + "\n\n" + engine_res.get("reply", "")).strip()
                actions.extend(engine_res.get("actions", []))

            # Add memory-saved confirmation if applicable
            if saved_memory:
                reply = f"🧠 Yaad rakh liya: *{saved_memory['content']}*\n\n{reply}"

            log.info("chat_response", message_len=len(reply), actions=len(actions))

            # Save assistant reply to DB
            await self._save_message("assistant", reply, json.dumps(actions))

            return {
                "reply": reply,
                "actions": actions,
            }

        except Exception as e:
            err_str = str(e).lower()
            log.error("chat_error", error=str(e))
            if "connection" in err_str or "timeout" in err_str:
                msg = "⚠️ Internet ya Groq API se connection issue. Phir try kar — usually 5-10 sec mein theek ho jata hai."
            elif "rate" in err_str or "quota" in err_str or "429" in err_str:
                msg = "⚠️ Groq daily limit (100k tokens) khatam ho gayi. Kal subah reset hogi — ya Gemini API laga le."
            elif "auth" in err_str or "401" in err_str or "403" in err_str:
                msg = "⚠️ API key issue — JARVIS_GROQ_API_KEY check kar."
            else:
                msg = f"Sorry, error aa gayi: {e}"
            return {"reply": msg, "actions": []}

    async def _try_verification(self, user_message: str) -> dict | None:
        """If verification session active, route typed input to verification."""
        from app.services.verification import VerificationService
        vs = VerificationService.get()
        if not vs.active_session:
            return None

        msg = user_message.strip().lower()
        state = vs.active_session.get("state")

        if state == "awaiting_approval":
            if msg in ("approve", "y", "yes", "ha", "haan", "ok", "✅"):
                return await vs.approve_current()
            if msg in ("reject", "n", "no", "nahi", "skip", "❌"):
                return await vs.reject_current()
            if msg in ("cancel", "stop", "exit"):
                return await vs.cancel()
        elif state == "awaiting_platform":
            if msg in ("whatsapp", "wa"):
                return await vs.send_via_platform("whatsapp", self._config)
            if msg in ("teams", "team"):
                return await vs.send_via_platform("teams", self._config)
            if msg in ("email", "gmail", "mail"):
                return await vs.send_via_platform("email", self._config)
            if msg in ("cancel", "stop"):
                return await vs.cancel()
        return None

    # Words that must NEVER be picked as a recipient name. Keep in sync with the
    # PRONOUNS set used in _try_laptop_control's safety override below.
    _PRONOUN_STOPWORDS = frozenset({
        "ais", "is", "isko", "isay", "isi", "us", "usko", "usi", "usay",
        "yeh", "ye", "yh", "wo", "woh", "wahi", "ws",
        "yaha", "wahaa", "yahan", "wahan", "uha",
        "this", "that", "it", "him", "her",
        # generic words that appear around send commands but aren't names
        "logo", "file", "image", "photo", "document", "doc", "pdf", "pic",
        "picture", "video", "audio", "voice", "message", "msg", "msj",
        "bhej", "bhejo", "bhejna", "bhejna", "send", "kar", "karo", "karna",
        "kr", "krdo", "ka", "ki", "ke", "ko", "ko",
        "pe", "par", "se", "tak", "lo", "le", "da",
        "whatsapp", "wa", "teams", "team", "email", "mail", "gmail",
        "main", "mein", "may", "abhi", "ab", "tu", "tum", "ap", "aap", "ham",
        "hum", "boss", "bhai", "yar", "yaar", "yr", "yara",
        "haan", "ha", "nahi", "no", "yes", "ok",
        "phir", "again", "wapas", "back", "dobara",
        "ye logo", "ye file", "ye image", "ye photo",
    })

    @staticmethod
    def _extract_recipient_from_message(message: str) -> str | None:
        """Pull a real recipient (name / phone / email) out of the raw user message
        when the LLM picked a pronoun. Returns first plausible token that isn't a
        pronoun/stopword. Phone numbers and emails are preferred over names.
        """
        if not message:
            return None
        import re as _re

        # 1) Email wins outright
        m = _re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", message)
        if m:
            return m.group(0)

        # 2) Phone number — Pakistani patterns
        m = _re.search(r"\+?\d[\d\s\-]{6,}\d", message)
        if m:
            digits = _re.sub(r"\D", "", m.group(0))
            if 7 <= len(digits) <= 15:
                return digits

        # 3) Look for "<name> ka {whatsapp|teams|email}" — captures the name
        m = _re.search(
            r"\b([A-Za-z][a-zA-Z']{1,30})\s+(?:ka|ki|ke|ko|wala|wali)\s+"
            r"(?:whatsapp|wa|teams|team|email|mail|gmail)",
            message,
            flags=_re.IGNORECASE,
        )
        if m:
            cand = m.group(1)
            if cand.lower() not in ChatService._PRONOUN_STOPWORDS:
                return cand

        # 4) Look for "<name> ko" (recipient marker)
        for m in _re.finditer(r"\b([A-Za-z][a-zA-Z']{1,30})\s+ko\b", message, flags=_re.IGNORECASE):
            cand = m.group(1)
            if cand.lower() not in ChatService._PRONOUN_STOPWORDS:
                return cand

        # 5) Last resort — first non-stopword word that looks name-ish
        for tok in _re.findall(r"[A-Za-z][a-zA-Z']{1,30}", message):
            if tok.lower() not in ChatService._PRONOUN_STOPWORDS:
                return tok

        return None

    @staticmethod
    def _rule_based_file_intent(user_message: str) -> dict | None:
        """Deterministic catch for file/folder open/find commands.

        Matches casual phrasings like:
            "X kholo", "X khol", "X open kar"
            "X dekh", "X dikhao", "X dikha"
            "X find kar", "X dhoondh"
            "X yaha file dekh / dikhao"  (user often says "file" for folder)
            "X folder kholo"

        Decides between open_folder / open_file / find_file based on:
            - "find" / "dhoondh" verb → find_file
            - has file extension (.pdf .docx .txt .xlsx etc.) → open_file
            - default → open_folder (folders are the common case)
        """
        import re as _re
        msg = (user_message or "").strip()
        if not msg or len(msg) > 300:
            return None

        msg_low = msg.lower()

        # Reject obvious non-file commands so we don't grab "WhatsApp pe X bhej"
        # or "Excel mein..." etc.
        non_file_indicators = (
            "whatsapp", "teams", "gmail", "outlook", "email", "send",
            "excel mein", "word mein", "powerpoint", "calculator",
            "wifi", "bluetooth", "settings kholo", "calendar",
        )
        if any(ind in msg_low for ind in non_file_indicators):
            return None

        # Find action verbs and split the message around them
        OPEN_VERBS = (
            r"kholo", r"khol\b", r"open\s+kar", r"open\b",
            r"dekh(?:ao|na|o)?", r"dikhao", r"dikha\b",
            r"show", r"display",
        )
        FIND_VERBS = (
            r"find\s+kar", r"find\b", r"dhoondh\b", r"dhoond\b", r"search\b",
        )

        # Try FIND verbs first
        for vp in FIND_VERBS:
            m = _re.search(rf"^(.+?)\s+({vp})\b", msg, _re.IGNORECASE)
            if m:
                target = m.group(1).strip().rstrip(",.?!").strip("'\"")
                if 2 <= len(target) <= 200:
                    return {"action": "find_file", "params": {"query": target}}

        # Try OPEN verbs
        for vp in OPEN_VERBS:
            # Pattern A: "X verb"  e.g. "MyFolder kholo"
            m = _re.search(rf"^(.+?)\s+({vp})\b", msg, _re.IGNORECASE)
            if m:
                target = m.group(1).strip()
                # Strip common trailing words to extract clean target
                target = _re.sub(
                    r"\b(yaha|yeh|ye|wo|wala|wali|walaa|folder|file|ko|ka|ki|ke|me|mein|mujhay|mujhe|tujay|tujhe)\b\s*",
                    "", target, flags=_re.IGNORECASE
                ).strip().rstrip(",.?!").strip("'\"")
                # Reasonable target length
                if 2 <= len(target) <= 200:
                    # Decide file vs folder by extension
                    has_ext = bool(_re.search(r"\.[a-z]{2,5}$", target, _re.IGNORECASE))
                    if has_ext:
                        return {"action": "open_file", "params": {"path": target}}
                    return {"action": "open_folder", "params": {"name": target}}

        # Pattern B: "verb X"  e.g. "kholo MyFolder"
        for vp in OPEN_VERBS:
            m = _re.search(rf"\b({vp})\s+(.+?)$", msg, _re.IGNORECASE)
            if m:
                target = m.group(2).strip().rstrip(",.?!").strip("'\"")
                target = _re.sub(r"\b(yaha|yeh|ye|wo|wala|wali|walaa|folder|file|ko|mujhe|tujhe)\b\s*", "", target, flags=_re.IGNORECASE).strip()
                if 2 <= len(target) <= 200:
                    has_ext = bool(_re.search(r"\.[a-z]{2,5}$", target, _re.IGNORECASE))
                    if has_ext:
                        return {"action": "open_file", "params": {"path": target}}
                    return {"action": "open_folder", "params": {"name": target}}

        return None

    @staticmethod
    def _rule_based_send_intent(user_message: str) -> dict | None:
        """Deterministic regex-based catch for OBVIOUS send commands.

        Bypasses the LLM intent detector for clear send-to-someone-something
        patterns. This is the "rule-based pre-filter" the user asked for —
        catches the common case so the LLM never gets a chance to hallucinate.

        Returns an action dict shaped like the LLM's output, or None if no
        obvious pattern matches (in which case we fall through to LLM intent).
        """
        import re as _re
        msg = user_message.strip()
        msg_low = msg.lower()

        # Detect platform from keywords
        platform = None
        is_business = False
        # behind_mode → position WhatsApp window off-screen, user only sees
        # JARVIS dashboard. Triggered by these keywords ONLY when they appear
        # OUTSIDE quoted text — otherwise a user forwarding a quote like
        # "Please revise the background color" would incorrectly trigger it.
        # We strip the body of any 'single/double quoted' content before testing.
        _msg_for_behind_test = _re.sub(r"['\"][^'\"]+['\"]", "", msg_low)
        is_behind = bool(_re.search(
            r"\b(background\s+mein|behind\s+mein|chupke\s+se|hidden\s+mode|"
            r"background\s+mode|behind\s+mode|invisible\s+mode|chupke\s+chupke)\b",
            _msg_for_behind_test,
        ))
        if _re.search(r"\b(whatsapp|whats\s*app|wa\s*pe|wa\s*pa)\b", msg_low):
            platform = "whatsapp"
            # Detect "Business" or "WhatsApp Business" context — handles
            # both word orders ("business whatsapp" / "whatsapp business")
            if _re.search(r"\bbusiness\b", msg_low) or _re.search(r"\bwa[\s-]*business\b", msg_low):
                is_business = True
        elif _re.search(r"\b(teams|ms\s*teams|microsoft\s*teams)\b", msg_low):
            platform = "teams"
        elif _re.search(r"\b(email|gmail|mail)\b", msg_low) and "@" in msg:
            platform = "email"
        if not platform:
            return None

        # Extract phone number (international or local Pakistani format)
        phone_match = _re.search(r"(\+?\d[\d\s\-]{7,15}\d)", msg)
        phone = phone_match.group(1) if phone_match else None

        # Extract email address
        email_match = _re.search(r"([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})", msg)
        email = email_match.group(1) if email_match else None

        # Extract recipient name. Strategy:
        #  - Prefer the LAST "<Name> ko" in the message (closest to the
        #    send instruction), since earlier names often refer to whose
        #    content it is (e.g. "Muzzamil ki notes ise WhatsApp Ali ko"
        #    → recipient is Ali, not Muzzamil).
        #  - "ko" is the strongest particle for recipient (always means
        #    "to"). "ki"/"ke" can mean "of/'s" → demote them.
        recipient_name = None
        # All "<Name> ko" matches — take the LAST one
        ko_matches = list(_re.finditer(
            r"\b([A-Z][a-zA-Z'\-]{1,30}(?:\s+[A-Z][a-zA-Z'\-]{1,30})?)\s+ko\b",
            msg,
        ))
        for cand_m in reversed(ko_matches):
            cand = cand_m.group(1).strip()
            if cand.lower() not in ChatService._PRONOUN_STOPWORDS:
                recipient_name = cand
                break
        if not recipient_name:
            # Lowercase fallback for Roman Urdu, also take LAST "ko" match
            ko_lower = list(_re.finditer(
                r"\b([a-z][a-z'\-]{1,30}(?:\s+[a-z][a-z'\-]{1,30})?)\s+ko\b",
                msg_low,
            ))
            for cand_m in reversed(ko_lower):
                cand = cand_m.group(1).strip()
                if cand.lower() not in ChatService._PRONOUN_STOPWORDS:
                    recipient_name = cand
                    break
        if not recipient_name:
            # Fallback to "ki"/"ke" too (weak signal — last resort)
            m = _re.search(
                r"\b([A-Z][a-zA-Z'\-]{1,30}(?:\s+[A-Z][a-zA-Z'\-]{1,30})?)\s+(?:ki|ke)\b",
                msg,
            )
            if m:
                cand = m.group(1).strip()
                if cand.lower() not in ChatService._PRONOUN_STOPWORDS:
                    recipient_name = cand

        # Extract message body inside quotes 'like this' or "like this"
        msg_match = _re.search(r"['\"]([^'\"]+)['\"]", msg)
        body = msg_match.group(1) if msg_match else None

        # Without a body in quotes, try to peel out the message body.
        # Two distinct patterns:
        #
        # PATTERN A — body AFTER platform keyword:
        #   "WhatsApp pe Saif ko 'hello' bhej"
        #   "Saif ko WhatsApp pe HELLO bhej"
        #
        # PATTERN B — body BEFORE send instruction (the case Zain hit):
        #   "1. Review Photos\n2. Contract Docs\n3. Website finalize\n
        #    yaha whatsapp kr Zaid Zenesa ko bhai"
        # Trigger words like "yaha"/"yeh"/"ise"/"isko" pinpoint where the
        # send instruction starts — everything BEFORE that is the body.
        if not body:
            # Pattern B FIRST — search for an "anchor" trigger word that
            # marks the start of the send instruction. If found AND the
            # text before it is substantive (>20 chars), that text is the body.
            anchor_pat = _re.search(
                r"(?:^|\n|[.!?\s])\s*(?P<anchor>(?:yaha|yahaan|ye|yeh|isse?|isko|iss\s+ko|ab|aage|ise|inhe|iska))\s+(?:whatsapp|whats\s*app|wa|teams|email|gmail|mail)\b",
                msg,
                _re.IGNORECASE,
            )
            if anchor_pat:
                anchor_start = anchor_pat.start("anchor")
                pre_anchor = msg[:anchor_start].strip()
                pre_anchor = pre_anchor.rstrip(" \t\n.,;:!?")
                # Lowered threshold to 10 chars — short task lists still count
                if len(pre_anchor) >= 10:
                    body = pre_anchor

        if not body:
            # Pattern A — body after platform keyword
            mb = _re.search(
                r"(?:whatsapp|teams|email|mail|ko)\s+(?:pe|pa)\s+(.+?)\s+(?:bhej|bhejo|bhaj|send|kar|kr|de|do|kr da)\b",
                msg_low,
                _re.IGNORECASE,
            )
            if mb:
                body = mb.group(1).strip()
                body = body.strip("'\"")

        # If we have NO body, defer to the LLM intent parser — the body may
        # be implicit ("Muzzamil ke tasks bhej do") and only the LLM with
        # chat history can resolve it. Returning a half-filled action here
        # leads to a failed send with no helpful message.
        body_clean = (body or "").strip()

        # FIX: When Pattern A regex captures "{recipient} ko" as body, that's
        # NOT a real message — it's the recipient mention. Strip such captures.
        # Example bug: "WhatsApp pa Zaid Zenesa ko send" → body wrongly = "Zaid Zenesa ko"
        if body_clean and recipient_name:
            body_low = body_clean.lower().strip()
            rec_low = recipient_name.lower().strip()
            # Body is exactly "{recipient}" or "{recipient} ko/ki/ke" → recipient mention, not message
            if body_low in (rec_low, f"{rec_low} ko", f"{rec_low} ki", f"{rec_low} ke"):
                body_clean = ""
            # Body STARTS with recipient mention — strip the prefix
            elif body_low.startswith(f"{rec_low} ko ") or body_low.startswith(f"{rec_low} ki ") or body_low.startswith(f"{rec_low} ke "):
                # Drop the "{recipient} ko/ki/ke " prefix from original body (preserve case)
                prefix_len = len(rec_low) + 4  # name + " ko " etc.
                body_clean = body_clean[prefix_len:].strip()

        if not body_clean:
            return None

        # Build action based on platform
        if platform == "whatsapp":
            if not (phone or recipient_name):
                return None
            params = {
                "recipient": phone or recipient_name,
                "message": body_clean,
            }
            if is_business:
                params["business"] = True
            if is_behind:
                params["behind_mode"] = True
            return {
                "action": "send_whatsapp_message",
                "params": params,
            }
        if platform == "teams":
            if not recipient_name:
                return None
            return {
                "action": "send_teams_message",
                "params": {
                    "recipient": recipient_name,
                    "message": body_clean,
                },
            }
        if platform == "email":
            if not email:
                return None
            return {
                "action": "send_email",
                "params": {
                    "to": email,
                    "subject": "(no subject)",
                    "body": body_clean,
                },
            }
        return None

    async def _try_laptop_control(
        self,
        user_message: str,
        confirm_token: str | None,
        attachments: list[str] | None = None,
    ) -> dict | None:
        """Try to route as a laptop control command. Returns None if not a laptop command."""
        from app.services.laptop_control import LaptopController
        if self._laptop is None:
            self._laptop = LaptopController(self._config)

        msg_lower = user_message.strip().lower()
        is_yes = msg_lower in ("y", "yes", "ha", "haan", "ok", "confirm", "haan kar", "ha kar do", "kar do", "haan karo", "ji haan")
        is_no = msg_lower in ("n", "no", "nahi", "cancel", "mat kar", "rok", "stop")

        # Handle confirmation of a previously pending action via explicit token
        if confirm_token:
            async with self._pending_lock:
                pending = self._pending_actions.pop(confirm_token, None)
            if pending is not None:
                if is_yes:
                    return await self._confirm_and_run(pending)
                return {"reply": "Theek hai, cancel kar diya.", "actions": []}

        # Auto-detect confirmation reply (user typed "yes"/"no" without token)
        if is_yes or is_no:
            async with self._pending_lock:
                if self._pending_actions:
                    # Pop the most recent pending action atomically
                    token = next(reversed(self._pending_actions))
                    pending = self._pending_actions.pop(token)
                else:
                    pending = None
            if pending is not None:
                if is_yes:
                    return await self._confirm_and_run(pending)
                return {"reply": "Theek hai, cancel kar diya.", "actions": []}

        # Detect intent — pass recent history + structured recent send-actions so
        # pronouns ("is ko", "wahi") and implicit platform ("again", "phir") resolve
        # against precise context (last recipient phone, last platform, etc.) not
        # just chat text.
        try:
            history = await self._load_history()
            if history and history[-1].get("role") == "user" and history[-1].get("content") == user_message:
                history = history[:-1]

            from app.services.laptop_control.activity_log import ActivityLogger
            try:
                recent_actions = await ActivityLogger.recent_send_summary(limit=5)
            except Exception as ae:
                log.debug("recent_actions_fetch_failed", error=str(ae))
                recent_actions = []

            # DETERMINISTIC RULE-BASED PRE-FILTER: if the user typed an
            # obvious "send X to Y on whatsapp/teams/email" command, build
            # the action directly via regex — don't ask the LLM. This catches
            # 95% of common send commands and CANNOT hallucinate.
            rule_action = self._rule_based_send_intent(user_message)
            if rule_action:
                log.info(
                    "rule_based_intent_caught",
                    action=rule_action.get("action"),
                    recipient=rule_action.get("params", {}).get("recipient")
                        or rule_action.get("params", {}).get("to"),
                )
                actions = [rule_action]
            else:
                # File / folder rule-based pre-filter — catches casual phrasings
                # like "X kholo", "X dikhao", "X dekh", "X yaha file dekh" that
                # the LLM tends to mis-classify as conversational. This is
                # deterministic and CANNOT hallucinate.
                rule_file = self._rule_based_file_intent(user_message)
                if rule_file:
                    log.info("rule_based_file_intent_caught", action=rule_file.get("action"))
                    actions = [rule_file]
                else:
                    actions = await self._laptop.parse(
                        user_message, history=history, recent_actions=recent_actions
                    )
        except Exception as e:
            log.warning("laptop_intent_failed", error=str(e))
            return None

        if not actions:
            return await self._maybe_engine_fallback(user_message, attachments)

        # If only a pure "chat" action — fall through to normal chat
        if len(actions) == 1 and actions[0].get("action") == "chat":
            return await self._maybe_engine_fallback(user_message, attachments)

        # Filter out chat actions mixed with real actions
        laptop_actions = [a for a in actions if a.get("action") != "chat"]
        if not laptop_actions:
            return await self._maybe_engine_fallback(user_message, attachments)

        # Per-app scraper actions were removed in the repivot — those tasks
        # (WhatsApp/Teams sends etc.) now go to the universal engine, which
        # drives the real apps via UIA. Sends stay behind a confirm step.
        from app.services.laptop_control.controller import REMOVED_ACTIONS
        if any(a.get("action") in REMOVED_ACTIONS for a in laptop_actions):
            return await self._run_engine_task(
                user_message, attachments, needs_confirm=True
            )

        # Code-level safety net: if the LLM picked a Roman Urdu pronoun as the
        # recipient (e.g. "Ais", "is", "yeh"), override it by extracting the real
        # name from the original message. This is a hard guarantee — not
        # dependent on LLM judgment. Works for ANY name (zain/Ahmed/anyone),
        # because we filter OUT the closed set of pronouns and prefer anything
        # else (capitalized word OR phone number OR email) as the recipient.
        SEND_ACTIONS = {"send_whatsapp_message", "send_teams_message", "send_email"}
        PRONOUNS = {
            "ais", "is", "isko", "isay", "isi", "us", "usko", "usi", "usay",
            "yeh", "ye", "yh", "wo", "woh", "wahi", "ws",
            "yaha", "wahaa", "yahan", "wahan", "uha",
            "this", "that", "it", "him", "her",
        }
        for a in laptop_actions:
            if a.get("action") not in SEND_ACTIONS:
                continue
            params = a.setdefault("params", {})
            rec_key = "to" if a["action"] == "send_email" else "recipient"
            rec = params.get(rec_key)
            # For email "to" can be a list — only sanity-check string single values
            if isinstance(rec, str) and rec.strip().lower() in PRONOUNS:
                better = self._extract_recipient_from_message(user_message)
                if better:
                    log.info("recipient_pronoun_overridden", was=rec, now=better)
                    params[rec_key] = better

        # If the user uploaded files in the dashboard chat, force them into the
        # send_* action params so the LLM-derived "attachment" is overridden by
        # the explicit, server-resolved temp path.
        if attachments:
            ATTACHMENT_ACTIONS = {
                "send_whatsapp_message", "send_teams_message", "send_email",
            }
            for a in laptop_actions:
                if a.get("action") not in ATTACHMENT_ACTIONS:
                    continue
                params = a.setdefault("params", {})
                if a["action"] == "send_email":
                    # Email supports multiple attachments
                    existing = params.get("attachments") or params.get("attachment") or []
                    if isinstance(existing, str):
                        existing = [existing]
                    params["attachments"] = list(existing) + list(attachments)
                    params.pop("attachment", None)
                else:
                    # WhatsApp/Teams take a single attachment (first one wins; rest ignored for now)
                    params["attachment"] = attachments[0]

        # Check if any action needs confirmation
        results = []
        pending_list = []
        for a in laptop_actions:
            if self._laptop.needs_confirmation(a):
                # Cryptographically random token — earlier we used
                # `hash(str(a)) % 10000` which collided across distinct actions,
                # causing the wrong pending action to be executed on confirm.
                token = f"confirm_{secrets.token_urlsafe(16)}"
                async with self._pending_lock:
                    self._pending_actions[token] = a
                pending_list.append((token, a))
            else:
                result = await self._laptop.execute(a)
                results.append(result)

        # Build reply
        reply_parts = []
        for r in results:
            icon = "✅" if r["status"] == "success" else ("🚫" if r["status"] == "blocked" else "❌")
            reply_parts.append(f"{icon} {r.get('message', '')}")

        for token, a in pending_list:
            summary = self._describe_action(a)
            reply_parts.append(
                f"{summary}\n\n"
                "➡️ **Send karne ke liye type karo:** `yes` ya `haan`\n"
                "❌ **Cancel ke liye:** `no` ya `nahi`\n"
                "✏️ **Edit ke liye:** dobara command bolo correct details ke saath"
            )

        reply = "\n\n".join(reply_parts) if reply_parts else "Kuch nahi hua"

        return {
            "reply": reply,
            "actions": results,
            "pending": [{"token": t, "action": a} for t, a in pending_list],
        }

    # ==================================================================
    # Universal Engine routing (repivot step 3)
    # ==================================================================

    # Imperative verbs that signal "do something on the laptop" — used only
    # when the intent detector found nothing, to decide engine vs plain chat.
    _TASK_VERBS = (
        "kholo", "khol", "banao", "bana", "rename", "karo", "kar do", "krdo",
        "kardo", "delete", "hatao", "uda do", "move", "copy", "likho", "likh",
        "chalao", "chala", "install", "band karo", "band kr", "close",
        "open", "create", "type", "download", "search karo", "dhundo",
        "organize", "saaf karo", "arrange", "sort",
    )
    _DESTRUCTIVE_WORDS = ("delete", "hatao", "uda do", "remove kar", "format")

    @classmethod
    def _looks_like_task(cls, message: str) -> bool:
        low = f" {message.strip().lower()} "
        return any(v in low for v in cls._TASK_VERBS)

    _TYPING_VERBS = ("likho", "likh do", "likh de", "type karo", "type kar", "type kr")
    _IN_APP_MARKERS = ("usme", "us me", "isme", "is me", "us mein", "is mein")
    # Chat apps the universal engine drives directly (NOT email — email has a
    # working SMTP path). Casual/typo spellings included.
    _CHAT_APPS = ("whatsapp", "whats app", "wts app", "watsapp", "teams", "team",
                  "slack", "discord", "telegram")
    _SEND_VERBS = ("bhej", "bhejo", "bhaj", "bhejna", "bhejde", "send", "message",
                   "msg", "likh", "likho", "bol do", "keh do")

    @classmethod
    def _is_chat_app_send(cls, message: str) -> bool:
        """A 'send something to someone on Teams/WhatsApp/etc.' command — these
        go straight to the universal engine (with confirm), NOT the old chat.
        If a chat-app name appears with a send verb OR a 'ko/pe' (to/on)
        preposition, it's a send (e.g. 'sami ko whatsapp pe salam')."""
        low = f" {message.strip().lower()} "
        has_app = any(a in low for a in cls._CHAT_APPS)
        if not has_app:
            return False
        has_verb = any(v in low for v in cls._SEND_VERBS) or cls._looks_like_task(message)
        has_prep = " ko " in low or " pe " in low or " pa " in low or " par " in low
        return has_verb or has_prep

    # "Where is X / find the X file/folder / iski location" — these MUST hit
    # the engine (real disk search via find_files/powershell). If they fall to
    # the chat LLM it INVENTS fake paths (it can't see the disk).
    _FIND_WORDS = ("location", "kahan", "kahaan", "kaha", "dhoondo", "dhundo",
                   "dhoond", "find", "locate", "path", "kis folder", "kis jagah",
                   "where is", "where's")
    _FIND_NOUNS = ("file", "folder", "document", "pdf", "photo", "image", "song",
                   "video", "excel", "word", ".pdf", ".docx", ".xlsx", ".png", ".jpg")

    @classmethod
    def _is_find_query(cls, message: str) -> bool:
        low = f" {message.strip().lower()} "
        has_find = any(w in low for w in cls._FIND_WORDS)
        has_noun = any(n in low for n in cls._FIND_NOUNS)
        # "location"/"kahan"/"path" with a file/folder noun → real search needed
        return has_find and has_noun

    @classmethod
    def _needs_engine_first(cls, message: str) -> bool:
        low = f" {message.strip().lower()} "
        # Chat-app sends → engine (this was the bug: they used to fall through
        # to the conversational LLM which just chatted instead of sending).
        if cls._is_chat_app_send(message):
            return True
        # File/folder find/location → engine (real search, not LLM guesses)
        if cls._is_find_query(message):
            return True
        # Typing into an app, or operating inside one
        if any(v in low for v in cls._TYPING_VERBS):
            return True
        if any(m in low for m in cls._IN_APP_MARKERS) and cls._looks_like_task(message):
            return True
        # Multi-step: "X kholo aur/phir Y" with two task verbs around a joiner
        if (" aur " in low or " phir " in low or " then " in low):
            verb_hits = sum(1 for v in cls._TASK_VERBS if v in low)
            if verb_hits >= 2:
                return True
        return False

    async def _maybe_engine_fallback(
        self, user_message: str, attachments: list[str] | None
    ) -> dict | None:
        """Intent detector ne kuch nahi pakda — agar message task jaisa hai
        to universal engine ko do, warna None (normal chat)."""
        if not self._looks_like_task(user_message):
            return None
        needs_confirm = any(
            w in user_message.lower() for w in self._DESTRUCTIVE_WORDS
        )
        return await self._run_engine_task(
            user_message, attachments, needs_confirm=needs_confirm
        )

    @staticmethod
    def _strip_action_code_blocks(reply: str):
        """Remove any fenced code block that is JSON containing an 'action'.

        Returns (cleaned_reply, leaked_action_or_None). Keeps the FIRST such
        action found (so we can route it to the engine); strips all of them
        from the visible text so raw JSON never reaches the user.
        """
        import re as _re
        if "```" not in (reply or ""):
            return reply, None
        leaked = None
        def _repl(m):
            nonlocal leaked
            body = m.group(1).strip()
            # drop a leading language tag like "json"
            if "\n" in body:
                first, rest = body.split("\n", 1)
                if first.strip().lower() in ("json", "action"):
                    body = rest.strip()
            try:
                obj = json.loads(body)
                if isinstance(obj, dict) and ("action" in obj or "platform" in obj):
                    if leaked is None:
                        leaked = obj
                    return ""  # strip it
            except (json.JSONDecodeError, ValueError):
                pass
            return m.group(0)  # not an action block — leave as-is
        cleaned = _re.sub(r"```(.*?)```", _repl, reply, flags=_re.DOTALL)
        return cleaned.strip(), leaked

    @staticmethod
    def _leaked_action_to_task(action: dict) -> str:
        """Turn a leaked {action:send_message,...} dict into an engine task."""
        platform = (action.get("platform") or "").strip() or "the app"
        to = (action.get("to") or action.get("recipient") or "").strip()
        msg = (action.get("message") or action.get("body") or action.get("text") or "").strip()
        return f"{platform} pe '{to}' ko yeh message bhejo: {msg}"

    # cues that mean "control the screen visually, like a human looking at it"
    _VISION_CUES = ("dekh ke", "dekh kar", "dekhke", "dekh k ", "screen se",
                    "screen dekh", "vision se", "vision", "screenshot le",
                    "khud dekh ke", "screen pe dekh")

    _STOP_WORDS = {
        "stop", "ruk", "ruk ja", "ruko", "ruk jao", "ruk jao", "roko", "rok do",
        "rok de", "band karo", "band kar", "band kr", "cancel karo", "abort",
    }

    @classmethod
    def _is_stop_command(cls, message: str) -> bool:
        return message.strip().lower().rstrip("!.") in cls._STOP_WORDS

    @classmethod
    def _is_vision_request(cls, message: str) -> bool:
        low = f" {message.strip().lower()} "
        return any(cue in low for cue in cls._VISION_CUES)

    def _run_vision(self, task: str) -> dict:
        """Blocking see→act loop (runs in a thread). Vision = Claude CLI only."""
        from app.services.laptop_control.vision_control import VisionController
        return VisionController.get().run(task)

    def _post_send_cleanup(self, origin: str | None) -> None:
        """After ANY app-control task: minimize the app we used + bring the
        dashboard back. Delegates to the engine's SINGLE-PASS cleanup (which
        minimizes the current foreground app + chat windows, then refocuses
        origin). Calling it exactly ONCE is important — the earlier version
        minimized the app AND then ran the engine cleanup, which re-read the
        foreground (now the dashboard) and minimized the dashboard too."""
        try:
            from app.services.universal_engine.engine import _post_task_cleanup
            _post_task_cleanup([], origin)
        except Exception:
            pass

    @staticmethod
    def _detect_chat_app(low: str) -> str | None:
        """Normalize any chat-app mention to a canonical key. Works for ANY of
        the common apps — not a hardcoded 2-app list."""
        import re as _re
        m = _re.search(
            r"\b(whats?\s*app|wa|teams?|slack|discord|telegram|tg|signal|messenger|skype)\b", low)
        if not m:
            return None
        tok = m.group(1).replace(" ", "")
        if tok in ("whatsapp", "whatsap", "wa"):
            return "whatsapp"
        if tok.startswith("team"):
            return "teams"
        if tok == "tg":
            return "telegram"
        return tok

    def _parse_chat_send(self, task: str) -> tuple[str, str, str] | None:
        """Parse a clean single-message chat-app send into (app, contact,
        message) for the FAST deterministic native path. Handles both word
        orders:
            'whatsapp pe <name> ko <msg> bhej'
            '<name> ko whatsapp pe <msg> bhej'
        Returns None if it's not a clean send (then the engine handles it).
        Works for ANY name — nothing is hardcoded; name+msg come from here."""
        import re as _re
        if not task:
            return None
        low = task.lower()
        app = self._detect_chat_app(low)
        if not app:
            return None
        idx = low.find(" ko ")
        if idx == -1:
            return None
        before, after = task[:idx], task[idx + 4:]
        _APP = r"whats?\s*app|wa|teams?|slack|discord|telegram|tg|signal|messenger|skype"
        # Roman-Urdu prepositions for on/in — pe/pa/par/mein/me AND ma/mai/may/mn
        # (Zain writes "whatsapp MA Zain ko" → 'ma' must be stripped or it becomes
        # part of the contact: "ma Zain Ul" → contact not found).
        _PREP = r"pe|pa|par|pr|mein|mei|me|ma|mai|may|mn|men|may"
        contact = _re.sub(rf"(?i)\b({_APP}|{_PREP})\b", " ", before)
        contact = contact.strip(" ,.'\"")
        # message = the 'after' part; strip a leading 'whatsapp pe/ma' (other order),
        # surrounding quotes, and any trailing send verb
        after = _re.sub(rf"(?i)^\s*({_APP})\s+({_PREP})?\s*", "", after)
        msg = after.strip().strip("'\"").strip()
        msg = _re.sub(
            r"(?i)\s+(bhej\s*do|bhej\s*de|bhejo|bhej|bhaj\s*do|bhaj|send\s*kar\s*do|"
            r"send|kar\s*do|kr\s*do|kardo|likh\s*do|likho|de\s*do|do)\s*$",
            "", msg).strip().strip("'\"").strip()
        if not contact or not msg or len(contact) > 40:
            return None
        return (app, contact, msg)

    def _parse_chat_app_and_contact(self, task: str) -> tuple[str, str] | None:
        """Lighter parse for FILE sends: just (app, contact), no message needed.
        Works for ANY name — contact comes from the task, nothing hardcoded."""
        import re as _re
        if not task:
            return None
        low = task.lower()
        app = self._detect_chat_app(low)
        if not app:
            return None
        idx = low.find(" ko ")
        if idx == -1:
            rec = self._extract_recipient_from_message(task)
            return (app, rec) if rec else None
        _APP = r"whats?\s*app|wa|teams?|slack|discord|telegram|tg|signal|messenger|skype"
        _PREP = r"pe|pa|par|pr|mein|mei|me|ma|mai|may|mn|men"
        contact = _re.sub(rf"(?i)\b({_APP}|{_PREP})\b", " ", task[:idx])
        contact = contact.strip(" ,.'\"")
        if not contact or len(contact) > 40:
            return None
        return (app, contact)

    async def _run_engine_task(
        self,
        task: str,
        attachments: list[str] | None = None,
        needs_confirm: bool = False,
        resume_transcript: list[dict] | None = None,
    ) -> dict:
        """Run (or queue/resume) a task on the universal engine.

        resume_transcript: continue a paused task after the user answered an
            engine clarifying question. `task` is then the user's answer.
        """
        if attachments and not resume_transcript:
            files = ", ".join(attachments)
            task = (
                f"{task}\n\nYEH EK FILE/DOCUMENT BHEJNI HAI (text message NAHI). "
                f"File path: {files}\n"
                f"File ko attach_file tool se compose box mein ATTACH karo "
                f"(file ka naam/path kabhi TYPE mat karo — woh galat hai). "
                f"Steps: contact kholo → attach_file {{file_path yeh path}} → "
                f"thoda ruko → Enter se bhejo → verify ke file chat mein nazar aaye."
            )

        if needs_confirm and not resume_transcript:
            token = f"confirm_{secrets.token_urlsafe(16)}"
            pending = {"action": "universal_task", "params": {"task": task}}
            async with self._pending_lock:
                self._pending_actions[token] = pending
            return {
                "reply": (
                    f"🤖 Yeh task engine se karunga:\n> {task[:200]}\n\n"
                    "➡️ **Karne ke liye:** `yes` ya `haan`\n"
                    "❌ **Cancel:** `no` ya `nahi`"
                ),
                "actions": [],
                "pending": [{"token": token, "action": pending}],
            }

        # Capture the user's window (dashboard) NOW — BEFORE any deterministic
        # path focuses an app — so cleanup (and the engine fallback) returns
        # here, not to the app it opened. (Bug: engine captured origin AFTER the
        # deterministic path focused Teams → it minimized the dashboard instead.)
        origin = None
        if not resume_transcript:
            from app.services.laptop_control.laptop_native import LaptopNative as _LN
            try:
                origin = await asyncio.to_thread(_LN.get().active_window_title)
            except Exception:
                origin = None

        # ── FAST DETERMINISTIC PATH (chat-app text + file sends) ──
        # Pure Python: focus → paste name → Enter → FOCUS compose → paste/attach
        # → Enter → verify. No slow LLM loop, no hanging ui_tree. Handles a
        # clean single send; anything it can't parse/confirm falls to the engine.
        if not resume_transcript:
            import re as _re
            # file path: from the dashboard attachment, or the "File path:" line
            # the augment step embedded into the task (attachments are dropped
            # across the confirm step, so we re-read it from the task string).
            file_path = attachments[0] if attachments else None
            if not file_path:
                m = _re.search(r"File path:\s*(.+)", task)
                if m:
                    file_path = m.group(1).splitlines()[0].strip()

            native = None
            from app.services.laptop_control.laptop_native import LaptopNative
            nat = LaptopNative.get()
            if file_path:
                pc = self._parse_chat_app_and_contact(task)
                if pc:
                    app, contact = pc
                    # WhatsApp = clipboard paste; Teams etc. = attach button +
                    # OS Open dialog (uia_type the path — vision can't pick files
                    # from a dialog reliably, so we do NOT route files to vision).
                    log.info("deterministic_file_send_try", app=app, contact=contact, file=file_path)
                    native = await asyncio.to_thread(nat.chat_send_file, app, contact, file_path, "")
            else:
                parsed = self._parse_chat_send(task)
                if parsed:
                    app, contact, message = parsed
                    log.info("deterministic_send_try", app=app, contact=contact, msg=message[:40])
                    native = await asyncio.to_thread(nat.chat_send, app, contact, message)

            if native and native.get("ok"):
                # minimize the app + return to the dashboard (every path)
                await asyncio.to_thread(self._post_send_cleanup, origin)
                return {
                    "reply": f"✅ {native.get('msg', f'{contact} ko bhej diya')}",
                    "actions": [{
                        "action": "chat_send_file" if file_path else "chat_send",
                        "status": "success", "message": native.get("msg", ""),
                    }],
                }
            if native:
                log.info("deterministic_send_fallback",
                         reason=(native.get("error") or native.get("msg", ""))[:120])
                # fall through to the engine (it can open the app, handle odd UIs)

        from app.services.universal_engine.engine import UniversalEngine
        result = await UniversalEngine.get().run(task, transcript=resume_transcript, origin=origin)

        # Engine needs more info — park the session and ask the user.
        if result.get("needs_input"):
            self._engine_session = {"transcript": result.get("transcript")}
            return {
                "reply": f"🤔 {result.get('question', 'Thodi aur detail chahiye, Boss.')}",
                "actions": [],
                "awaiting_input": True,
            }

        # ALL-ROUNDER fallback: if the engine couldn't finish via UIA/tools
        # (WebView/odd UI it couldn't reach), give it EYES — run the vision
        # loop on the same task. This is what lets JARVIS control *anything*:
        # fast deterministic → engine (UIA) → vision (sees the screen). Vision
        # has its own honest verify, so a real fail still reports honestly.
        if not result.get("ok") and not resume_transcript:
            log.info("engine_to_vision_fallback", task=task[:80])
            try:
                vres = await asyncio.to_thread(self._run_vision, task)
                if vres.get("ok"):
                    result = {"ok": True, "reply": vres.get("reply", ""),
                              "steps": result.get("steps", [])}
            except Exception as e:
                log.warning("vision_fallback_failed", error=str(e)[:120])

        icon = "✅" if result.get("ok") else "❌"
        return {
            "reply": f"{icon} {result.get('reply', '')}",
            "actions": [
                {
                    "action": "universal_task",
                    "status": "success" if result.get("ok") else "failed",
                    "message": result.get("reply", ""),
                    "steps": len(result.get("steps", [])),
                }
            ],
        }

    async def _confirm_and_run(self, pending: dict) -> dict:
        """User ne pending action confirm kiya — chala kar chat-reply do.

        Engine tasks (universal_task) ask/resume/steps support karte hain;
        baqi controller actions purane tareeqe se.
        """
        self._remember_approved_recipient(pending)
        if pending.get("action") == "universal_task":
            task = pending.get("params", {}).get("task", "")
            return await self._run_engine_task(task)  # full ask/result handling
        result = await self._laptop.execute(pending)
        return {
            "reply": f"✅ {result.get('message', 'Done')}",
            "actions": [result],
        }

    @staticmethod
    def _remember_approved_recipient(action: dict) -> None:
        """After a user confirms a send, remember the recipient so the next
        send to the same person doesn't prompt again. "Remember me" mode.
        Safe to call for non-send actions — it just no-ops.
        """
        from app.services.approved_recipients import ApprovedRecipients
        t = action.get("action", "")
        p = action.get("params", {})
        try:
            if t == "send_whatsapp_message":
                ApprovedRecipients.approve("whatsapp", p.get("recipient"))
            elif t == "send_teams_message":
                ApprovedRecipients.approve("teams", p.get("recipient"))
            elif t == "send_email":
                to = p.get("to") or p.get("recipient")
                primary = to if isinstance(to, str) else (to[0] if isinstance(to, list) and to else None)
                if primary:
                    ApprovedRecipients.approve("email", primary)
            elif t == "trello_create_card":
                ApprovedRecipients.approve("trello_list", p.get("list") or p.get("list_name") or "default")
            elif t == "trello_move_card":
                card = p.get("card") or p.get("title") or ""
                target = p.get("to_list") or p.get("list") or ""
                if card and target:
                    ApprovedRecipients.approve("trello_move", f"{card}->{target}")
            elif t == "trello_comment":
                card = p.get("card") or p.get("title") or ""
                if card:
                    ApprovedRecipients.approve("trello_comment", card)
        except Exception as e:
            log.debug("approve_failed", error=str(e))

    @staticmethod
    def _describe_action(action: dict) -> str:
        """Human-readable preview of an action.

        For messaging actions this is the user's last line of defense before
        a wrong message ships — keep it copy-paste-clear so the user can
        actually see who's getting what.
        """
        t = action.get("action", "")
        p = action.get("params", {})

        def _trim(s: str, n: int = 280) -> str:
            s = str(s or "")
            return s if len(s) <= n else s[: n - 1] + "…"

        if t == "send_whatsapp_message":
            recipient = p.get("recipient", "?")
            message = p.get("message") or "(text nahi diya)"
            attachment = p.get("attachment")
            lines = [
                "📱 **WhatsApp Send — Confirm karein:**",
                f"  • To: **{recipient}**",
                f"  • Message: {_trim(message)}",
            ]
            if attachment:
                lines.append(f"  • Attachment: {attachment}")
            return "\n".join(lines)

        if t == "send_teams_message":
            recipient = p.get("recipient", "?")
            message = p.get("message") or "(text nahi diya)"
            attachment = p.get("attachment")
            lines = [
                "💼 **Teams Send — Confirm karein:**",
                f"  • To: **{recipient}**",
                f"  • Message: {_trim(message)}",
            ]
            if attachment:
                lines.append(f"  • Attachment: {attachment}")
            return "\n".join(lines)

        if t == "send_email":
            to = p.get("to") or p.get("recipient") or "?"
            subject = p.get("subject", "") or "(no subject)"
            body = p.get("body") or p.get("message") or "(no body)"
            from_account = p.get("from") or p.get("from_account") or p.get("account")
            attachments = p.get("attachments") or p.get("attachment")
            lines = [
                "📧 **Email Send — Confirm karein:**",
                f"  • To: **{to}**",
            ]
            if from_account:
                lines.append(f"  • From: {from_account}")
            lines.append(f"  • Subject: {subject}")
            lines.append(f"  • Body: {_trim(body)}")
            if attachments:
                lines.append(f"  • Attachments: {attachments}")
            return "\n".join(lines)

        if t == "trello_create_card":
            return (
                "📋 **Trello — Card Banane se pehle confirm:**\n"
                f"  • Title: **{p.get('title') or p.get('name', '?')}**\n"
                f"  • List: {p.get('list') or p.get('list_name') or '(default)'}"
            )

        if t == "trello_move_card":
            return (
                "📋 **Trello — Card move se pehle confirm:**\n"
                f"  • Card: **{p.get('card') or p.get('title', '?')}**\n"
                f"  • To list: **{p.get('to_list') or p.get('list', '?')}**"
            )

        if t == "trello_comment":
            return (
                "📋 **Trello — Comment se pehle confirm:**\n"
                f"  • Card: **{p.get('card') or p.get('title', '?')}**\n"
                f"  • Comment: {_trim(p.get('comment') or p.get('text', ''))}"
            )

        # Existing destructive actions
        if t == "delete_file":
            return f"🗑️ **Delete File:**\n  • Path: {p.get('path', '?')}"
        if t == "move_file":
            return f"📦 **Move File:**\n  • From: {p.get('source', '?')}\n  • To: {p.get('dest', '?')}"
        if t == "create_file":
            return f"📝 **Create File:**\n  • Name: {p.get('name', '?')}\n  • Type: {p.get('type', 'txt')}"
        if t == "system_command":
            return f"⚙️ **System Command:**\n  • Cmd: {p.get('command', '?')}"

        return f"{t}: {p}"

    async def _execute_action(self, action: dict) -> dict:
        """Execute an action from the chat response."""
        action_type = action.get("action")

        if action_type == "create_task":
            async with async_session() as db:
                task = Task(
                    title=action.get("title", ""),
                    description=action.get("description", ""),
                    assigned_to=action.get("assigned_to"),
                    priority=action.get("priority", "medium"),
                    action_type=action.get("action_type"),
                    action_payload=json.dumps(action.get("action_payload")) if action.get("action_payload") else None,
                )
                db.add(task)
                await db.commit()
                return {"type": "task_created", "title": task.title, "id": task.id}

        elif action_type == "assign_task":
            task_id = action.get("task_id")
            assigned_to = action.get("assigned_to")
            if task_id and assigned_to:
                async with async_session() as db:
                    result = await db.execute(
                        select(Task).where(Task.id == int(task_id))
                    )
                    task = result.scalar_one_or_none()
                    if task:
                        task.assigned_to = assigned_to
                        task.status = "approved"
                        await db.commit()

                        # Auto notify — save notification for team member
                        notify_msg = (
                            f"📌 Naya task assign hua hai tujhe:\n"
                            f"  Task: {task.title}\n"
                            f"  Priority: {task.priority}\n"
                            f"  Description: {task.description or 'N/A'}\n\n"
                            f"Boss ne assign kiya hai — jaldi kar!"
                        )
                        await db.execute(
                            __import__('sqlalchemy').text(
                                "INSERT INTO notifications (assigned_to, task_id, message, status) VALUES (:to, :tid, :msg, 'pending')"
                            ),
                            {"to": assigned_to, "tid": task_id, "msg": notify_msg},
                        )
                        await db.commit()

                        return {"type": "task_assigned", "task_id": task_id, "assigned_to": assigned_to, "title": task.title, "notified": True}
            return {"type": "assign_failed", "error": "Task not found"}

        elif action_type == "create_client":
            async with async_session() as db:
                client = Client(
                    name=action.get("name", ""),
                    company=action.get("company"),
                    role=action.get("role"),
                    email=action.get("email"),
                    phone=action.get("phone"),
                    website=action.get("website"),
                )
                db.add(client)
                await db.commit()
                return {"type": "client_created", "name": client.name, "id": client.id}

        return {"type": "unknown", "action": action_type}
