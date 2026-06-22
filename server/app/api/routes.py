"""REST API endpoints for the dashboard."""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import ServerConfig
from app.core.database import get_db
from app.models.agent import Agent
from app.models.chunk import AudioChunk
from app.models.client import Client
from app.models.rule import Rule
from app.models.task import Task
from app.models.transcription import Transcription

router = APIRouter(prefix="/api", tags=["api"])

# Chat service (singleton)
_chat_service = None

def _get_chat():
    global _chat_service
    if _chat_service is None:
        from app.services.chat import ChatService
        _chat_service = ChatService(ServerConfig())
    return _chat_service


@router.get("/health")
async def health():
    return {"status": "ok", "service": "jarvis-server"}


# ===== System diagnostic (Phase 9) — full end-to-end health check =====

@router.get("/diagnostics/full")
async def diagnostics_full():
    """Run full 10-layer health check. Used for first-run + multi-laptop testing.
    Each layer: Python deps, native infra, Office COM, Multi-Gmail, UIA,
    OpenCV, server routes, WA Playwright, system env, LLM keys.
    """
    from app.services.laptop_control.system_diagnostic import SystemDiagnostic
    import asyncio
    return await asyncio.to_thread(SystemDiagnostic.get().run_full_check)


# ===== Image-match fallback (Phase 5 — OpenCV template matching) =====
# For apps where UIA fails (Photoshop, Premiere, custom UIs).
# 100% local — no LLM, no screenshots leave laptop.

@router.get("/imgmatch/templates")
async def imgmatch_templates():
    """List all saved templates organized by app."""
    from app.services.laptop_control.image_match import ImageMatcher
    return ImageMatcher.get().list_templates()


@router.post("/imgmatch/capture")
async def imgmatch_capture(payload: dict):
    """Capture a screen region as a template.
    {save_as: 'photoshop/new_layer', x, y, width, height}"""
    from app.services.laptop_control.image_match import ImageMatcher
    import asyncio
    p = payload or {}
    return await asyncio.to_thread(
        ImageMatcher.get().capture_region,
        p.get("save_as", ""),
        int(p.get("x", 0)),
        int(p.get("y", 0)),
        int(p.get("width", 0)),
        int(p.get("height", 0)),
    )


@router.post("/imgmatch/delete")
async def imgmatch_delete(payload: dict):
    """Delete a saved template. {key}"""
    from app.services.laptop_control.image_match import ImageMatcher
    p = payload or {}
    return ImageMatcher.get().delete_template(p.get("key", ""))


@router.post("/imgmatch/find")
async def imgmatch_find(payload: dict):
    """Find a template on screen. {key, confidence?}"""
    from app.services.laptop_control.image_match import ImageMatcher
    import asyncio
    p = payload or {}
    return await asyncio.to_thread(
        ImageMatcher.get().find_template,
        p.get("key", ""),
        float(p.get("confidence", 0.85)),
    )


@router.post("/imgmatch/click")
async def imgmatch_click(payload: dict):
    """Find a template + click it. {key, confidence?, button?, clicks?}"""
    from app.services.laptop_control.image_match import ImageMatcher
    import asyncio
    p = payload or {}
    return await asyncio.to_thread(
        ImageMatcher.get().click_template,
        p.get("key", ""),
        float(p.get("confidence", 0.85)),
        p.get("button", "left"),
        int(p.get("clicks", 1)),
    )


@router.post("/imgmatch/wait")
async def imgmatch_wait(payload: dict):
    """Wait for a template to appear on screen.
    {key, timeout_sec?, confidence?}"""
    from app.services.laptop_control.image_match import ImageMatcher
    import asyncio
    p = payload or {}
    return await asyncio.to_thread(
        ImageMatcher.get().wait_for_template,
        p.get("key", ""),
        float(p.get("timeout_sec", 10.0)),
        float(p.get("confidence", 0.85)),
        float(p.get("poll_interval", 0.5)),
    )


@router.post("/imgmatch/find-all")
async def imgmatch_find_all(payload: dict):
    """Find ALL occurrences of a template. {key, confidence?, max_results?}"""
    from app.services.laptop_control.image_match import ImageMatcher
    import asyncio
    p = payload or {}
    return await asyncio.to_thread(
        ImageMatcher.get().find_all_templates,
        p.get("key", ""),
        float(p.get("confidence", 0.85)),
        int(p.get("max_results", 10)),
    )


# ===== Native Windows apps via UIA (Phase 4) =====
# Specialized drivers for Notepad/Calculator/File Explorer/Settings + generic workflow

@router.post("/winapps/notepad/save")
async def winapps_notepad_save(payload: dict):
    """Open Notepad, type content, save to path. {content, save_path}"""
    from app.services.laptop_control.win_apps_uia import WinAppsUIA
    import asyncio
    p = payload or {}
    return await asyncio.to_thread(
        WinAppsUIA.get().notepad_write_and_save,
        p.get("content", ""),
        p.get("save_path", ""),
    )


@router.post("/winapps/notepad/read")
async def winapps_notepad_read(payload: dict):
    """Read a text file via Python (no Notepad needed). {file_path}"""
    from app.services.laptop_control.win_apps_uia import WinAppsUIA
    import asyncio
    p = payload or {}
    return await asyncio.to_thread(
        WinAppsUIA.get().notepad_read, p.get("file_path", ""),
    )


@router.post("/winapps/calculator")
async def winapps_calculator(payload: dict):
    """Compute expression via Windows Calculator. {expression: '15+7*3'}"""
    from app.services.laptop_control.win_apps_uia import WinAppsUIA
    import asyncio
    p = payload or {}
    return await asyncio.to_thread(
        WinAppsUIA.get().calculator_compute, p.get("expression", ""),
    )


@router.post("/winapps/explorer/open")
async def winapps_explorer_open(payload: dict):
    """Open File Explorer at a path. {path?}"""
    from app.services.laptop_control.win_apps_uia import WinAppsUIA
    import asyncio
    p = payload or {}
    return await asyncio.to_thread(
        WinAppsUIA.get().explorer_open, p.get("path", ""),
    )


@router.post("/winapps/explorer/search")
async def winapps_explorer_search(payload: dict):
    """Search inside a folder via File Explorer. {folder, query}"""
    from app.services.laptop_control.win_apps_uia import WinAppsUIA
    import asyncio
    p = payload or {}
    return await asyncio.to_thread(
        WinAppsUIA.get().explorer_search,
        p.get("folder", ""), p.get("query", ""),
    )


@router.post("/winapps/settings/open")
async def winapps_settings_open(payload: dict):
    """Open Windows Settings. {panel?} — wifi/bluetooth/display/sound/etc."""
    from app.services.laptop_control.win_apps_uia import WinAppsUIA
    import asyncio
    p = payload or {}
    return await asyncio.to_thread(
        WinAppsUIA.get().settings_open, p.get("panel", ""),
    )


@router.post("/winapps/workflow")
async def winapps_workflow(payload: dict):
    """Generic open → type → save workflow for any app.
    {app_command, expected_title, text_to_type, save_path?, save_hotkey?}"""
    from app.services.laptop_control.win_apps_uia import WinAppsUIA
    import asyncio
    p = payload or {}
    return await asyncio.to_thread(
        WinAppsUIA.get().workflow_open_type_save,
        p.get("app_command", ""),
        p.get("expected_title", ""),
        p.get("text_to_type", ""),
        p.get("save_path", ""),
        tuple(p.get("save_hotkey") or ("ctrl", "s")),
    )


@router.post("/winapps/inspect")
async def winapps_inspect(payload: dict):
    """Enumerate buttons/menus/edits in any window — debug helper.
    {window_title}"""
    from app.services.laptop_control.win_apps_uia import WinAppsUIA
    import asyncio
    p = payload or {}
    return await asyncio.to_thread(
        WinAppsUIA.get().list_window_buttons, p.get("window_title", ""),
    )


# ===== Office COM (Phase 2 — Excel/Word/Outlook SILENT) =====
# All operations background — Visible=False — no window flashes.
# Requires Office installed locally.

@router.get("/office/check")
async def office_check():
    """Check which Office apps (Excel/Word/Outlook) are installed + COM-accessible."""
    from app.services.laptop_control.office_com import OfficeCOM
    import asyncio
    return await asyncio.to_thread(OfficeCOM.get().check_office_installed)


@router.post("/office/excel/read")
async def office_excel_read(payload: dict):
    """Read cells from Excel. {file_path, sheet?, cell_range?}"""
    from app.services.laptop_control.office_com import OfficeCOM
    import asyncio
    p = payload or {}
    return await asyncio.to_thread(
        OfficeCOM.get().excel_read_range,
        p.get("file_path", ""),
        p.get("sheet", ""),
        p.get("cell_range", "A1:Z100"),
    )


@router.post("/office/excel/write")
async def office_excel_write(payload: dict):
    """Write cells to Excel. {file_path, sheet, updates: [{cell, value}, ...]}"""
    from app.services.laptop_control.office_com import OfficeCOM
    import asyncio
    p = payload or {}
    return await asyncio.to_thread(
        OfficeCOM.get().excel_write_cells,
        p.get("file_path", ""),
        p.get("sheet", ""),
        p.get("updates", []),
    )


@router.post("/office/excel/formula")
async def office_excel_formula(payload: dict):
    """Run a formula in a cell + return result.
    {file_path, sheet, target_cell, formula}"""
    from app.services.laptop_control.office_com import OfficeCOM
    import asyncio
    p = payload or {}
    return await asyncio.to_thread(
        OfficeCOM.get().excel_run_formula,
        p.get("file_path", ""),
        p.get("sheet", ""),
        p.get("target_cell", ""),
        p.get("formula", ""),
    )


@router.post("/office/excel/create")
async def office_excel_create(payload: dict):
    """Create new Excel file with optional headers + rows.
    {file_path, headers?, rows?}"""
    from app.services.laptop_control.office_com import OfficeCOM
    import asyncio
    p = payload or {}
    return await asyncio.to_thread(
        OfficeCOM.get().excel_create_new,
        p.get("file_path", ""),
        p.get("headers"),
        p.get("rows"),
    )


@router.post("/office/excel/append")
async def office_excel_append(payload: dict):
    """Append a row at the bottom of used range. {file_path, sheet, row}"""
    from app.services.laptop_control.office_com import OfficeCOM
    import asyncio
    p = payload or {}
    return await asyncio.to_thread(
        OfficeCOM.get().excel_append_row,
        p.get("file_path", ""),
        p.get("sheet", ""),
        p.get("row", []),
    )


@router.post("/office/word/create")
async def office_word_create(payload: dict):
    """Create Word document. {file_path, content, title?}"""
    from app.services.laptop_control.office_com import OfficeCOM
    import asyncio
    p = payload or {}
    return await asyncio.to_thread(
        OfficeCOM.get().word_create,
        p.get("file_path", ""),
        p.get("content", ""),
        p.get("title", ""),
    )


@router.post("/office/word/append")
async def office_word_append(payload: dict):
    """Append text to existing Word doc. {file_path, text}"""
    from app.services.laptop_control.office_com import OfficeCOM
    import asyncio
    p = payload or {}
    return await asyncio.to_thread(
        OfficeCOM.get().word_append,
        p.get("file_path", ""),
        p.get("text", ""),
    )


@router.post("/office/word/read")
async def office_word_read(payload: dict):
    """Read full Word doc text. {file_path}"""
    from app.services.laptop_control.office_com import OfficeCOM
    import asyncio
    p = payload or {}
    return await asyncio.to_thread(
        OfficeCOM.get().word_read,
        p.get("file_path", ""),
    )


@router.post("/office/outlook/send")
async def office_outlook_send(payload: dict):
    """Send email via local Outlook. {to, subject, body, cc?, bcc?, attachments?, html_body?}"""
    from app.services.laptop_control.office_com import OfficeCOM
    import asyncio
    p = payload or {}
    return await asyncio.to_thread(
        OfficeCOM.get().outlook_send_email,
        p.get("to", ""),
        p.get("subject", ""),
        p.get("body", ""),
        p.get("cc", ""),
        p.get("bcc", ""),
        p.get("attachments", []),
        p.get("html_body", False),
    )


@router.get("/office/outlook/inbox")
async def office_outlook_inbox(count: int = 10):
    """Read recent inbox messages from Outlook."""
    from app.services.laptop_control.office_com import OfficeCOM
    import asyncio
    return await asyncio.to_thread(
        OfficeCOM.get().outlook_recent_inbox, count, "Inbox",
    )


# ===== Native Laptop Control (Phase 1 — mouse/keyboard/UIA) =====
# Foreground mode — windows briefly visible. For ANY Windows app.

@router.get("/native/screen-info")
async def native_screen_info():
    """Screen size + mouse position (sanity check pyautogui works)."""
    from app.services.laptop_control.laptop_native import LaptopNative
    inst = LaptopNative.get()
    return {
        "screen": inst.screen_size(),
        "mouse": inst.mouse_position(),
    }


@router.get("/native/windows")
async def native_list_windows():
    """List all visible top-level windows."""
    from app.services.laptop_control.laptop_native import LaptopNative
    return LaptopNative.get().list_open_windows()


@router.post("/native/focus")
async def native_focus(payload: dict):
    """Focus a window by title substring."""
    from app.services.laptop_control.laptop_native import LaptopNative
    title = (payload or {}).get("title", "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title required")
    return LaptopNative.get().focus_window(title)


@router.post("/native/open-app")
async def native_open_app(payload: dict):
    """Open + focus an app. {command: 'notepad', expected_title: 'Notepad'}"""
    from app.services.laptop_control.laptop_native import LaptopNative
    cmd = (payload or {}).get("command", "").strip()
    expected = (payload or {}).get("expected_title", "").strip()
    if not cmd or not expected:
        raise HTTPException(status_code=400, detail="command + expected_title required")
    return LaptopNative.get().open_and_focus(cmd, expected, timeout=10.0)


@router.post("/native/type")
async def native_type(payload: dict):
    """Type text into currently focused window."""
    from app.services.laptop_control.laptop_native import LaptopNative
    text = (payload or {}).get("text", "")
    return LaptopNative.get().type_text(text)


@router.post("/native/hotkey")
async def native_hotkey(payload: dict):
    """Press a hotkey combo. {keys: ['ctrl', 's']}"""
    from app.services.laptop_control.laptop_native import LaptopNative
    keys = (payload or {}).get("keys", [])
    if not keys:
        raise HTTPException(status_code=400, detail="keys array required")
    return LaptopNative.get().hotkey(*keys)


@router.post("/native/uia-click")
async def native_uia_click(payload: dict):
    """Click element by name in a window via UIA.
    {window: 'Notepad', element: 'File', control_type: 'MenuItem'}
    """
    from app.services.laptop_control.laptop_native import LaptopNative
    window = (payload or {}).get("window", "").strip()
    element = (payload or {}).get("element", "").strip()
    ctype = ((payload or {}).get("control_type") or "Button").strip()
    if not window or not element:
        raise HTTPException(status_code=400, detail="window + element required")
    return LaptopNative.get().uia_click(window, element, ctype)


# ── Listening Control ────────────────────────────────────


@router.get("/listening")
async def get_listening(db: AsyncSession = Depends(get_db)):
    """Get listening state."""
    from sqlalchemy import text as sa_text
    result = await db.execute(sa_text("SELECT value FROM system_state WHERE key = 'listening'"))
    row = result.fetchone()
    return {"listening": row[0] if row else "off"}


@router.post("/listening/on")
async def start_listening(db: AsyncSession = Depends(get_db)):
    """Start listening — boss ki marzi se."""
    from sqlalchemy import text as sa_text
    await db.execute(sa_text("INSERT OR REPLACE INTO system_state (key, value) VALUES ('listening', 'on')"))
    await db.commit()

    # Notify in chat
    await db.execute(
        sa_text("INSERT INTO chat_messages (role, content, actions) VALUES ('assistant', 'Listening ON — sun raha hun ab. Jab band karna ho to Stop dabana.', '[]')")
    )
    await db.commit()
    return {"listening": "on"}


@router.post("/listening/off")
async def stop_listening(db: AsyncSession = Depends(get_db)):
    """Stop listening — boss ki marzi se."""
    from sqlalchemy import text as sa_text
    await db.execute(sa_text("INSERT OR REPLACE INTO system_state (key, value) VALUES ('listening', 'off')"))
    await db.commit()

    await db.execute(
        sa_text("INSERT INTO chat_messages (role, content, actions) VALUES ('assistant', 'Listening OFF — ab nahi sun raha. Jab chahein phir ON karein.', '[]')")
    )
    await db.commit()
    return {"listening": "off"}


@router.post("/agent-jobs")
async def agent_job_spawn(payload: dict):
    """Naya background task-AGENT banao (parallel chalega). Body: {"task": "..."}.
    Foran job-id return (block nahi) — agent peeche kaam karta rahega."""
    from app.services.agent_manager import AgentManager
    task = (payload.get("task") or "").strip()
    if not task:
        return {"ok": False, "error": "task chahiye"}
    return await AgentManager.get().spawn(task)


@router.get("/agent-jobs")
async def agent_jobs_list(limit: int = 50):
    """Saare task-agents + unka live status (queued/running/completed/failed)."""
    from app.services.agent_manager import AgentManager
    return {"jobs": await AgentManager.get().list_jobs(limit)}


@router.get("/agent-jobs/{job_id}")
async def agent_job_get(job_id: str):
    """Ek agent ka status/result."""
    from app.services.agent_manager import AgentManager
    job = await AgentManager.get().get_job(job_id)
    if not job:
        return {"ok": False, "error": "job nahi mili"}
    return job


@router.delete("/agent-jobs/{job_id}")
async def agent_job_delete(job_id: str):
    """User ne ✕ dabaya — yeh agent panel se hatao (manual only)."""
    from app.services.agent_manager import AgentManager
    return await AgentManager.get().delete_job(job_id)


@router.post("/web/eval")
async def web_eval_direct(payload: dict):
    """JARVIS browser ki current page pe JS DIRECT chalao (brain ke baghair, fast).
    Debugging/diagnostics ke liye — WhatsApp/koi bhi site ka live DOM turant dekho."""
    from app.services.web_browser import WebBrowser
    import asyncio as _a
    js = payload.get("js", "")
    if not js:
        return {"ok": False, "error": "js chahiye"}
    res = await _a.to_thread(WebBrowser.get().eval_js, js)
    return res


@router.post("/web/click")
async def web_click_direct(payload: dict):
    """JARVIS browser pe REAL (Playwright) click — CSS selector ya visible text.
    JS .click() React buttons pe nahi chalta; yeh asli mouse-event bhejta hai."""
    from app.services.web_browser import WebBrowser
    import asyncio as _a
    target = payload.get("target", "")
    if not target:
        return {"ok": False, "error": "target chahiye"}
    return await _a.to_thread(WebBrowser.get().click, target)


@router.post("/web/close")
async def web_close():
    """JARVIS ke Playwright browser ko CLEANLY band karo — taake logged-in
    sessions (WhatsApp/Gmail/koi bhi site) IndexedDB/disk pe flush ho jayein aur
    server restart ke baad bhi LOGGED-IN rahein. Start script restart se pehle
    isay call karta hai (hard-kill se pehle), warna session udh jaata hai."""
    from app.services.web_browser import WebBrowser
    import asyncio as _a
    try:
        res = await _a.to_thread(WebBrowser.get().close)
        return {"ok": True, "result": res}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


class ChatMessage(BaseModel):
    message: str
    confirm_token: str | None = None
    # Server-resolved paths to files the user attached in the dashboard chat.
    # Populated by POST /api/chat/upload; chat service forwards these as the
    # `attachment` param for send_whatsapp/send_teams/send_email actions so
    # the LLM doesn't need to search the user's laptop.
    attachments: list[str] | None = None


# ───── Chat file upload ─────
# Files uploaded from the dashboard chat are stored here. Cleanup runs on each
# upload to keep the dir small — anything older than UPLOAD_MAX_AGE_SEC goes.
UPLOAD_DIR = Path(__file__).resolve().parents[2] / "data" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_MAX_BYTES = 50 * 1024 * 1024  # 50 MB
UPLOAD_MAX_AGE_SEC = 3600  # 1 hour
SAFE_EXT_RE = re.compile(r"^[A-Za-z0-9._-]{1,40}$")


def _cleanup_old_uploads() -> None:
    """Best-effort sweep of uploads older than UPLOAD_MAX_AGE_SEC. Called on each upload.

    Now recurses into subdirectories — each upload is stored as
    UPLOAD_DIR/<hash>/<original_name>, so we sweep the hash directories.
    """
    now = time.time()
    try:
        for p in UPLOAD_DIR.iterdir():
            try:
                # Old flat-files (legacy)
                if p.is_file():
                    if now - p.stat().st_mtime > UPLOAD_MAX_AGE_SEC:
                        p.unlink(missing_ok=True)
                    continue
                # New: hash-subdir holding the file with original name
                if p.is_dir():
                    if now - p.stat().st_mtime > UPLOAD_MAX_AGE_SEC:
                        import shutil
                        shutil.rmtree(p, ignore_errors=True)
            except OSError:
                continue
    except FileNotFoundError:
        pass


def _sanitize_filename(name: str) -> str:
    """Strip path separators and unsafe chars from a user-supplied filename.
    Keeps the human-readable original so recipients see a real name in Teams/WA.
    Defends against path traversal + null-byte injection.
    """
    import re as _re
    import os as _os
    if not name:
        return "file"
    # Defense layer 1: strip null bytes (CVE-style filename truncation)
    name = name.replace("\x00", "")
    # Defense layer 2: drop ANY directory component via os.path.basename
    # (handles forward slashes, backslashes, drive letters, UNC paths)
    base = _os.path.basename(name.replace("\\", "/"))
    # Defense layer 3: reject "." and ".." entirely (parent dir refs)
    if base in (".", "..") or base.startswith(".."):
        return "file"
    # Defense layer 4: scrub unsafe chars (keep alnum + dot/dash/underscore/brackets/spaces/unicode)
    clean = _re.sub(r"[^A-Za-z0-9._\-() \[\]À-￿]", "_", base).strip()
    if not clean:
        return "file"
    # Cap length to avoid Windows MAX_PATH issues
    if len(clean) > 80:
        from os.path import splitext as _spl
        stem, ext = _spl(clean)
        clean = stem[: 80 - len(ext)] + ext
    return clean or "file"


@router.post("/chat/upload")
async def chat_upload(file: UploadFile = File(...)):
    """Receive a file attached in the dashboard chat. Saves to a temp dir and
    returns the absolute server-side path. The dashboard then includes this
    path in `/api/chat`'s `attachments` array.
    """
    if not file or not file.filename:
        raise HTTPException(status_code=400, detail="No file uploaded")

    _cleanup_old_uploads()

    # Preserve the original filename so recipients see a real name in Teams/WA.
    # Layout: UPLOAD_DIR / <hash> / <sanitized_original_filename>
    # The hash subdir prevents collisions; the filename inside is human-readable.
    original = _sanitize_filename(Path(file.filename).name)
    ext = Path(original).suffix
    if ext and not SAFE_EXT_RE.match(ext.lstrip(".")):
        ext = ""
        # Strip the bad extension from the original name too
        original = original[: -len(Path(original).suffix)] if Path(original).suffix else original
    subdir = UPLOAD_DIR / uuid.uuid4().hex
    subdir.mkdir(parents=True, exist_ok=True)
    target = subdir / original

    written = 0
    with target.open("wb") as out:
        while True:
            chunk = await file.read(64 * 1024)
            if not chunk:
                break
            written += len(chunk)
            if written > UPLOAD_MAX_BYTES:
                out.close()
                target.unlink(missing_ok=True)
                try:
                    subdir.rmdir()
                except OSError:
                    pass
                raise HTTPException(status_code=413, detail=f"File too large (>{UPLOAD_MAX_BYTES // (1024*1024)} MB)")
            out.write(chunk)

    return {
        "path": str(target.resolve()),
        "filename": original,
        "size": written,
        # Browser isay <img>/<a> mein dikha sake — chat mein attachment nazar aaye.
        "url": f"/api/chat/file/{subdir.name}/{original}",
    }


@router.get("/chat/file/{subdir}/{name}")
async def chat_file(subdir: str, name: str):
    """Serve a previously-uploaded chat file so the dashboard can DISPLAY it
    (image preview / PDF link). Path is validated to stay inside UPLOAD_DIR —
    no traversal. Returns 404 if missing."""
    from fastapi.responses import FileResponse
    # subdir = uuid hex (strict). name mein traversal/separator/null na ho.
    if not re.fullmatch(r"[A-Za-z0-9]+", subdir or ""):
        raise HTTPException(status_code=400, detail="bad path")
    if (not name) or "\x00" in name or "/" in name or "\\" in name or name in (".", ".."):
        raise HTTPException(status_code=400, detail="bad name")
    target = (UPLOAD_DIR / subdir / name).resolve()
    # Hard guarantee: resolved path UPLOAD_DIR ke andar hi ho (no traversal).
    if UPLOAD_DIR.resolve() not in target.parents or not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    # Sahi content-type — warna .jfif/.heic jaise image browser inline render nahi
    # karta (broken icon). mimetypes inhe nahi jaanta, isliye khud map karo.
    import mimetypes
    _IMG = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".jpe": "image/jpeg",
        ".jfif": "image/jpeg", ".jff": "image/jpeg", ".jif": "image/jpeg",
        ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp",
        ".bmp": "image/bmp", ".tiff": "image/tiff", ".tif": "image/tiff",
        ".svg": "image/svg+xml", ".ico": "image/x-icon", ".avif": "image/avif",
        ".heic": "image/heic", ".heif": "image/heif", ".pdf": "application/pdf",
    }
    ext = target.suffix.lower()
    media = _IMG.get(ext) or mimetypes.guess_type(str(target))[0] or "application/octet-stream"
    return FileResponse(str(target), media_type=media)


@router.get("/chat/history")
async def chat_history():
    """Get all chat messages."""
    service = _get_chat()
    return await service.get_history()


# ── Calendar ─────────────────────────────────────────────


@router.get("/calendar")
async def list_events(db: AsyncSession = Depends(get_db)):
    """List all calendar events."""
    from sqlalchemy import text as sa_text
    result = await db.execute(
        sa_text("SELECT id, title, event_date, event_time, duration_minutes, attendees, location, notes, status, created_at FROM calendar_events ORDER BY event_date ASC, event_time ASC")
    )
    rows = result.fetchall()
    import json as _json
    return [
        {
            "id": r[0], "title": r[1], "event_date": r[2], "event_time": r[3],
            "duration_minutes": r[4], "attendees": _json.loads(r[5]) if r[5] else [],
            "location": r[6], "notes": r[7], "status": r[8],
            "created_at": r[9],
        }
        for r in rows
    ]


@router.patch("/calendar/{event_id}/confirm")
async def confirm_event(event_id: int, db: AsyncSession = Depends(get_db)):
    """Confirm a calendar event."""
    from sqlalchemy import text as sa_text
    await db.execute(
        sa_text("UPDATE calendar_events SET status = 'confirmed' WHERE id = :id"),
        {"id": event_id},
    )
    await db.commit()
    return {"status": "confirmed", "event_id": event_id}


@router.delete("/calendar/{event_id}")
async def delete_event(event_id: int, db: AsyncSession = Depends(get_db)):
    """Delete a calendar event."""
    from sqlalchemy import text as sa_text
    await db.execute(
        sa_text("DELETE FROM calendar_events WHERE id = :id"),
        {"id": event_id},
    )
    await db.commit()
    return {"deleted": True, "event_id": event_id}


# ── Meetings ─────────────────────────────────────────────

_meeting_service = None

def _get_meeting():
    global _meeting_service
    if _meeting_service is None:
        from app.services.meeting_service import MeetingService
        _meeting_service = MeetingService(ServerConfig())
    return _meeting_service


@router.post("/meeting/start")
async def start_meeting(body: dict | None = None):
    """Start a meeting — turns on listening + tracking.

    Optional body: {"title": "...", "client_id": <int>}. If client_id is given
    the meeting + its tasks are tagged to that client. If omitted, the client
    is auto-detected from the conversation at meeting end.
    """
    body = body or {}
    service = _get_meeting()
    cid = body.get("client_id")
    try:
        cid = int(cid) if cid is not None else None
    except (TypeError, ValueError):
        cid = None
    return await service.start_meeting(body.get("title", ""), client_id=cid)


@router.post("/meeting/end")
async def end_meeting():
    """End meeting — generates summary + turns off listening."""
    service = _get_meeting()
    return await service.end_meeting()


@router.get("/meeting/active")
async def active_meeting():
    """Get currently active meeting."""
    service = _get_meeting()
    meeting = await service.get_active_meeting()
    return meeting or {"active": False}


@router.get("/meetings")
async def list_meetings():
    """List all past meetings (each with its extracted tasks inline)."""
    service = _get_meeting()
    return await service.list_meetings()


@router.get("/meetings/{meeting_id}/tasks")
async def get_meeting_tasks(meeting_id: int):
    """Get a single meeting plus the tasks extracted during it."""
    service = _get_meeting()
    return await service.get_meeting_tasks(meeting_id)


@router.get("/meetings/latest")
async def latest_meeting():
    """Latest meeting + its tasks. Convenient for the dashboard 'last meeting' view."""
    service = _get_meeting()
    meetings = await service.list_meetings()
    return meetings[0] if meetings else None


# ── Notifications ────────────────────────────────────────


@router.get("/notifications")
async def list_notifications(db: AsyncSession = Depends(get_db)):
    """List pending notifications."""
    from sqlalchemy import text as sa_text
    result = await db.execute(
        sa_text("SELECT id, assigned_to, task_id, message, platform, status, created_at FROM notifications ORDER BY id DESC LIMIT 50")
    )
    return [
        {"id": r[0], "assigned_to": r[1], "task_id": r[2], "message": r[3], "platform": r[4], "status": r[5], "created_at": r[6]}
        for r in result.fetchall()
    ]


@router.patch("/notifications/{notif_id}/sent")
async def mark_notification_sent(notif_id: int, db: AsyncSession = Depends(get_db)):
    """Mark notification as sent."""
    from sqlalchemy import text as sa_text
    await db.execute(
        sa_text("UPDATE notifications SET status = 'sent', sent_at = CURRENT_TIMESTAMP WHERE id = :id"),
        {"id": notif_id},
    )
    await db.commit()
    return {"status": "sent", "id": notif_id}


@router.post("/report/generate")
async def generate_report():
    """Manually trigger daily report."""
    from app.services.daily_report import DailyReportService
    reporter = DailyReportService(ServerConfig())
    report = await reporter.generate_report()
    return {"status": "generated", "report": report}


@router.post("/chat")
async def chat(body: ChatMessage):
    """Chat with Jarvis AI."""
    service = _get_chat()
    result = await service.chat(
        body.message,
        confirm_token=body.confirm_token,
        attachments=body.attachments,
    )
    return result


@router.get("/laptop/activity")
async def laptop_activity(limit: int = 50):
    """Get recent laptop control activity log."""
    from app.services.laptop_control.activity_log import ActivityLogger
    return await ActivityLogger.list_recent(limit=limit)


@router.post("/laptop/confirm")
async def laptop_confirm(body: dict):
    """Confirm a pending laptop action by token."""
    service = _get_chat()
    token = body.get("token", "")
    decision = body.get("decision", "yes")
    result = await service.chat(decision, confirm_token=token)
    return result


@router.post("/laptop/edit")
async def laptop_edit(body: dict):
    """Edit a pending message draft (any app) before sending — updates the draft
    text and returns a fresh Confirm/Edit/Cancel draft. Body: {token, message}."""
    service = _get_chat()
    token = body.get("token", "")
    message = body.get("message", "")
    return await service.edit_pending(token, message)


# ===== Universal Engine (UIA + keyboard + PowerShell, Claude CLI brain) =====

@router.get("/engine/status")
async def engine_status():
    """Is the universal engine ready (Claude CLI brain available)?"""
    from app.services.universal_engine.brain import get_brain
    brain = get_brain()
    return {"brain": type(brain).__name__, "available": brain.is_available()}


@router.get("/engine/progress")
async def engine_progress():
    """Live progress of the in-flight engine task (for the chat 'doing…' line)."""
    from app.services.universal_engine.engine import get_progress
    return get_progress()


@router.post("/engine/run")
async def engine_run(body: dict):
    """Run one universal task. {task: '...'} → {ok, reply, steps}."""
    from app.services.consent import ConsentService
    from app.services.laptop_control.activity_log import ActivityLogger
    from app.services.universal_engine.engine import UniversalEngine

    task = (body or {}).get("task", "").strip()
    if not task:
        raise HTTPException(status_code=400, detail="task required")

    if not await ConsentService.is_granted():
        return {
            "ok": False,
            "reply": (
                "🔒 Laptop access permission nahi mili. "
                "Settings page pe ja ke 'Grant Full Access' dabao."
            ),
            "steps": [],
        }

    result = await UniversalEngine.get().run(task)
    await ActivityLogger.log_action(
        "universal_task", {"task": task[:300]},
        result=str(result.get("reply", ""))[:300],
        status="success" if result.get("ok") else "failed",
    )
    return result


# ===== Consent =====

@router.get("/consent")
async def get_consent():
    from app.services.consent import ConsentService
    return await ConsentService.get_state()


@router.post("/consent/grant")
async def grant_consent(body: dict):
    from app.services.consent import ConsentService
    ua = body.get("user_agent", "")
    return await ConsentService.grant(ua)


@router.post("/consent/revoke")
async def revoke_consent():
    from app.services.consent import ConsentService
    return await ConsentService.revoke()


# ===== AI Memory =====

@router.get("/memory")
async def list_memories():
    """List all AI memories."""
    from app.services.memory import MemoryService
    return await MemoryService.list_all()


@router.post("/memory")
async def add_memory(body: dict):
    """Manually add a memory."""
    from app.services.memory import MemoryService
    content = body.get("content", "").strip()
    category = body.get("category", "general")
    importance = int(body.get("importance", 1))
    if not content:
        return {"error": "content required"}
    mem_id = await MemoryService.save(content, category, importance=importance)
    return {"id": mem_id, "saved": True}


@router.delete("/memory/{memory_id}")
async def delete_memory(memory_id: int):
    from app.services.memory import MemoryService
    await MemoryService.deactivate(memory_id)
    return {"deleted": True}


@router.post("/memory/forget-all")
async def forget_all_memories():
    from app.services.memory import MemoryService
    count = await MemoryService.forget_all()
    return {"forgotten": count}


# ===== Verification Flow =====

@router.get("/verification/status")
async def verification_status():
    """Get current verification session state (if any)."""
    from app.services.verification import VerificationService
    vs = VerificationService.get()
    status = vs.get_status()
    if not status:
        return {"active": False}
    return status


@router.post("/verification/action")
async def verification_action(body: dict):
    """Handle verification button clicks: approve/reject/whatsapp/teams/email/cancel."""
    from app.services.verification import VerificationService
    vs = VerificationService.get()
    action = (body.get("action") or "").lower()
    config = ServerConfig()

    if action == "approve":
        result = await vs.approve_current()
    elif action == "reject":
        result = await vs.reject_current()
    elif action in ("whatsapp", "teams", "email"):
        result = await vs.send_via_platform(action, config)
    elif action == "cancel":
        result = await vs.cancel()
    else:
        return {"error": f"Unknown action: {action}"}

    # Save reply to chat history
    from sqlalchemy import text as sa_text
    from app.core.database import async_session
    import json as _json
    async with async_session() as db:
        await db.execute(
            sa_text("INSERT INTO chat_messages (role, content, actions) VALUES ('assistant', :msg, :actions)"),
            {
                "msg": result.get("reply", ""),
                "actions": _json.dumps([{"verification": result.get("verification")}] if result.get("verification") else []),
            },
        )
        await db.commit()

    return result


@router.get("/stats")
async def stats(db: AsyncSession = Depends(get_db)):
    """Dashboard stats."""
    chunks_count = await db.scalar(select(func.count(AudioChunk.id)))
    transcriptions_count = await db.scalar(select(func.count(Transcription.id)))
    tasks_count = await db.scalar(select(func.count(Task.id)))
    pending_tasks = await db.scalar(
        select(func.count(Task.id)).where(Task.status == "pending")
    )

    agents_online = await db.scalar(
        select(func.count(Agent.id)).where(Agent.is_online == True)
    )

    return {
        "total_chunks": chunks_count,
        "total_transcriptions": transcriptions_count,
        "total_tasks": tasks_count,
        "pending_tasks": pending_tasks,
        "agents_online": agents_online,
    }


@router.get("/chunks")
async def list_chunks(
    limit: int = 50, offset: int = 0, db: AsyncSession = Depends(get_db)
):
    """List recent audio chunks."""
    result = await db.execute(
        select(AudioChunk)
        .order_by(AudioChunk.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    chunks = result.scalars().all()

    return [
        {
            "chunk_id": c.chunk_id,
            "agent_id": c.agent_id,
            "source": c.source,
            "start_time": c.start_time.isoformat(),
            "end_time": c.end_time.isoformat(),
            "duration_sec": c.duration_sec,
            "has_speech": c.has_speech,
            "speech_ratio": c.speech_ratio,
            "active_window": c.active_window,
            "status": c.status,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        }
        for c in chunks
    ]


@router.get("/transcriptions")
async def list_transcriptions(
    limit: int = 50, offset: int = 0, db: AsyncSession = Depends(get_db)
):
    """List recent transcriptions."""
    result = await db.execute(
        select(Transcription)
        .order_by(Transcription.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    transcriptions = result.scalars().all()

    return [
        {
            "id": t.id,
            "chunk_id": t.chunk_id,
            "text": t.text,
            "language": t.language,
            "confidence": t.confidence,
            "processing_time_sec": t.processing_time_sec,
            "created_at": t.created_at.isoformat() if t.created_at else None,
        }
        for t in transcriptions
    ]


class TaskCreate(BaseModel):
    title: str
    description: str = ""
    assigned_to: str | None = None
    priority: str = "medium"
    action_type: str | None = None
    action_payload: str | None = None
    requires_approval: bool = True


@router.post("/tasks")
async def create_task(body: TaskCreate, db: AsyncSession = Depends(get_db)):
    """Manually create a task."""
    task = Task(
        title=body.title,
        description=body.description,
        assigned_to=body.assigned_to,
        priority=body.priority,
        action_type=body.action_type,
        action_payload=body.action_payload,
        requires_approval=body.requires_approval,
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return {
        "id": task.id,
        "title": task.title,
        "status": task.status,
        "created_at": task.created_at.isoformat() if task.created_at else None,
    }


@router.get("/tasks")
async def list_tasks(
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
):
    """List tasks, optionally filtered by status."""
    query = select(Task).order_by(Task.created_at.desc()).limit(limit).offset(offset)
    if status:
        query = query.where(Task.status == status)

    result = await db.execute(query)
    tasks = result.scalars().all()

    return [
        {
            "id": t.id,
            "chunk_id": t.chunk_id,
            "title": t.title,
            "description": t.description,
            "assigned_to": t.assigned_to,
            "priority": t.priority,
            "status": t.status,
            "action_type": t.action_type,
            "requires_approval": t.requires_approval,
            "created_at": t.created_at.isoformat() if t.created_at else None,
        }
        for t in tasks
    ]


@router.patch("/tasks/{task_id}/approve")
async def approve_task(task_id: int, db: AsyncSession = Depends(get_db)):
    """Approve a pending task."""
    result = await db.execute(select(Task).where(Task.id == task_id))
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    task.status = "approved"
    await db.commit()
    return {"status": "approved", "task_id": task_id}


@router.patch("/tasks/{task_id}/reject")
async def reject_task(task_id: int, db: AsyncSession = Depends(get_db)):
    """Reject a pending task."""
    result = await db.execute(select(Task).where(Task.id == task_id))
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    task.status = "rejected"
    await db.commit()
    return {"status": "rejected", "task_id": task_id}


class StatusUpdate(BaseModel):
    status: str
    error: str = ""


@router.patch("/tasks/{task_id}/status")
async def update_task_status(
    task_id: int, body: StatusUpdate, db: AsyncSession = Depends(get_db)
):
    """Update task status (used by desktop agent to report execution results)."""
    result = await db.execute(select(Task).where(Task.id == task_id))
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    task.status = body.status
    if body.status == "completed":
        task.completed_at = datetime.utcnow()
    await db.commit()
    return {"status": body.status, "task_id": task_id}


# ── Rules CRUD ───────────────────────────────────────────────


class RuleCreate(BaseModel):
    name: str
    description: str = ""
    enabled: bool = True
    match_action_type: str | None = None
    match_priority: str | None = None
    match_keyword: str | None = None
    match_assigned_to: str | None = None
    match_window: str | None = None
    auto_approve: bool = False
    override_priority: str | None = None
    override_assigned_to: str | None = None
    notify_telegram: bool = False


def _rule_to_dict(r: Rule) -> dict:
    return {
        "id": r.id,
        "name": r.name,
        "description": r.description,
        "enabled": r.enabled,
        "match_action_type": r.match_action_type,
        "match_priority": r.match_priority,
        "match_keyword": r.match_keyword,
        "match_assigned_to": r.match_assigned_to,
        "match_window": r.match_window,
        "auto_approve": r.auto_approve,
        "override_priority": r.override_priority,
        "override_assigned_to": r.override_assigned_to,
        "notify_telegram": r.notify_telegram,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


@router.get("/rules")
async def list_rules(db: AsyncSession = Depends(get_db)):
    """List all automation rules."""
    result = await db.execute(select(Rule).order_by(Rule.id))
    return [_rule_to_dict(r) for r in result.scalars().all()]


@router.post("/rules")
async def create_rule(body: RuleCreate, db: AsyncSession = Depends(get_db)):
    """Create a new automation rule."""
    rule = Rule(**body.model_dump())
    db.add(rule)
    await db.commit()
    await db.refresh(rule)
    return _rule_to_dict(rule)


@router.put("/rules/{rule_id}")
async def update_rule(
    rule_id: int, body: RuleCreate, db: AsyncSession = Depends(get_db)
):
    """Update an existing rule."""
    result = await db.execute(select(Rule).where(Rule.id == rule_id))
    rule = result.scalar_one_or_none()
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")

    for key, val in body.model_dump().items():
        setattr(rule, key, val)
    await db.commit()
    return _rule_to_dict(rule)


@router.delete("/rules/{rule_id}")
async def delete_rule(rule_id: int, db: AsyncSession = Depends(get_db)):
    """Delete a rule."""
    result = await db.execute(select(Rule).where(Rule.id == rule_id))
    rule = result.scalar_one_or_none()
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")

    await db.delete(rule)
    await db.commit()
    return {"deleted": True, "rule_id": rule_id}


@router.patch("/rules/{rule_id}/toggle")
async def toggle_rule(rule_id: int, db: AsyncSession = Depends(get_db)):
    """Toggle a rule enabled/disabled."""
    result = await db.execute(select(Rule).where(Rule.id == rule_id))
    rule = result.scalar_one_or_none()
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")

    rule.enabled = not rule.enabled
    await db.commit()
    return {"id": rule_id, "enabled": rule.enabled}


# ── Agents ───────────────────────────────────────────────


@router.post("/agents/{agent_id}/stop")
async def stop_agent(agent_id: str):
    """Send stop signal to a connected agent."""
    from app.ws.handler import connected_agents
    ws = connected_agents.get(agent_id)
    if not ws:
        raise HTTPException(status_code=404, detail="Agent not connected")
    try:
        await ws.send_json({"command": "stop"})
        await ws.close()
    except Exception:
        pass
    connected_agents.pop(agent_id, None)
    return {"status": "stopped", "agent_id": agent_id}


@router.get("/agents")
async def list_agents(db: AsyncSession = Depends(get_db)):
    """List all registered agents."""
    result = await db.execute(select(Agent).order_by(Agent.created_at))
    agents = result.scalars().all()

    return [
        {
            "id": a.id,
            "agent_id": a.agent_id,
            "name": a.name,
            "hostname": a.hostname,
            "is_online": a.is_online,
            "last_seen": a.last_seen.isoformat() if a.last_seen else None,
            "total_chunks": a.total_chunks,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        }
        for a in agents
    ]


# ── Clients ──────────────────────────────────────────────


class ClientCreate(BaseModel):
    name: str
    company: str = ""
    role: str = ""
    email: str = ""
    phone: str = ""
    website: str = ""
    logo_url: str = ""
    notes: str = ""
    speaker_label: str = ""


def _client_to_dict(c: Client) -> dict:
    return {
        "id": c.id,
        "name": c.name,
        "company": c.company,
        "role": c.role,
        "email": c.email,
        "phone": c.phone,
        "website": c.website,
        "logo_url": c.logo_url,
        "notes": c.notes,
        "speaker_label": c.speaker_label,
        "total_conversations": c.total_conversations,
        "total_tasks": c.total_tasks,
        "last_contact": c.last_contact.isoformat() if c.last_contact else None,
        "is_active": c.is_active,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


@router.get("/clients")
async def list_clients(db: AsyncSession = Depends(get_db)):
    """List all clients."""
    result = await db.execute(select(Client).order_by(Client.name))
    return [_client_to_dict(c) for c in result.scalars().all()]


@router.get("/clients/{client_id}")
async def get_client(client_id: int, db: AsyncSession = Depends(get_db)):
    """Get a single client with their tasks and transcriptions."""
    result = await db.execute(select(Client).where(Client.id == client_id))
    client = result.scalar_one_or_none()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    # Get client's tasks
    tasks_result = await db.execute(
        select(Task).where(Task.client_id == client_id).order_by(Task.created_at.desc()).limit(20)
    )
    tasks = [
        {
            "id": t.id, "title": t.title, "status": t.status,
            "priority": t.priority, "action_type": t.action_type,
            "created_at": t.created_at.isoformat() if t.created_at else None,
        }
        for t in tasks_result.scalars().all()
    ]

    # Get client's transcriptions
    trans_result = await db.execute(
        select(Transcription).where(Transcription.client_id == client_id).order_by(Transcription.created_at.desc()).limit(20)
    )
    transcriptions = [
        {
            "id": t.id, "text": t.text, "language": t.language,
            "created_at": t.created_at.isoformat() if t.created_at else None,
        }
        for t in trans_result.scalars().all()
    ]

    data = _client_to_dict(client)
    data["tasks"] = tasks
    data["transcriptions"] = transcriptions
    return data


@router.post("/clients")
async def create_client(body: ClientCreate, db: AsyncSession = Depends(get_db)):
    """Create a new client."""
    client = Client(
        name=body.name,
        company=body.company or None,
        role=body.role or None,
        email=body.email or None,
        phone=body.phone or None,
        website=body.website or None,
        logo_url=body.logo_url or None,
        notes=body.notes or None,
        speaker_label=body.speaker_label or None,
    )
    db.add(client)
    await db.commit()
    await db.refresh(client)
    return _client_to_dict(client)


@router.put("/clients/{client_id}")
async def update_client(
    client_id: int, body: ClientCreate, db: AsyncSession = Depends(get_db)
):
    """Update a client."""
    result = await db.execute(select(Client).where(Client.id == client_id))
    client = result.scalar_one_or_none()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    for key, val in body.model_dump().items():
        setattr(client, key, val or None)
    await db.commit()
    return _client_to_dict(client)


@router.delete("/clients/{client_id}")
async def delete_client(client_id: int, db: AsyncSession = Depends(get_db)):
    """Delete a client."""
    result = await db.execute(select(Client).where(Client.id == client_id))
    client = result.scalar_one_or_none()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    await db.delete(client)
    await db.commit()
    return {"deleted": True, "client_id": client_id}


@router.patch("/clients/{client_id}/assign-speaker")
async def assign_speaker(client_id: int, body: dict, db: AsyncSession = Depends(get_db)):
    """Assign a speaker label (SPEAKER_00 etc.) to a client."""
    result = await db.execute(select(Client).where(Client.id == client_id))
    client = result.scalar_one_or_none()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    client.speaker_label = body.get("speaker_label", "")
    await db.commit()
    return {"id": client_id, "speaker_label": client.speaker_label}


# ── Vision control (JARVIS sees the screen + acts like a human) ──────
#
# Screenshot of the TARGET WINDOW only → Claude CLI (Opus) sees it → click/type
# like a human (pyautogui). Temp screenshots are deleted right after each step.
# Works on ANY app/web because it relies on what's visible, not app labels.


class VisionDo(BaseModel):
    task: str
    max_steps: int = 9


@router.post("/vision/do")
async def vision_do(body: VisionDo):
    """Run a see→act task. NOTE: moves the real mouse/keyboard."""
    import asyncio
    from app.services.laptop_control.vision_control import VisionController
    try:
        return await asyncio.to_thread(
            VisionController.get().run, body.task, body.max_steps
        )
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.get("/environment")
async def environment(refresh: bool = False):
    """What environment is JARVIS on? (PC/laptop vs phone, OS, installed apps,
    browsers) — so it adapts: PC → web/desktop, phone → app."""
    from app.services.environment import detect_environment
    return detect_environment(refresh=refresh)


@router.get("/environment/reach/{app}")
async def environment_reach(app: str):
    """For a given app, how should JARVIS reach it on this platform?"""
    from app.services.environment import how_to_reach
    return how_to_reach(app)


@router.get("/location")
async def location(refresh: bool = False):
    """Best-known location: user-set area (precise) + IP city (auto). Used so
    JARVIS orders from the NEARBY outlet (e.g. Karachi Defence, not Lahore)."""
    import asyncio
    from app.services.environment import get_location
    return await asyncio.to_thread(get_location, refresh)


@router.post("/location")
async def location_set(body: dict):
    """User sets precise area, e.g. {'location': 'Defence Phase 2, Karachi'}."""
    from app.services.environment import set_user_location
    return set_user_location(str((body or {}).get("location", "")))


@router.post("/location/gps")
async def location_gps(body: dict):
    """Dashboard browser se PRECISE GPS (lat/lon) — JARVIS khud user ki exact
    location jaan le (reverse-geocode + store). Body: {'lat': .., 'lon': ..}."""
    import asyncio
    from app.services.environment import set_gps_location
    try:
        lat = float((body or {}).get("lat"))
        lon = float((body or {}).get("lon"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="lat/lon chahiye (numbers)")
    try:
        accuracy = float((body or {}).get("accuracy"))
    except (TypeError, ValueError):
        accuracy = None
    return await asyncio.to_thread(set_gps_location, lat, lon, accuracy)


@router.post("/stop")
async def stop_task():
    """STOP the currently running task — it halts at its next step. Works even
    while a task is mid-run (the loop runs in a worker thread)."""
    from app.services.task_control import request_stop
    request_stop()
    return {"ok": True, "msg": "stop signal bhej diya — task agle step pe ruk jayega"}
