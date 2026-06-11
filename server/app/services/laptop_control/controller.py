"""LaptopController — orchestrates all laptop automation modules."""

from __future__ import annotations

import asyncio

from app.core.config import ServerConfig
from app.core.logging import get_logger
from app.services.laptop_control.activity_log import ActivityLogger
from app.services.laptop_control.apps import AppController
from app.services.laptop_control.files import FileOperations
from app.services.laptop_control.intent import IntentDetector
from app.services.laptop_control.security import SecurityBlocker
from app.services.laptop_control.system import SystemCommands
from app.services.laptop_control.vision import VisionDriver

log = get_logger(__name__)

# Option C — Smart Confirmation
# These actions run DIRECTLY (no confirmation)
DIRECT_ACTIONS = {
    "open_app", "close_app", "find_file", "open_file", "open_folder", "read_file",
    "focus_window", "edit_excel", "screenshot",
    "system_command", "chat",
    "create_folder", "create_file",
    "open_url", "web_search",
    "send_email",
}

# These actions require EXPLICIT confirmation (destructive only)
CONFIRM_ACTIONS = {
    "delete_file", "move_file",
}

# Per-app scraper actions removed in the universal-engine repivot.
# The new UIA-based universal engine will handle these generically.
REMOVED_ACTIONS = {
    "send_whatsapp_message", "send_teams_message",
    "trello_create_card", "trello_move_card", "trello_comment", "trello_list",
    "gmail_detect_accounts", "gmail_set_label", "gmail_list_accounts",
    "gmail_send_labeled",
}

DESTRUCTIVE_SYSTEM = {"shutdown", "restart", "wifi_off"}


class LaptopController:
    """Main controller — routes commands to appropriate modules."""

    def __init__(self, config: ServerConfig):
        self._config = config
        self._intent = IntentDetector(config)
        self._vision = VisionDriver(config)

    def needs_confirmation(self, action: dict) -> bool:
        """Check if this action requires user confirmation.

        "Remember me" mode for sends: if the recipient has been approved
        before (first send confirmed manually), subsequent sends to the
        same recipient skip confirmation. Other CONFIRM_ACTIONS (delete,
        move, etc.) always prompt — they're inherently destructive.
        """
        action_type = action.get("action", "")
        params = action.get("params", {})

        if action_type in CONFIRM_ACTIONS:
            # Send actions: skip confirmation if recipient is already approved
            from app.services.approved_recipients import ApprovedRecipients
            if action_type == "send_email":
                to = params.get("to") or params.get("recipient")
                # For email, "to" can be a list; check whichever the first key is
                primary = to if isinstance(to, str) else (to[0] if isinstance(to, list) and to else None)
                if primary and ApprovedRecipients.is_approved("email", primary):
                    return False
            return True

        if action_type == "system_command":
            cmd = params.get("command", "").lower()
            if cmd in DESTRUCTIVE_SYSTEM:
                return True

        return False

    async def parse(
        self,
        message: str,
        history: list[dict] | None = None,
        recent_actions: list[dict] | None = None,
    ) -> list[dict]:
        """Parse natural language into structured actions.

        history: recent chat turns (text context).
        recent_actions: structured recent send actions (platform/recipient/is_phone)
            — primary signal for pronoun + platform inheritance.
        """
        return await asyncio.to_thread(
            self._intent.detect, message, history, recent_actions
        )

    async def execute(self, action: dict) -> dict:
        """Execute a single action. Returns result dict."""
        action_type = action.get("action", "")
        params = action.get("params", {})

        # Consent check — block all laptop actions until user grants permission
        from app.services.consent import ConsentService
        consent_granted = await ConsentService.is_granted()
        if not consent_granted and action_type != "chat":
            reason = (
                "🔒 Laptop access permission nahi mili. "
                "Settings page pe ja ke 'Grant Full Access' dabao — phir kaam shuru hoga."
            )
            await ActivityLogger.log_action(
                action_type, params, status="blocked", error="consent_not_granted"
            )
            return {
                "action": action_type,
                "status": "blocked",
                "message": reason,
            }

        # Security check (banks, payment apps, etc.)
        allowed, reason = SecurityBlocker.validate_action(action)
        if not allowed:
            await ActivityLogger.log_action(
                action_type, params, status="blocked", error=reason
            )
            return {
                "action": action_type,
                "status": "blocked",
                "message": reason,
            }

        # Route to handler
        ok, message = await asyncio.to_thread(self._dispatch, action_type, params)

        # Log
        await ActivityLogger.log_action(
            action_type, params,
            result=message if ok else "",
            status="success" if ok else "failed",
            error=message if not ok else "",
        )

        return {
            "action": action_type,
            "status": "success" if ok else "failed",
            "message": message,
            "params": params,
        }

    def _dispatch(self, action_type: str, params: dict) -> tuple[bool, str]:
        """Dispatch to the right module (sync — runs in thread)."""
        try:
            if action_type in REMOVED_ACTIONS:
                return False, (
                    "Yeh feature naye universal engine mein rebuild ho raha hai — "
                    "purane per-app drivers hata diye gaye hain."
                )

            if action_type == "open_app":
                return AppController.open_app(params.get("app", ""))

            if action_type == "close_app":
                return AppController.close_app(params.get("app", ""))

            if action_type == "focus_window":
                return AppController.focus_window(params.get("title", ""))

            if action_type == "find_file":
                results = FileOperations.find_files(
                    params.get("query", ""),
                    location=params.get("location"),
                )
                if not results:
                    return False, "Koi file nahi mili"
                lines = []
                for i, r in enumerate(results[:10]):
                    icon = "📁" if r.get("is_folder") else "📄"
                    size = "" if r.get("is_folder") else f" — {r['size_kb']} KB"
                    lines.append(f"{i+1}. {icon} {r['name']}{size}\n   📍 {r['path']}")
                return True, f"{len(results)} {'files/folders' if any(x.get('is_folder') for x in results) else 'files'} mili:\n\n" + "\n\n".join(lines)

            if action_type == "open_file":
                return FileOperations.open_file(params.get("path", ""))

            if action_type == "open_folder":
                return FileOperations.open_folder(
                    params.get("name", "") or params.get("path", ""),
                    location=params.get("location"),
                )

            if action_type == "read_file":
                ok, content = FileOperations.read_file(params.get("path", ""))
                if ok and params.get("action") == "summarize":
                    return self._summarize(content)
                return ok, content[:1500] if ok else content

            if action_type == "create_file":
                ftype = (params.get("type") or "txt").lower()
                name = params.get("name", "untitled")
                content = params.get("content", "")
                location = params.get("location") or params.get("path") or "Desktop"
                if ftype == "folder":
                    return FileOperations.create_folder(name, location)
                if ftype == "docx":
                    return FileOperations.create_word_file(name, content, location)
                return FileOperations.create_text_file(name, content, location)

            if action_type == "create_folder":
                return FileOperations.create_folder(
                    params.get("name", "New Folder"),
                    params.get("location", "Desktop"),
                )

            if action_type == "edit_excel":
                return FileOperations.add_excel_row(
                    params.get("file", ""),
                    params.get("data", []),
                    params.get("sheet"),
                )

            if action_type == "move_file":
                return FileOperations.move_file(
                    params.get("source", ""), params.get("dest", "")
                )

            if action_type == "delete_file":
                return FileOperations.delete_file(
                    params.get("path", ""),
                    kind=params.get("kind", "auto"),
                )

            if action_type == "screenshot":
                return self._vision.analyze(params.get("query", ""))

            if action_type == "system_command":
                return SystemCommands.execute(params.get("command", ""))

            if action_type == "send_email":
                to = params.get("to") or params.get("recipient")
                subject = (params.get("subject") or "").strip() or "(no subject)"
                body = params.get("body") or params.get("message") or ""
                attachments = params.get("attachments") or params.get("attachment") or None
                if not to:
                    return False, "Email kis ko bhejna hai? Recipient (to) dena hoga."

                from app.services.laptop_control.email_sender import EmailSender
                return EmailSender.send(to=to, subject=subject, body=body, attachments=attachments)

            if action_type == "open_url":
                return AppController.open_url(params.get("url", ""))

            if action_type == "web_search":
                return AppController.web_search(params.get("query", ""))

            if action_type == "chat":
                return True, "chat"

            # ====== PHASE 6 — Native + multi-account + Office actions ======

            # --- Office COM (silent background) ---
            if action_type == "excel_formula":
                from app.services.laptop_control.office_com import OfficeCOM
                r = OfficeCOM.get().excel_run_formula(
                    params.get("file_path", ""),
                    params.get("sheet", ""),
                    params.get("target_cell", ""),
                    params.get("formula", ""),
                )
                if r.get("ok"):
                    return True, f"Excel: {params.get('target_cell','')} = {r.get('result')}"
                return False, r.get("error", "Excel formula fail")

            if action_type == "excel_append":
                from app.services.laptop_control.office_com import OfficeCOM
                r = OfficeCOM.get().excel_append_row(
                    params.get("file_path", ""),
                    params.get("sheet", ""),
                    params.get("row", []),
                )
                if r.get("ok"):
                    return True, f"Excel row {r.get('row_added')} mein add kar diya"
                return False, r.get("error", "Excel append fail")

            if action_type == "excel_create":
                from app.services.laptop_control.office_com import OfficeCOM
                r = OfficeCOM.get().excel_create_new(
                    params.get("file_path", ""),
                    params.get("headers"),
                    params.get("rows"),
                )
                if r.get("ok"):
                    return True, f"Excel file create kar diya: {r.get('path')}"
                return False, r.get("error", "Excel create fail")

            if action_type == "word_create":
                from app.services.laptop_control.office_com import OfficeCOM
                r = OfficeCOM.get().word_create(
                    params.get("file_path", ""),
                    params.get("content", ""),
                    params.get("title", ""),
                )
                if r.get("ok"):
                    return True, f"Word document save kar diya: {r.get('path')}"
                return False, r.get("error", "Word create fail")

            if action_type == "word_append":
                from app.services.laptop_control.office_com import OfficeCOM
                r = OfficeCOM.get().word_append(
                    params.get("file_path", ""),
                    params.get("text", ""),
                )
                if r.get("ok"):
                    return True, f"Word document mein {r.get('appended_chars')} chars add kar diye"
                return False, r.get("error", "Word append fail")

            if action_type == "outlook_send":
                from app.services.laptop_control.office_com import OfficeCOM
                r = OfficeCOM.get().outlook_send_email(
                    params.get("to", ""),
                    params.get("subject", ""),
                    params.get("body", ""),
                    params.get("cc", ""),
                    params.get("bcc", ""),
                    params.get("attachments"),
                    params.get("html_body", False),
                    from_account=params.get("from_account", ""),
                )
                if r.get("ok"):
                    via = r.get("from_account", "")
                    suffix = f" ({via} se)" if via else ""
                    warn = ""
                    if r.get("warning"):
                        warn = f"\n⚠️ {r['warning']}"
                    return True, f"Outlook se email bhej diya{suffix}{warn}"
                return False, r.get("error", "Outlook send fail")

            if action_type == "outlook_list_accounts":
                from app.services.laptop_control.office_com import OfficeCOM
                r = OfficeCOM.get().outlook_list_accounts()
                if r.get("ok"):
                    accs = r.get("accounts", [])
                    if not accs:
                        return True, "Outlook mein abhi koi account nahi mila"
                    lines = [f"Outlook mein {len(accs)} accounts hain:"]
                    for a in accs:
                        lines.append(f"  • {a.get('smtp') or a.get('display_name')}")
                    return True, "\n".join(lines)
                return False, r.get("error", "Account list fail")

            # --- Native Windows app drivers ---
            if action_type == "notepad_save":
                from app.services.laptop_control.win_apps_uia import WinAppsUIA
                r = WinAppsUIA.get().notepad_write_and_save(
                    params.get("content", ""),
                    params.get("save_path", ""),
                )
                if r.get("ok"):
                    return True, f"Notepad mein likh ke save kar diya: {r.get('saved_to', '?')}"
                return False, r.get("error", "Notepad save fail")

            if action_type == "calculator_compute":
                from app.services.laptop_control.win_apps_uia import WinAppsUIA
                r = WinAppsUIA.get().calculator_compute(params.get("expression", ""))
                if r.get("ok"):
                    return True, f"{params.get('expression')} = {r.get('result')}"
                return False, r.get("error", "Calculator fail")

            if action_type == "settings_open":
                from app.services.laptop_control.win_apps_uia import WinAppsUIA
                r = WinAppsUIA.get().settings_open(params.get("panel", ""))
                if r.get("ok"):
                    return True, f"Settings panel khol diya: {params.get('panel','main')}"
                return False, r.get("error", "Settings open fail")

            if action_type == "explorer_open":
                from app.services.laptop_control.win_apps_uia import WinAppsUIA
                r = WinAppsUIA.get().explorer_open(params.get("path", ""))
                if r.get("ok"):
                    return True, f"File Explorer khol diya: {r.get('opened')}"
                return False, r.get("error", "Explorer open fail")

            # --- Native mouse/keyboard (rare — usually dispatched indirectly) ---
            if action_type == "native_type":
                from app.services.laptop_control.laptop_native import LaptopNative
                r = LaptopNative.get().type_text(params.get("text", ""))
                if r.get("ok"):
                    return True, f"Type kar diya: {r.get('len')} chars"
                return False, r.get("error", "Type fail")

            if action_type == "native_hotkey":
                from app.services.laptop_control.laptop_native import LaptopNative
                keys = params.get("keys", [])
                if not keys:
                    return False, "keys list empty"
                r = LaptopNative.get().hotkey(*keys)
                if r.get("ok"):
                    return True, f"Hotkey: {r.get('combo')}"
                return False, r.get("error", "Hotkey fail")

            # --- Image-match (visual apps like Photoshop) ---
            if action_type == "image_click":
                from app.services.laptop_control.image_match import ImageMatcher
                r = ImageMatcher.get().click_template(
                    params.get("template", ""),
                    float(params.get("confidence", 0.85)),
                )
                if r.get("ok"):
                    return True, f"Template click kiya: confidence {r.get('confidence')}"
                return False, r.get("error", "Image click fail")

            return False, f"Unknown action: {action_type}"

        except Exception as e:
            log.warning("action_dispatch_failed", action=action_type, error=str(e))
            return False, f"Action failed: {e}"

    def _summarize(self, content: str) -> tuple[bool, str]:
        """Quick summary via LLM."""
        if not content or len(content) < 50:
            return True, content

        try:
            from app.core.llm import LLMClient
            client = LLMClient(self._config)
            response = client.chat.completions.create(
                model=self._config.groq_model,
                messages=[{
                    "role": "user",
                    "content": f"Summarize is content ko Roman Urdu mein, 3-5 key points mein:\n\n{content[:4000]}",
                }],
                temperature=0.3,
                max_tokens=400,
            )
            return True, response.choices[0].message.content.strip()
        except Exception:
            return True, content[:1000]
