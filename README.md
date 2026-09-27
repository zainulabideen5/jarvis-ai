# Jarvis AI

A desktop assistant for Windows that controls the whole PC the way a person would. You type or speak a request in the chat, and Jarvis opens apps, sends WhatsApp and Teams messages, attaches files, sends email, records meetings and turns them into tasks.

Nothing is hardcoded to one machine or one app. When the fast path does not work, Jarvis falls back to reading the screen and acting on what it sees.

## How it works

Every command goes through three layers. Each one catches what the previous one missed.

| Layer | What it does | Speed |
|---|---|---|
| 1. Deterministic | Plain Python using Windows UI Automation and the keyboard. Search, pick the contact, type, send, verify. | Instant, no AI call |
| 2. Universal engine | An LLM plans the steps, runs tools, checks the result and repeats. Successful runs are saved as recipes so the next time is fast. | Seconds |
| 3. Vision | Takes a screenshot, finds the target on screen and clicks or types like a human. Works in any app, in any language. | Slower, works anywhere |

## Features

- **PC control:** open and close apps, manage windows, search files, run PowerShell
- **Messaging:** WhatsApp messages in about 3 seconds, files and documents, Teams messages
- **Email:** Gmail with attachments over SMTP
- **Meetings:** records mic and system audio, transcribes with Whisper, labels speakers, pulls out tasks
- **Daily reports** and rule based notifications
- **Integrations:** Telegram, Slack, Discord and Notion
- **Safety:** stop any task mid-way from the chat or a button, and every action is verified before it is reported as done
- **Learns:** a task done through vision once is replayed quickly the next time

## Architecture

```
Dashboard (React, :3000)  <-- HTTP / WebSocket -->  Server (FastAPI, :8000)
                                                         |
                                                         v
                                          Windows: UI Automation, mouse,
                                          keyboard, screenshots, any app

Desktop Agent (Python, system tray) -- WebSocket --> Server
  captures audio, detects speech, runs tasks
```

| Part | Folder | Role |
|---|---|---|
| Server | `server/` | The brain: LLM routing, PC control, chat, database, REST and WebSocket APIs |
| Dashboard | `dashboard/` | Chat, tasks, clients, meetings, calendar, transcripts, rules, stats, settings |
| Desktop Agent | `desktop-agent/` | Microphone and loopback capture, voice activity detection, offline queue, tray icon, task executor |
| Deploy | `deploy/` | Whisper transcription service for a Linux server |

## Tech stack

- **Backend:** Python, FastAPI, SQLAlchemy with async SQLite, Uvicorn, structlog
- **AI:** Claude for planning and vision, with Groq, Cerebras, OpenRouter and Gemini as fallbacks
- **Speech:** faster-whisper, speaker diarization, Silero VAD
- **Control:** pywinauto (UI Automation), pyautogui, pywin32, mss, Pillow, Playwright
- **Frontend:** React, Vite, Tailwind CSS
- **Agent:** PyAudioWPatch, websockets, pystray

## Getting started

Requirements: Windows 10 or 11, Python 3.10+, Node.js 20+.

```powershell
# Server
cd server
python -m venv venv
venv\Scripts\pip install -r requirements.txt
copy .env.example .env        # add your API keys

# Dashboard
cd ..\dashboard
npm install

# Desktop agent
cd ..\desktop-agent
python -m venv venv
venv\Scripts\pip install -r requirements.txt
copy .env.example .env
```

Start everything with:

```powershell
.\start-jarvis.ps1
```

This starts the server on port 8000, the dashboard on port 3000 and the desktop agent. `stop-jarvis.ps1` shuts them down.

## Configuration

`server/.env`

| Variable | Purpose |
|---|---|
| `JARVIS_GROQ_API_KEY` | Groq key for the fallback LLM |
| `JARVIS_WHISPER_MODEL` | Whisper model size |
| `JARVIS_DIARIZATION_ENABLED` | Turn speaker labels on or off |
| `JARVIS_HF_TOKEN` | Hugging Face token for the diarization model |

`desktop-agent/.env` holds the server URLs and optional Gmail, Telegram, Slack, Discord and Notion credentials. See the `.env.example` files for the full list.

## Status

| Capability | Status |
|---|---|
| System control (apps, files, windows, PowerShell) | Working |
| WhatsApp messages and files | Working |
| Teams messages | Working |
| Teams files | In testing |
| Gmail with attachments | Working |
| Meeting recording, transcription and tasks | Working |
| Stop mid-task | Working |

A detailed design document is in [BLUEPRINT.md](BLUEPRINT.md).

---

Built by [Zain Ul Abideen](https://github.com/zainulabideen5)
