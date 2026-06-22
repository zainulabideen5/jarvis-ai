"""Multi-agent task system — JARVIS har bada/independent command ko ek ALAG
background AGENT (worker) ko de kar PARALLEL chalata hai. User hukum deta ja
(chat ab, voice baad mein) → har kaam ek agent → sab saath chalein → dashboard
mein live status (queued/running/done/failed) → har complete pe pata chale.

Self-adaptive, koi hardcode nahi. Status SQLite (agent_jobs) mein persist hota
hai (restart pe bhi rahe). Naam 'agent_jobs' — desktop-clients wale 'agents'
(WebSocket) se ALAG, taake takraav na ho.

Concurrency: har agent FRESH ChatService use karta hai (singleton session-state
collision nahi). WhatsApp/web ka kaam WebBrowser ke single worker-thread se
khud-ba-khud serialize ho jata hai (ek hi browser) — baqi kaam (file/research)
sach-much parallel.
"""
from __future__ import annotations

import asyncio
import secrets

from sqlalchemy import text as sa_text

from app.core.database import async_session
from app.core.logging import get_logger

log = get_logger(__name__)

_INIT_DONE = False

_COLS = ["job_id", "num", "task", "status", "result", "error",
         "created_at", "started_at", "completed_at"]


async def _ensure_table() -> None:
    """agent_jobs table pehli dafa khud bana do (har machine pe self-setup)."""
    global _INIT_DONE
    if _INIT_DONE:
        return
    async with async_session() as db:
        await db.execute(sa_text(
            "CREATE TABLE IF NOT EXISTS agent_jobs ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " job_id TEXT UNIQUE NOT NULL,"
            " num INTEGER,"
            " task TEXT NOT NULL,"
            " status TEXT NOT NULL DEFAULT 'queued',"
            " result TEXT,"
            " error TEXT,"
            " created_at TEXT DEFAULT (datetime('now')),"
            " started_at TEXT,"
            " completed_at TEXT)"
        ))
        await db.commit()
    _INIT_DONE = True


class AgentManager:
    """Background task-agents ka manager. Singleton. General — koi bhi task."""

    _inst: "AgentManager | None" = None

    # Claude CLI bhaari subprocess hai — ek saath 2 se zyada calls atak jaate hain
    # (test: 2 chale, 3 stuck). Isliye ek waqt mein itne hi agents Claude par
    # CHALEIN; baaki 'queued' rahein aur slot khali hote hi chalein. Groq pe NAHI
    # jaate (Claude hi brain) — bas concurrency control. 7-8 kaam = sab honge, 2-2.
    MAX_PARALLEL = 2

    def __init__(self) -> None:
        self._jobs: dict[str, dict] = {}        # job_id -> live state
        self._tasks: dict[str, asyncio.Task] = {}
        self._counter = 0
        self._sem = asyncio.Semaphore(self.MAX_PARALLEL)

    @classmethod
    def get(cls) -> "AgentManager":
        if cls._inst is None:
            cls._inst = cls()
        return cls._inst

    # ---------------- spawn ----------------
    async def spawn(self, task: str, executor=None) -> dict:
        """Naya agent banao jo `task` ko BACKGROUND mein KHUD complete kare.
        Foran job dict return (block NAHI). executor: optional async fn(task)->dict;
        default = autonomous ChatService run."""
        task = (task or "").strip()
        if not task:
            return {"ok": False, "error": "khali task — kuch nahi banaya"}
        await _ensure_table()
        if self._counter == 0:      # restart ke baad DB se continue (num na takraye)
            async with async_session() as db:
                r = await db.execute(sa_text("SELECT COALESCE(MAX(num),0) FROM agent_jobs"))
                self._counter = int(r.scalar() or 0)
        self._counter += 1
        num = self._counter
        job_id = f"A{num}-{secrets.token_hex(3)}"
        job = {"job_id": job_id, "num": num, "task": task, "status": "queued",
               "result": None, "error": None}
        self._jobs[job_id] = job
        async with async_session() as db:
            await db.execute(sa_text(
                "INSERT INTO agent_jobs (job_id, num, task, status) "
                "VALUES (:j,:n,:t,'queued')"),
                {"j": job_id, "n": num, "t": task})
            await db.commit()
        self._tasks[job_id] = asyncio.create_task(self._run(job_id, task, executor))
        log.info("agent_spawned", job_id=job_id, num=num, task=task[:60])
        return {"ok": True, "job_id": job_id, "num": num, "task": task}

    async def _run(self, job_id: str, task: str, executor) -> None:
        # Slot ka intezar (status 'queued' rahega jab tak slot na mile). Isse
        # Claude CLI overwhelm nahi hota — 7-8 agents ho to 2-2 chalte rahenge.
        async with self._sem:
            await self._update(job_id, status="running", started=True)
            await self._execute(job_id, task, executor)

    async def _execute(self, job_id: str, task: str, executor) -> None:
        try:
            run = executor or self._default_executor
            result = await run(task)
            if isinstance(result, dict):
                ok = bool(result.get("ok", True))
                msg = (result.get("reply") or result.get("msg")
                       or result.get("error") or "ho gaya")
            else:
                ok, msg = True, str(result)
            if ok:
                await self._update(job_id, status="completed",
                                   result=str(msg)[:1000], done=True)
                log.info("agent_completed", job_id=job_id)
            else:
                await self._update(job_id, status="failed",
                                   error=str(msg)[:600], done=True)
                log.info("agent_failed_soft", job_id=job_id)
        except Exception as e:
            log.warning("agent_exception", job_id=job_id, error=str(e)[:200])
            await self._update(job_id, status="failed", error=str(e)[:600], done=True)

    async def _default_executor(self, task: str) -> dict:
        """FRESH ChatService se task autonomous chalao. Confirm-gate (jaise
        WhatsApp send) khud confirm kar do (user ne autonomous kaha) — payment/
        privacy waale to chat-flow khud rok deta hai."""
        from app.core.config import ServerConfig
        from app.services.chat import ChatService
        svc = ChatService(ServerConfig())
        # allow_spawn=False — agent ke andar dobara agent na bane (no recursion).
        res = await svc.chat(task, allow_spawn=False)
        # pending confirm aaya to ek baar auto-confirm (autonomous).
        if isinstance(res, dict) and res.get("pending"):
            tok = (res["pending"][0] or {}).get("token")
            if tok:
                res = await svc.chat("yes", confirm_token=tok, allow_spawn=False)
        return res

    # ---------------- status updates ----------------
    async def _update(self, job_id: str, status: str | None = None,
                      result: str | None = None, error: str | None = None,
                      started: bool = False, done: bool = False) -> None:
        job = self._jobs.get(job_id)
        if job:
            if status:
                job["status"] = status
            if result is not None:
                job["result"] = result
            if error is not None:
                job["error"] = error
        sets, params = [], {"j": job_id}
        if status:
            sets.append("status=:s"); params["s"] = status
        if result is not None:
            sets.append("result=:r"); params["r"] = result
        if error is not None:
            sets.append("error=:e"); params["e"] = error
        if started:
            sets.append("started_at=datetime('now')")
        if done:
            sets.append("completed_at=datetime('now')")
        if not sets:
            return
        await _ensure_table()
        async with async_session() as db:
            await db.execute(sa_text(
                f"UPDATE agent_jobs SET {','.join(sets)} WHERE job_id=:j"), params)
            await db.commit()

    @staticmethod
    async def cleanup_stale() -> None:
        """Restart ke baad jo agents 'running'/'queued' reh gaye (process mar gaya
        tha) unhe 'failed' mark karo — warna hamesha-running dikhte rahenge."""
        await _ensure_table()
        async with async_session() as db:
            await db.execute(sa_text(
                "UPDATE agent_jobs SET status='failed',"
                " error='server restart — adhoora reh gaya',"
                " completed_at=datetime('now')"
                " WHERE status IN ('running','queued')"))
            await db.commit()

    async def delete_job(self, job_id: str) -> dict:
        """User ne ✕ dabaya — yeh agent/task panel se HATAO (sirf manual, auto nahi).
        Agar chal raha hai to cancel bhi karo."""
        t = self._tasks.pop(job_id, None)
        if t is not None and not t.done():
            try:
                t.cancel()
            except Exception:
                pass
        self._jobs.pop(job_id, None)
        await _ensure_table()
        async with async_session() as db:
            r = await db.execute(sa_text("DELETE FROM agent_jobs WHERE job_id=:j"),
                                 {"j": job_id})
            await db.commit()
        return {"ok": True, "removed": (r.rowcount or 0)}

    # ---------------- read ----------------
    async def list_jobs(self, limit: int = 50) -> list[dict]:
        await _ensure_table()
        async with async_session() as db:
            r = await db.execute(sa_text(
                "SELECT job_id, num, task, status, result, error, created_at,"
                " started_at, completed_at FROM agent_jobs"
                " ORDER BY id DESC LIMIT :l"), {"l": int(limit)})
            return [dict(zip(_COLS, row)) for row in r.fetchall()]

    async def get_job(self, job_id: str) -> dict | None:
        await _ensure_table()
        async with async_session() as db:
            r = await db.execute(sa_text(
                "SELECT job_id, num, task, status, result, error, created_at,"
                " started_at, completed_at FROM agent_jobs WHERE job_id=:j"),
                {"j": job_id})
            row = r.fetchone()
            return dict(zip(_COLS, row)) if row else None
