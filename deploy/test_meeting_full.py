"""PROPER end-to-end test — exercises the REAL window loop (short window),
mini-summaries, task dedup, and final consolidation.
"""
import asyncio
from sqlalchemy import text

from app.core.config import ServerConfig
from app.core.database import async_session
from app.services.meeting_service import MeetingService


async def insert_transcripts(mid, lines, tag):
    async with async_session() as db:
        for i, t in enumerate(lines):
            await db.execute(
                text("INSERT INTO transcriptions (chunk_id, text, language) VALUES (:c, :t, 'en')"),
                {"c": f"test-{mid}-{tag}-{i}", "t": t},
            )
        await db.commit()


async def main():
    svc = MeetingService(ServerConfig())
    svc.WINDOW_SEC = 6  # short window so the loop fires fast in the test

    print("[1] Starting meeting (real window loop launching)...")
    started = await svc.start_meeting("TEST FULL — windowed live")
    mid = started["id"]

    # WINDOW 1 speech
    await insert_transcripts(mid, [
        "Boss bole Ahmed ko kal tak company ka logo design karke dena hai urgent.",
        "Client ne kaha Sara ke saath Monday ko meeting rakho.",
    ], "w1")
    print("[2] Window-1 speech inserted, waiting for loop to fire (~7s)...")
    await asyncio.sleep(7)
    print(f"    held tasks: {len(svc._window_tasks.get(mid, []))} | mini-summaries: {len(svc._window_summaries.get(mid, []))}")

    # WINDOW 2 speech (Ahmed-logo repeated = duplicate + new Hamza task)
    await insert_transcripts(mid, [
        "Woh Ahmed wala logo ka kaam yaad hai na, kal tak chahiye.",
        "Aur Hamza is hafte client ke liye invoice tayyar kare.",
    ], "w2")
    print("[3] Window-2 speech inserted (Ahmed-logo dup), waiting (~7s)...")
    await asyncio.sleep(7)
    print(f"    held tasks: {len(svc._window_tasks.get(mid, []))} | mini-summaries: {len(svc._window_summaries.get(mid, []))}")

    print("[4] Ending meeting (consolidate tasks + summary from minis)...")
    result = await svc.end_meeting()

    print()
    print("=== RESULT ===")
    print(f"Duration: {result.get('duration_minutes')} min")
    tasks = result.get("meeting_tasks", [])
    print(f"FINAL TASKS: {len(tasks)}")
    for i, t in enumerate(tasks, 1):
        print(f"  {i}. {t.get('title')} -> {t.get('assigned_to')} [{t.get('priority')}]")
    print()
    print("SUMMARY:")
    print(result.get("summary", "")[:600])
    print()
    print("EXPECTED: ~3 tasks (Ahmed-logo ONCE, Sara, Hamza) + a real summary")

    # cleanup
    async with async_session() as db:
        await db.execute(text("DELETE FROM transcriptions WHERE chunk_id LIKE :p"), {"p": f"test-{mid}-%"})
        await db.execute(text("DELETE FROM meetings WHERE id = :id"), {"id": mid})
        await db.execute(text("DELETE FROM tasks WHERE chunk_id IS NULL AND datetime(created_at) >= datetime('now','-5 minutes')"))
        await db.commit()
    print("\n[cleanup] test data removed")


asyncio.run(main())
