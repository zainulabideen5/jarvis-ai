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
from app.services.laptop_control.teams import TeamsAutomation
from app.services.laptop_control.vision import VisionDriver
from app.services.laptop_control.whatsapp import WhatsAppAutomation

log = get_logger(__name__)

# Option C — Smart Confirmation
# These actions run DIRECTLY (no confirmation)
DIRECT_ACTIONS = {
    "open_app", "close_app", "find_file", "open_file", "open_folder", "read_file",
    "focus_window", "edit_excel", "screenshot", "send_teams_message",
    "send_whatsapp_message", "system_command", "chat",
    "create_folder", "create_file",
    "open_url", "web_search",
    "send_email",
    "trello_create_card", "trello_move_card", "trello_comment", "trello_list",
}

# These actions require EXPLICIT confirmation (destructive only)
CONFIRM_ACTIONS = {
    "delete_file", "move_file",
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
            if action_type == "send_whatsapp_message":
                if ApprovedRecipients.is_approved("whatsapp", params.get("recipient")):
                    return False
            elif action_type == "send_teams_message":
                if ApprovedRecipients.is_approved("teams", params.get("recipient")):
                    return False
            elif action_type == "send_email":
                to = params.get("to") or params.get("recipient")
                # For email, "to" can be a list; check whichever the first key is
                primary = to if isinstance(to, str) else (to[0] if isinstance(to, list) and to else None)
                if primary and ApprovedRecipients.is_approved("email", primary):
                    return False
            elif action_type == "trello_create_card":
                # Trello lists are static — approve a list once, then create freely
                lst = params.get("list") or params.get("list_name") or "default"
                if ApprovedRecipients.is_approved("trello_list", lst):
                    return False
            elif action_type == "trello_move_card":
                # Approve per (card, target list) pair — narrower than the
                # general list approval, so a slip-up still gets a prompt.
                card = params.get("card") or params.get("title") or ""
                target = params.get("to_list") or params.get("list") or ""
                if card and target:
                    pair = f"{card}->{target}"
                    if ApprovedRecipients.is_approved("trello_move", pair):
                        return False
            elif action_type == "trello_comment":
                card = params.get("card") or params.get("title") or ""
                if card and ApprovedRecipients.is_approved("trello_comment", card):
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

            if action_type == "send_teams_message":
                recipient = (params.get("recipient") or "").strip()
                message = (params.get("message") or "").strip()
                attachment = (params.get("attachment") or "").strip() or None
                if not recipient:
                    return False, "Bhai recipient ka naam to bata — kis ko message bhejna hai?"
                if not message and not attachment:
                    return False, f"{recipient} ko kya bhejna hai? Text ya file dena hoga."
                return TeamsAutomation.send_message(recipient, message, attachment=attachment)

            if action_type == "send_whatsapp_message":
                recipient = (params.get("recipient") or "").strip()
                message = (params.get("message") or "").strip()
                attachment = (params.get("attachment") or "").strip() or None
                prefer_business = bool(params.get("business") or params.get("prefer_business") or False)
                # behind_mode: True → off-screen positioning (user sees only JARVIS dashboard)
                behind_param = params.get("behind_mode")
                behind_mode = behind_param if isinstance(behind_param, bool) else None
                if not recipient:
                    return False, "Bhai recipient ka naam ya number to bata"
                if not message and not attachment:
                    return False, f"{recipient} ko kya WhatsApp pe bhejna hai? Text ya file dena hoga."
                return WhatsAppAutomation.send_message(recipient, message, attachment=attachment, prefer_business=prefer_business, behind_mode=behind_mode)

            if action_type == "screenshot":
                return self._vision.analyze(params.get("query", ""))

            if action_type == "system_command":
                return SystemCommands.execute(params.get("command", ""))

            if action_type == "send_email":
                to = params.get("to") or params.get("recipient")
                subject = (params.get("subject") or "").strip() or "(no subject)"
                body = params.get("body") or params.get("message") or ""
                attachments = params.get("attachments") or params.get("attachment") or None
                from_account = params.get("from") or params.get("from_account") or params.get("account")
                if not to:
                    return False, "Email kis ko bhejna hai? Recipient (to) dena hoga."

                # Extension route — supports text + attachments. Encodes each
                # file to base64 and ships through the extension which builds
                # real File objects and uploads to Gmail compose.
                from app.services.extension_bridge import ExtensionBridge
                bridge = ExtensionBridge.get()
                if bridge.is_connected():
                    ext_params = {
                        "to": to,
                        "subject": subject,
                        "body": body,
                        "from_account": from_account,
                    }
                    ext_timeout = 90
                    # Gmail supports multiple attachments — but extension
                    # currently only handles one. Take the first/main one.
                    first_attach = None
                    if attachments:
                        if isinstance(attachments, list) and attachments:
                            first_attach = attachments[0]
                        elif isinstance(attachments, str):
                            first_attach = attachments
                    if first_attach:
                        # Resolve bare filename via the existing resolver
                        # (WhatsAppAutomation is already imported at module top)
                        ok, resolved = WhatsAppAutomation._resolve_file_path(first_attach)
                        if not ok:
                            return False, resolved
                        import base64, mimetypes, os
                        try:
                            size = os.path.getsize(resolved)
                        except OSError as e:
                            return False, f"Attachment read fail: {e}"
                        MAX_BYTES = 25 * 1024 * 1024  # Gmail's hard limit
                        if size > MAX_BYTES:
                            return False, f"File bohot bara ({size//1024//1024} MB) — Gmail max 25 MB."
                        with open(resolved, "rb") as f:
                            ext_params["attachment_b64"] = base64.b64encode(f.read()).decode("ascii")
                        ext_params["attachment_name"] = os.path.basename(resolved)
                        ext_params["attachment_mime"] = (
                            mimetypes.guess_type(resolved)[0] or "application/octet-stream"
                        )
                        ext_timeout = max(120, int(size / (200 * 1024)) + 60)
                    try:
                        bridge.send_command_sync("gmail_send", ext_params, timeout=ext_timeout)
                        kind = " + attachment" if first_attach else ""
                        return True, f"Email bhej diya {to} ko{kind} (via extension — regular Chrome)"
                    except Exception as e:
                        log.warning("ext_gmail_failed_no_cdp_fallback", error=str(e))
                        return False, (
                            f"Extension se Gmail send fail: {e}\n"
                            f"Regular Chrome mein Gmail tab khol ke login confirm karo, phir retry."
                        )

                # Next: Chrome CDP (uses logged-in Gmail accounts in JARVIS
                # Chrome — no SMTP app-password setup needed). Falls back to
                # SMTP only if CDP isn't running OR send fails AND Gmail
                # credentials are configured.
                from app.services.laptop_control import chrome_cdp
                if chrome_cdp.is_debug_running():
                    from app.services.laptop_control.gmail_cdp import GmailCDP
                    ok, msg = GmailCDP.send(
                        to=to, subject=subject, body=body,
                        attachments=attachments, from_account=from_account,
                    )
                    if ok:
                        return True, msg
                    # If CDP attempted and failed in a specific way (e.g. not
                    # logged in to that account), surface the error directly
                    # instead of silently falling to SMTP (which uses a different
                    # account anyway, surprising the user).
                    log.warning("gmail_cdp_failed", error=msg)
                    # Fall through to SMTP only if explicitly configured
                    try:
                        from app.core.config import ServerConfig
                        cfg = ServerConfig()
                        smtp_ready = bool(cfg.gmail_address) and bool(cfg.gmail_app_password)
                    except Exception:
                        smtp_ready = False
                    if not smtp_ready:
                        return False, msg

                from app.services.laptop_control.email_sender import EmailSender
                return EmailSender.send(to=to, subject=subject, body=body, attachments=attachments)

            if action_type == "trello_create_card":
                from app.services.laptop_control.trello import TrelloDriver
                return TrelloDriver.create_card(
                    title=params.get("title", "") or params.get("name", ""),
                    list_name=params.get("list") or params.get("list_name"),
                    board_name=params.get("board") or params.get("board_name"),
                )

            if action_type == "trello_move_card":
                from app.services.laptop_control.trello import TrelloDriver
                return TrelloDriver.move_card(
                    card_query=params.get("card", "") or params.get("title", ""),
                    to_list=params.get("to_list", "") or params.get("list", ""),
                )

            if action_type == "trello_comment":
                from app.services.laptop_control.trello import TrelloDriver
                return TrelloDriver.add_comment(
                    card_query=params.get("card", "") or params.get("title", ""),
                    comment=params.get("comment", "") or params.get("text", ""),
                )

            if action_type == "trello_list":
                from app.services.laptop_control.trello import TrelloDriver
                return TrelloDriver.list_cards(
                    list_filter=params.get("list") or params.get("filter"),
                )

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

            # --- Multi-Gmail label management via chat ---
            if action_type == "gmail_detect_accounts":
                from app.services.laptop_control.multi_account import GmailMultiAccount
                r = GmailMultiAccount.get().detect_accounts_sync(14, 180.0)
                if r.get("ok"):
                    accounts = r.get("accounts", [])
                    return True, f"{len(accounts)} Gmail accounts detected. Ab labels assign kar: 'Account 0 ko Personal label do'"
                return False, r.get("error", "Gmail detect fail")

            if action_type == "gmail_set_label":
                from app.services.laptop_control.gmail_chrome_native import GmailChromeNative
                idx = params.get("index")
                label = params.get("label", "")
                if idx is None or not label:
                    return False, "Index aur label dono chahiye"
                r = GmailChromeNative.get().set_label(int(idx), label, params.get("email", ""))
                if r.get("ok"):
                    return True, f"Account u/{idx} → '{label}' label assign ho gaya"
                return False, "Label assign fail"

            if action_type == "gmail_list_accounts":
                from app.services.laptop_control.gmail_chrome_native import GmailChromeNative
                accs = GmailChromeNative.get().list_labeled_accounts()
                if not accs:
                    return True, "Koi Gmail label set nahi. Pehle 'Gmail accounts detect karo' bolo, phir labels assign kar."
                lines = [f"  u/{a.get('index')}: {a.get('label') or '(no label)'} — {a.get('email','')}" for a in accs]
                return True, "Gmail accounts:\n" + "\n".join(lines)

            # --- Multi-Gmail (account-labelled send) — NATIVE Chrome ---
            # User explicitly asked for normal Chrome control (NOT Playwright
            # background). Uses gmail_chrome_native which launches Chrome at
            # mail.google.com/mail/u/<index>/ + UIA-driven compose.
            if action_type == "gmail_send_labeled":
                from app.services.laptop_control.gmail_chrome_native import GmailChromeNative
                r = GmailChromeNative.get().send_email_sync(
                    params.get("label", ""),
                    params.get("to", ""),
                    params.get("subject", ""),
                    params.get("body", ""),
                    params.get("attachment", ""),
                    150.0,
                )
                if r.get("ok"):
                    return True, f"'{params.get('label')}' Gmail se bhej diya (Chrome native)"
                return False, r.get("error", "Labeled Gmail send fail")

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
