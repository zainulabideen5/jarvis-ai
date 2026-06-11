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


# ── Extension Bridge (Chrome extension WebSocket) ────────

@router.websocket("/ext/ws")
async def extension_ws(websocket: WebSocket):
    """Persistent socket from the JARVIS Chrome extension.

    The extension auto-connects and stays connected. Server sends `command`
    messages over this socket when chat needs the extension to do something
    in the user's Chrome (open a WhatsApp tab, etc.). Extension replies with
    `result` messages keyed by req_id.

    See app.services.extension_bridge.ExtensionBridge for the matching logic.
    """
    import json as _json
    from app.services.extension_bridge import ExtensionBridge

    await websocket.accept()
    bridge = ExtensionBridge.get()
    await bridge.attach(websocket)
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = _json.loads(raw)
            except Exception:
                continue
            await bridge.handle_message(msg)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await websocket.close()
        except Exception:
            pass
    finally:
        await bridge.detach()


@router.get("/ext/status")
async def extension_status():
    """Diagnostic — is the extension currently connected?"""
    from app.services.extension_bridge import ExtensionBridge
    return ExtensionBridge.get().status()


# ── Selector config (Phase 1 hardening) ──────────────────────────────────
# Centralized selector source-of-truth. Extension fetches this on load and
# polls every N minutes for updates. When Meta/Google/Microsoft push a UI
# update that breaks a selector, we edit data/selectors.json + bump version
# — all 50 users auto-update within minutes. No manual intervention per user.

SELECTORS_JSON_PATH = Path(__file__).resolve().parents[2] / "data" / "selectors.json"


@router.get("/ext/selectors")
async def ext_selectors(since: int = 0):
    """Serve the latest selector config to the extension.

    Args:
        since: client's last-known version. If server's version <= since,
               returns 304 with no body (saves bandwidth).
    """
    if not SELECTORS_JSON_PATH.exists():
        raise HTTPException(status_code=500, detail="selectors.json missing on server")
    try:
        data = json.loads(SELECTORS_JSON_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=f"selectors.json invalid: {e}")
    server_version = int(data.get("_meta", {}).get("version", 0))
    if since and server_version <= since:
        # Not modified — extension already has latest
        from fastapi import Response
        return Response(status_code=304)
    return data


@router.get("/ext/selectors/version")
async def ext_selectors_version():
    """Lightweight endpoint — just returns current version + updated_at.

    Extension polls this every ~5 minutes; only fetches full config when
    version increments. Saves bandwidth + reduces server load.
    """
    if not SELECTORS_JSON_PATH.exists():
        return {"version": 0, "updated_at": "", "available": False}
    try:
        data = json.loads(SELECTORS_JSON_PATH.read_text(encoding="utf-8"))
        meta = data.get("_meta", {})
        return {
            "version": int(meta.get("version", 0)),
            "updated_at": meta.get("updated_at", ""),
            "available": True,
        }
    except Exception:
        return {"version": 0, "updated_at": "", "available": False}


@router.post("/whatsapp/setup")
async def whatsapp_pw_setup():
    """First-time WhatsApp Web login via headless Playwright.

    Opens a VISIBLE Chromium window so the user can scan the QR code on
    their phone. After successful login, cookies are saved to disk and all
    subsequent sends run completely headless (no window ever visible again).
    """
    from app.services.laptop_control.wa_playwright import WhatsAppPlaywright
    import asyncio
    try:
        # Run the blocking sync wrapper in a thread so we don't block the event loop
        ok, msg = await asyncio.to_thread(
            WhatsAppPlaywright.get().first_time_login_sync, 180
        )
        return {"ok": ok, "message": msg}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/whatsapp/diagnose-attach")
async def whatsapp_pw_diagnose_attach(payload: dict | None = None):
    """One-shot diagnostic: open attach menu and dump all file inputs
    with their DOM ancestry. Used to fix sticker/document routing without
    requiring the user to send actual files repeatedly.
    """
    from app.services.laptop_control.wa_playwright import WhatsAppPlaywright
    import asyncio
    recipient = ((payload or {}).get("recipient") or "").strip()
    try:
        result = await asyncio.to_thread(
            WhatsAppPlaywright.get().diagnose_attach_inputs_sync, recipient, 90
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/whatsapp/status")
async def whatsapp_pw_status():
    """Quick check: is the Playwright WA session set up + is the headless
    background browser currently alive?
    """
    from app.services.laptop_control.wa_playwright import WhatsAppPlaywright, _SESSION_DIR
    inst = WhatsAppPlaywright.get()
    return {
        "playwright_session_exists": WhatsAppPlaywright.session_exists(),
        "background_alive": inst.is_background_alive(),
        "session_dir": str(_SESSION_DIR),
    }


@router.post("/workspace/open")
async def workspace_open():
    """Open the unified JARVIS browser — ONE Chromium with WhatsApp +
    Teams tabs side by side. User does both logins in a single window.
    """
    from app.services.laptop_control.wa_playwright import WhatsAppPlaywright
    import asyncio
    try:
        result = await asyncio.to_thread(
            WhatsAppPlaywright.get().open_workspace_sync, 180
        )
        # Bust the workspace status cache so the dashboard's next poll
        # sees the fresh post-login state immediately.
        _WORKSPACE_STATUS_CACHE["data"] = None
        _WORKSPACE_STATUS_CACHE["at"] = 0.0
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


_WORKSPACE_STATUS_CACHE: dict = {"at": 0.0, "data": None}
_WORKSPACE_STATUS_TTL = 4.0  # seconds — dashboard polls every 8s, half that is safe


@router.get("/workspace/status")
async def workspace_status():
    """Per-service connection status for the unified browser.

    Cached briefly so the 8-second dashboard polling doesn't hammer the
    Playwright Page.is_closed() checks (cross-process calls). The cache
    TTL (4s) is half the poll interval so it gives the latest within one
    refresh window.
    """
    import time as _time
    now = _time.monotonic()
    if _WORKSPACE_STATUS_CACHE["data"] is not None and \
       (now - _WORKSPACE_STATUS_CACHE["at"]) < _WORKSPACE_STATUS_TTL:
        return _WORKSPACE_STATUS_CACHE["data"]

    from app.services.laptop_control.wa_playwright import WhatsAppPlaywright
    inst = WhatsAppPlaywright.get()
    data = {
        "browser_alive": inst.is_background_alive(),
        "whatsapp": {
            "session_exists": WhatsAppPlaywright.session_exists(),
            "page_alive": inst.is_background_alive(),
        },
        "teams": {
            "session_exists": WhatsAppPlaywright.teams_session_exists(),
            "page_alive": inst.is_teams_alive(),
        },
        "gmail": {
            "session_exists": WhatsAppPlaywright.gmail_session_exists(),
            "page_alive": inst.is_gmail_alive(),
        },
        "trello": {
            "session_exists": WhatsAppPlaywright.trello_session_exists(),
            "page_alive": inst.is_trello_alive(),
        },
    }
    _WORKSPACE_STATUS_CACHE["data"] = data
    _WORKSPACE_STATUS_CACHE["at"] = now
    return data


# ===== WA Chrome native diagnostic =====

@router.get("/wa-chrome/find")
async def wa_chrome_find():
    """Find WhatsApp tab in user's regular Chrome (UIA tab-strip scan)."""
    from app.services.laptop_control.wa_chrome_native import WhatsAppChromeNative
    return WhatsAppChromeNative.get().find_whatsapp_window()


@router.post("/wa-chrome/focus")
async def wa_chrome_focus():
    from app.services.laptop_control.wa_chrome_native import WhatsAppChromeNative
    import asyncio
    return await asyncio.to_thread(WhatsAppChromeNative.get().focus_whatsapp, True)


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


# ===== Multi-account routing (Phase 3 — Gmail 14+ accounts in one Chromium) =====

@router.post("/accounts/gmail/detect")
async def accounts_gmail_detect(payload: dict | None = None):
    """Probe /u/0..u/N — detect logged-in Gmail accounts in JARVIS Chromium."""
    from app.services.laptop_control.multi_account import GmailMultiAccount
    import asyncio
    max_probe = ((payload or {}).get("max_probe") or 14)
    try:
        result = await asyncio.to_thread(
            GmailMultiAccount.get().detect_accounts_sync, max_probe, 180.0,
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/accounts/gmail/list")
async def accounts_gmail_list():
    """List saved Gmail accounts with their labels."""
    from app.services.laptop_control.multi_account import GmailMultiAccount
    return {"accounts": GmailMultiAccount.get().list_labeled_accounts_sync()}


@router.post("/accounts/gmail/label")
async def accounts_gmail_label(payload: dict):
    """Assign / update a label. {index, label, email?}"""
    from app.services.laptop_control.multi_account import GmailMultiAccount
    p = payload or {}
    idx = p.get("index")
    label = (p.get("label") or "").strip()
    email = (p.get("email") or "").strip()
    if idx is None or not label:
        raise HTTPException(status_code=400, detail="index + label required")
    return GmailMultiAccount.get().set_label_sync(int(idx), label, email)


@router.post("/accounts/gmail/remove")
async def accounts_gmail_remove(payload: dict):
    """Remove a Gmail account label. {index}"""
    from app.services.laptop_control.multi_account import GmailMultiAccount
    idx = (payload or {}).get("index")
    if idx is None:
        raise HTTPException(status_code=400, detail="index required")
    return GmailMultiAccount.get().remove_label_sync(int(idx))


@router.post("/accounts/gmail/open")
async def accounts_gmail_open(payload: dict):
    """Open a Gmail tab by label or index. {label?, index?}"""
    from app.services.laptop_control.multi_account import GmailMultiAccount
    import asyncio
    p = payload or {}
    target = p.get("label") if p.get("label") else p.get("index")
    if target is None or target == "":
        raise HTTPException(status_code=400, detail="label or index required")
    try:
        return await asyncio.to_thread(
            GmailMultiAccount.get().open_account_sync, target, 30.0,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/accounts/gmail/send")
async def accounts_gmail_send(payload: dict):
    """Send email from a labeled Gmail account.
    {label, to, subject, body, attachment?}"""
    from app.services.laptop_control.multi_account import GmailMultiAccount
    import asyncio
    p = payload or {}
    label = (p.get("label") or "").strip()
    to = (p.get("to") or "").strip()
    if not label or not to:
        raise HTTPException(status_code=400, detail="label + to required")
    try:
        return await asyncio.to_thread(
            GmailMultiAccount.get().send_via_label_sync,
            label,
            to,
            p.get("subject", ""),
            p.get("body", ""),
            p.get("attachment", ""),
            150.0,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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


# ===== Universal Browser Controller (Hybrid Option D) =====
# Open ANY website in the shared Chromium and automate it via:
#   1. Cached macro (recorded by user)
#   2. DOM heuristics
#   3. ARIA tree fallback
# Zero LLM. Zero per-service code. Works on ANY site (eventually).

@router.post("/universal/open")
async def universal_open(payload: dict):
    """Open a URL in the shared JARVIS Chromium."""
    from app.services.laptop_control.universal_browser import UniversalBrowser
    import asyncio
    url = (payload or {}).get("url", "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="url required")
    try:
        result = await asyncio.to_thread(UniversalBrowser.get().open_url_sync, url, 45)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/universal/send")
async def universal_send(payload: dict):
    """Universal send — tries cached macro → heuristics → ARIA → fail with
    suggestion to teach the site.
    """
    from app.services.laptop_control.universal_browser import UniversalBrowser
    import asyncio
    p = payload or {}
    url = (p.get("url") or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="url required")
    recipient = (p.get("recipient") or "").strip()
    message = (p.get("message") or "").strip()
    attachment = (p.get("attachment") or "").strip()
    try:
        result = await asyncio.to_thread(
            UniversalBrowser.get().send_message_sync,
            url, recipient, message, attachment, 120,
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/universal/teach/start")
async def universal_teach_start(payload: dict):
    """Begin recording user actions on a URL. Returns site_key."""
    from app.services.laptop_control.universal_browser import UniversalBrowser
    import asyncio
    url = (payload or {}).get("url", "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="url required")
    try:
        result = await asyncio.to_thread(UniversalBrowser.get().teach_start_sync, url, 45)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/universal/teach/stop")
async def universal_teach_stop(payload: dict):
    """Stop recording, save captured macro."""
    from app.services.laptop_control.universal_browser import UniversalBrowser
    import asyncio
    site_key = (payload or {}).get("site_key", "").strip()
    label = ((payload or {}).get("label") or "send").strip()
    if not site_key:
        raise HTTPException(status_code=400, detail="site_key required")
    try:
        result = await asyncio.to_thread(UniversalBrowser.get().teach_stop_sync, site_key, label, 15)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/universal/learned-sites")
async def universal_learned_sites():
    """List all learned sites + their saved macro labels."""
    from app.services.laptop_control.universal_browser import UniversalBrowser
    import asyncio
    try:
        result = await asyncio.to_thread(UniversalBrowser.get().list_learned_sites_sync)
        return {"sites": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/teams/diagnose-chat")
async def teams_diagnose_chat(payload: dict | None = None):
    """Open Teams, optionally search for a recipient, dump search results +
    compose box + attach buttons for debugging selector drift."""
    from app.services.laptop_control.wa_playwright import WhatsAppPlaywright
    import asyncio
    recipient = ((payload or {}).get("recipient") or "").strip()
    try:
        result = await asyncio.to_thread(
            WhatsAppPlaywright.get()._diagnose_teams_chat_sync, recipient, 60
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/gmail/diagnose-compose")
async def gmail_diagnose_compose():
    """Open Gmail compose dialog and dump all input/textbox elements with
    their aria-labels — used to fix selector drift when Gmail UI changes.
    """
    from app.services.laptop_control.wa_playwright import WhatsAppPlaywright
    import asyncio
    try:
        result = await asyncio.to_thread(WhatsAppPlaywright.get()._diagnose_gmail_compose_sync, 60)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/trello/diagnose-board")
async def trello_diagnose_board(payload: dict | None = None):
    """Open a Trello board and dump list/card structure for debugging."""
    from app.services.laptop_control.wa_playwright import WhatsAppPlaywright
    import asyncio
    board = ((payload or {}).get("board_name") or "").strip()
    try:
        result = await asyncio.to_thread(
            WhatsAppPlaywright.get()._diagnose_trello_board_sync, board, 60
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/teams/setup")
async def teams_pw_setup():
    """First-time Teams login via the SAME Playwright Chromium as WhatsApp.
    Brings teams.microsoft.com to the front in the persistent browser so the
    user can sign in with their Microsoft account. After login, cookies are
    saved to the shared session dir; subsequent sends are silent + background.
    """
    from app.services.laptop_control.wa_playwright import WhatsAppPlaywright
    import asyncio
    try:
        ok, msg = await asyncio.to_thread(
            WhatsAppPlaywright.get().teams_first_time_login_sync, 180
        )
        return {"ok": ok, "message": msg}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/teams/status")
async def teams_pw_status():
    """Is Teams set up + currently logged in inside the shared browser?"""
    from app.services.laptop_control.wa_playwright import WhatsAppPlaywright
    inst = WhatsAppPlaywright.get()
    return {
        "teams_session_exists": WhatsAppPlaywright.teams_session_exists(),
        "teams_page_alive": inst.is_teams_alive(),
        "background_alive": inst.is_background_alive(),
    }


@router.post("/ext/test")
async def extension_test(payload: dict | None = None):
    """Send a test command to the extension. Used for end-to-end ping."""
    from app.services.extension_bridge import ExtensionBridge
    bridge = ExtensionBridge.get()
    if not bridge.is_connected():
        raise HTTPException(status_code=503, detail="extension_not_connected")
    action = (payload or {}).get("action", "echo")
    params = (payload or {}).get("params", {"hello": "from JARVIS"})
    try:
        result = await bridge.send_command(action, params, timeout=15)
        return {"ok": True, "result": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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
    }


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
async def start_meeting(body: dict = {}):
    """Start a meeting — turns on listening + tracking."""
    service = _get_meeting()
    return await service.start_meeting(body.get("title", ""))


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


# ===== Chrome Background Mode =====

@router.get("/chrome/status")
async def chrome_status():
    """Check if Chrome is in debug mode for background sending."""
    from app.services.laptop_control import chrome_cdp
    return chrome_cdp.get_status()


@router.post("/chrome/enable-background")
async def chrome_enable_background():
    """Restart Chrome with debug flag (kills existing Chrome — restores tabs)."""
    from app.services.laptop_control import chrome_cdp
    import asyncio
    ok, msg = await asyncio.to_thread(chrome_cdp.restart_chrome_with_debug, True)
    return {"success": ok, "message": msg, "status": chrome_cdp.get_status()}


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
        return {"error": "Task not found"}, 404

    task.status = "approved"
    await db.commit()
    return {"status": "approved", "task_id": task_id}


@router.patch("/tasks/{task_id}/reject")
async def reject_task(task_id: int, db: AsyncSession = Depends(get_db)):
    """Reject a pending task."""
    result = await db.execute(select(Task).where(Task.id == task_id))
    task = result.scalar_one_or_none()
    if not task:
        return {"error": "Task not found"}, 404

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
        return {"error": "Task not found"}, 404

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
        return {"error": "Rule not found"}, 404

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
        return {"error": "Rule not found"}, 404

    await db.delete(rule)
    await db.commit()
    return {"deleted": True, "rule_id": rule_id}


@router.patch("/rules/{rule_id}/toggle")
async def toggle_rule(rule_id: int, db: AsyncSession = Depends(get_db)):
    """Toggle a rule enabled/disabled."""
    result = await db.execute(select(Rule).where(Rule.id == rule_id))
    rule = result.scalar_one_or_none()
    if not rule:
        return {"error": "Rule not found"}, 404

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
        return {"error": "Agent not connected"}, 404
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
        return {"error": "Client not found"}, 404

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
        return {"error": "Client not found"}, 404

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
        return {"error": "Client not found"}, 404

    await db.delete(client)
    await db.commit()
    return {"deleted": True, "client_id": client_id}


@router.patch("/clients/{client_id}/assign-speaker")
async def assign_speaker(client_id: int, body: dict, db: AsyncSession = Depends(get_db)):
    """Assign a speaker label (SPEAKER_00 etc.) to a client."""
    result = await db.execute(select(Client).where(Client.id == client_id))
    client = result.scalar_one_or_none()
    if not client:
        return {"error": "Client not found"}, 404

    client.speaker_label = body.get("speaker_label", "")
    await db.commit()
    return {"id": client_id, "speaker_label": client.speaker_label}
