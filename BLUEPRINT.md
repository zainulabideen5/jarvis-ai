# JARVIS — Project Blueprint

> "PC ka Siri" — ek AI jo poore laptop aur kisi bhi app ko insaan ki tarah
> control karta hai. Chatbox se type/bol kar koi bhi kaam — message bhejna,
> file bhejna, app kholna, system control — sab. Har laptop, har naam, har app
> ke liye (self-adaptive, kuch hardcoded nahi).

---

## 1. Teen hisse (high-level architecture)

```
┌─────────────────┐     HTTP/WS      ┌──────────────────────┐
│   DASHBOARD     │ <──────────────> │       SERVER         │
│  (React + Vite) │   localhost:3000 │   (FastAPI :8000)    │
│   chatbox + UI  │                  │   brain + control    │
└─────────────────┘                  └──────────┬───────────┘
                                                 │ controls the PC
                                     ┌───────────┴───────────┐
                                     │  Windows (UIA, real    │
                                     │  mouse/keyboard, vision)│
                                     │  WhatsApp/Teams/any app │
                                     └────────────────────────┘
        ┌──────────────────────┐  WebSocket
        │   DESKTOP AGENT      │ ───────────►  (audio/meeting capture →
        │  (Python, system     │                server transcribes)
        │  tray, audio, VAD)   │
        └──────────────────────┘
```

| Hissa | Folder | Kaam |
|---|---|---|
| **Server** | `server/` | Dimaag — brain (LLM), laptop control, chat, DB, APIs |
| **Dashboard** | `dashboard/` | React UI — chatbox, tasks, clients, meetings, settings |
| **Desktop Agent** | `desktop-agent/` | Audio/meeting capture, VAD, system tray, task executor |

---

## 2. CONTROL ka dil — 3-layer system (all-rounder)

Har "kaam karo" command in 3 layers se guzarta hai — har layer pichli ki kami
pakadta hai, isliye **kuch ruakta nahi**:

```
User: "Zaid ko whatsapp pe hello bhej"
   │
   ▼
1. FAST DETERMINISTIC  ── pure Python (UIA + keyboard), no LLM
   search → naam → Enter → compose focus → type → Enter → verify
   ⚡ instant, free | known chat apps (WhatsApp fast; Teams search = vision)
   │ (na ho / parse na ho to)
   ▼
2. UNIVERSAL ENGINE  ── LLM brain (Claude CLI) + tools, think-act-observe loop
   ui_tree padho → tool chalao → verify → repeat | + recipe (2nd time fast)
   │ (na ho to)
   ▼
3. VISION  ── screen DEKH ke insaan ki tarah (screenshot → AI → click/type)
   🎯 KOI bhi app/web, koi version/language | + recipe (learn→replay)
```

- **Layer 1 (deterministic):** `laptop_native.chat_send()` / `chat_send_file()` —
  fixed reliable steps. Message parse: `chat.py::_parse_chat_send` (regex) →
  `_llm_extract_send` (LLM samajhta hai any phrasing).
- **Layer 2 (engine):** `universal_engine/engine.py` — brain plans, tools execute.
  Brain = `brain.py` (Claude CLI). Recipes = `recipes.py` (learn step sequence).
- **Layer 3 (vision):** `vision_control.py` — Set-of-Marks (UIA positions + grid)
  + Claude CLI vision. `click_target()` = "jo cheez X hai usko dekh ke click".

**Yehi "kuch bhi control kar sakta hai" banata hai** — UIA fail ho to aankhें (vision).

---

## 3. Server breakdown (`server/app/`)

### Entry + infra
- `main.py` — FastAPI app, lifespan (background loop: daily report), CORS
- `api/routes.py` — saare REST endpoints (chat, tasks, clients, rules, vision, stop, …)
- `ws/handler.py` — agent WebSocket (audio chunks receive)
- `core/config.py` — env/keys, self-adaptive paths
- `core/llm.py` — LLM client: **Groq → Cerebras → OpenRouter → Gemini** fallback (multi-key)
- `core/database.py` — async SQLite (`server/data/jarvis.db`)

### Chat + control brain
- `services/chat.py` — **central router**: stop-check → vision-request → engine-first →
  deterministic send → conversational LLM. Message/file parsing.
- `services/universal_engine/` — `engine.py` (loop), `brain.py` (Claude CLI),
  `tools.py` (UIA/keyboard/PowerShell tools), `recipes.py` (learn→replay)
- `services/laptop_control/` — the hands:
  - `laptop_native.py` — UIA + pyautogui + clipboard (chat_send, chat_send_file, move_window, minimize…)
  - `vision_control.py` — vision loop + click_target + recipe
  - `controller.py`, `intent.py`, `apps.py`, `files.py`, `system.py`, `office_com.py`,
    `email_sender.py` (Gmail SMTP), `security.py` (AV-safe guards)
- `services/task_control.py` — global STOP flag

### Meetings / audio / tasks
- `services/processor.py` — audio chunk → transcription pipeline
- `services/transcription.py`, `diarization.py` — Whisper + speaker labels
- `services/meeting_service.py`, `task_extractor.py`, `daily_report.py`
- `services/rules_engine.py`, `smart_notify.py`, `verification.py`, `memory.py`

### Models (DB tables)
`agent, chunk, client, rule, task, transcription`

---

## 4. Dashboard (`dashboard/src/`)
- `App.jsx` — shell + global polling
- `pages/ChatPage.jsx` — **main chatbox** (send, Stop button, attachments, voice mic, live progress)
- `pages/` — Dashboard, Tasks, Clients, Meetings, Calendar, Transcripts, Rules, Agents, Stats, Settings
- `api.js` — backend API wrapper · `hooks/usePolling.js` · `components/`

---

## 5. Desktop Agent (`desktop-agent/src/jarvis_agent/`)
- `core/orchestrator.py` — main loop (TaskGroup)
- `audio/` — microphone + loopback + mixer + chunker
- `vad/silero.py` — voice activity detection (speech hi bheje)
- `transport/ws_client.py` + `offline_queue.py` — server connection + offline buffer
- `executor/` — task listener + drivers (vision, reminder) + router
- `ui/tray.py` + `approval.py` — system tray + approval prompts

---

## 6. Tech stack
- **Backend:** Python, FastAPI, SQLAlchemy (async SQLite), uvicorn
- **Brain (LLM):** Claude CLI (Opus) primary; fallback Groq/Cerebras/OpenRouter/Gemini
- **Control:** pywinauto (UIA), pyautogui (mouse/kbd), win32 (clipboard/windows), mss (screenshot), PIL
- **Vision:** Claude CLI vision (screenshot → action), Groq llama-4-scout (alt)
- **Frontend:** React + Vite + Tailwind
- **Agent:** Python (audio capture, silero VAD, websockets, system tray)

---

## 7. Capabilities scorecard (honest)

| Capability | Status |
|---|---|
| System control (app/file/window/PowerShell) | ✅ Pakka (native) |
| File search | ✅ Real search |
| WhatsApp message | ✅ Deterministic, ~3s |
| WhatsApp document/file | ✅ Confirmed (paste) |
| Teams message | ✅ Vision search + send |
| Teams document/file | ⚠️ Paste-first then dialog (testing) |
| Gmail email + attachment | ✅ SMTP backend |
| Any app/web "dekh ke" | 🎯 Vision (universal, slower) |
| Vision learn→replay (2nd time fast) | ✅ Recipe |
| STOP mid-task | ✅ Chat + button |
| Auto-minimize + dashboard return | ✅ All apps |
| Honest verify (no false success) | ✅ Throughout |
| Meeting record/transcribe/tasks | ✅ Base (agent + server) |

### Pending / weak spots
- Teams file 100% reliable (paste-first should help)
- Slack/Discord/Telegram — code-ready, untested
- **WhatsApp Web (fast + true background)** — biggest pending; official app can't be hidden
- Vision speed (~10s/step via CLI) + occasional misclick (retries)
- Installer (self-adaptive packaging — "sab ke liye" goal)
- Voice (Mic button integration)

---

## 8. Core design principles (NON-negotiable)
1. **Universal / self-adaptive** — koi hardcoded naam/path/contact nahi. Har laptop,
   har naam, har app khud detect. (Vision = ultimate universal layer.)
2. **Never lie** — har send VERIFY hota hai; pakka na ho to honestly "nahi gaya" bolega.
3. **AV-safe** — koi PowerShell SendKeys/Forms nahi (malware-shaped). UIA + real input only.
4. **CLI brain** — Claude CLI primary (Zain ki pasand); privacy ke liye vision bhi CLI.
5. **3-layer fallback** — fast → smart → eyes. Kuch ruakta nahi.

---

## 9. Run / start
```
d:\jarvis\start-jarvis.ps1            # sab start
d:\jarvis\start-jarvis.ps1 -Force     # restart
```
Server :8000 · Dashboard :3000 · Agent (no port). Open http://localhost:3000

---
*Blueprint generated 2026-06-16. Code is the source of truth — verify before relying on any file:line.*
