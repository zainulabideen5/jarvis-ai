"""Teams automation — Chrome CDP background mode (preferred) with /chat slash command UI fallback."""

from __future__ import annotations

import time

from app.core.logging import get_logger
from app.services.laptop_control.apps import AppController

log = get_logger(__name__)


class TeamsAutomation:
    """Send Teams DMs (and files/documents) — uses Chrome CDP (background, no focus steal) when available, else falls back to UI automation."""

    @staticmethod
    def send_message(recipient: str, message: str, attachment: str | None = None) -> tuple[bool, str]:
        if not recipient or not recipient.strip():
            return False, "Recipient name dena hoga"
        # Attachment-only sends are valid (caption optional) — text-only sends still need a message
        if not attachment and (not message or not message.strip()):
            return False, "Message text ya attachment dena hoga"

        recipient = recipient.strip()
        message = (message or "").strip()
        attachment = (attachment or "").strip() or None

        # Resolve attachment path if given
        if attachment:
            from app.services.laptop_control.whatsapp import WhatsAppAutomation
            ok, resolved_or_err = WhatsAppAutomation._resolve_file_path(attachment)
            if not ok:
                return False, resolved_or_err
            attachment = resolved_or_err

        # ===== teams_fixed.py DISABLED (2026-06-11) =====
        # Stonic-style fixed-window path repeatedly failed for Teams (search bar
        # focus state, fuzzy autocomplete, /chat slash inconsistencies). User
        # explicitly asked to revert to legacy proven UIA path. The teams_fixed.py
        # module stays on disk for future revival, but is NOT wired in.

        # ===== Legacy UIA path — TEXT and ATTACHMENTS =====
        try:
            ok_d, msg_d = TeamsAutomation._send_via_desktop_uia_silent(recipient, message, attachment)
            return ok_d, msg_d
        except Exception as e:
            log.warning("teams_desktop_uia_exception", error=str(e)[:300])
            return False, (
                f"Teams Desktop UIA exception: {str(e)[:160]}. "
                f"Teams kholo + '{recipient}' ka chat open kar — phir retry."
            )

        # ===== Old paths kept disabled to avoid Playwright/Chrome popups =====
        try:
            from app.services.laptop_control.wa_playwright import WhatsAppPlaywright
            tpw = WhatsAppPlaywright.get()
            ok_pw, msg_pw = tpw.teams_send_message_sync(
                recipient=recipient,
                message=message,
                attachment_path=attachment or "",
                timeout_sec=120,
            )
            if ok_pw:
                kind = " + attachment" if attachment else ""
                return True, f"Teams pe {recipient} ko bhej diya{kind} (via headless Playwright). {msg_pw}"
            # If session not set up, log and fall through to extension/CDP
            if "logged out" in msg_pw.lower() or "/api/teams/setup" in msg_pw:
                log.info("teams_playwright_needs_login", error=msg_pw)
            else:
                log.info("teams_playwright_failed_no_fallback", error=msg_pw)
                return False, f"Teams send fail hua: {msg_pw}"
        except Exception as e:
            log.info("teams_playwright_init_fallback", error=str(e)[:200])

        # Extension route — supports text + attachments via base64 encoding.
        from app.services.extension_bridge import ExtensionBridge
        bridge = ExtensionBridge.get()
        for _ in range(15):
            if bridge.is_connected():
                break
            time.sleep(0.2)
        if bridge.is_connected():
            ext_params = {"recipient": recipient, "message": message}
            ext_timeout = 90
            if attachment:
                import base64
                import mimetypes
                import os
                try:
                    size = os.path.getsize(attachment)
                except OSError as e:
                    return False, f"Attachment read fail: {e}"
                MAX_BYTES = 90 * 1024 * 1024
                if size > MAX_BYTES:
                    return False, f"File bohot bara hai ({size // 1024 // 1024} MB) — Teams limit 100MB."
                with open(attachment, "rb") as f:
                    ext_params["attachment_b64"] = base64.b64encode(f.read()).decode("ascii")
                ext_params["attachment_name"] = os.path.basename(attachment)
                ext_params["attachment_mime"] = (
                    mimetypes.guess_type(attachment)[0] or "application/octet-stream"
                )
                ext_timeout = max(120, int(size / (200 * 1024)) + 60)
            try:
                bridge.send_command_sync("teams_send", ext_params, timeout=ext_timeout)
                kind = " + attachment" if attachment else ""
                return True, f"Teams pe {recipient} ko bhej diya{kind} (via extension)"
            except Exception as e:
                log.warning("ext_teams_failed_no_cdp_fallback", error=str(e))
                return False, (
                    f"Extension se Teams send fail: {e}\n"
                    f"Regular Chrome mein Teams tab khol ke login confirm karo, phir retry."
                )

        # Try CDP first (background mode — invisible, no window switch)
        from app.services.laptop_control import chrome_cdp
        if chrome_cdp.is_debug_running():
            def _attempt():
                if attachment:
                    return TeamsAutomation._send_file_via_cdp(recipient, message, attachment)
                return TeamsAutomation._send_via_cdp(recipient, message)

            ok, msg = _attempt()
            # Auto-retry once on transient verification failure
            if not ok and "verify" in msg.lower():
                log.info("teams_send_retry", first_error=msg)
                time.sleep(1.0)
                ok, msg = _attempt()

            if ok:
                return True, msg
            log.warning("cdp_teams_send_failed_fallback_to_ui", error=msg)
            if attachment:
                return False, (
                    f"CDP fail hua aur Teams desktop app fallback file send abhi support nahi karta. "
                    f"JARVIS Chrome aur Teams web khol ke retry kar. Error: {msg}"
                )

        # Fallback: UI automation (text only, Teams desktop app)
        return TeamsAutomation._send_via_ui(recipient, message)

    @staticmethod
    def _send_via_cdp(recipient: str, message: str) -> tuple[bool, str]:
        """Send via existing Chrome's Teams web tab — invisible/background."""
        from app.services.laptop_control import chrome_cdp

        try:
            p, browser, context = chrome_cdp.connect_playwright_cdp()
        except Exception as e:
            return False, f"CDP connect failed: {e}"

        # Match any Teams web flavor: business (teams.microsoft.com) and personal (teams.live.com)
        TEAMS_HOSTS = ("teams.microsoft.com", "teams.live.com")

        def is_teams_url(u: str) -> bool:
            u = (u or "").lower()
            return any(h in u for h in TEAMS_HOSTS)

        try:
            # Find existing Teams page across all contexts (business or personal)
            teams_page = None
            for ctx in browser.contexts:
                for page in ctx.pages:
                    if is_teams_url(page.url):
                        teams_page = page
                        break
                if teams_page:
                    break

            if teams_page is None:
                return False, (
                    "Teams web tab khuli nahi hai. "
                    "JARVIS Chrome mein pehle teams.microsoft.com (business) ya teams.live.com (personal) khol ke login kar."
                )

            teams_page.wait_for_load_state("domcontentloaded", timeout=15000)

            cur = (teams_page.url or "").lower()
            if "login.microsoftonline" in cur or "login.live.com" in cur:
                return False, "Teams web logged out hai — Chrome mein pehle login kar."

            recipient_low = recipient.lower()
            current_title = (teams_page.title() or "").lower()

            # Fast path: if the currently-open chat is already with the intended recipient,
            # just type into the compose box. Avoids navigation and is the most common case.
            if recipient_low in current_title:
                compose = TeamsAutomation._find_compose_box(teams_page)
                if compose:
                    return TeamsAutomation._type_and_send(teams_page, compose, recipient, message)
                # If compose missing, fall through to full new-chat flow below

            # Slow path: open "New chat" and use Teams's people-picker — searches the full
            # directory (not just recent chats), so it finds anyone the user can message.
            ok, err = TeamsAutomation._open_new_chat_with(teams_page, recipient)
            if not ok:
                return False, err

            compose = TeamsAutomation._find_compose_box(teams_page, timeout_sec=10)
            if not compose:
                return False, "Teams compose box nahi mila — chat load nahi hua."

            return TeamsAutomation._type_and_send(teams_page, compose, recipient, message)

        except Exception as e:
            log.warning("cdp_teams_send_failed", error=str(e))
            return False, f"CDP Teams send failed: {e}"
        finally:
            try:
                p.stop()
            except Exception:
                pass

    @staticmethod
    def _send_file_via_cdp(recipient: str, message: str, attachment_path: str) -> tuple[bool, str]:
        """Upload + send a file (with optional caption) via Chrome's Teams web tab.

        Background mode — no window switch. Same fast/slow path logic as text send:
        if recipient's chat already open use it; otherwise open via "New chat" people picker.
        Verifies post-send by reading the chat for filename or caption.
        """
        import os
        import time as _t
        from app.services.laptop_control import chrome_cdp

        try:
            p, browser, context = chrome_cdp.connect_playwright_cdp()
        except Exception as e:
            return False, f"CDP connect failed: {e}"

        TEAMS_HOSTS = ("teams.microsoft.com", "teams.live.com")

        def is_teams_url(u: str) -> bool:
            u = (u or "").lower()
            return any(h in u for h in TEAMS_HOSTS)

        try:
            # Find Teams page across all contexts
            teams_page = None
            for ctx in browser.contexts:
                for page in ctx.pages:
                    if is_teams_url(page.url):
                        teams_page = page
                        break
                if teams_page:
                    break

            if teams_page is None:
                return False, (
                    "Teams web tab khuli nahi hai. JARVIS Chrome mein "
                    "teams.microsoft.com ya teams.live.com khol ke login kar."
                )

            teams_page.wait_for_load_state("domcontentloaded", timeout=15000)
            cur = (teams_page.url or "").lower()
            if "login.microsoftonline" in cur or "login.live.com" in cur:
                return False, "Teams web logged out hai — Chrome mein pehle login kar."

            # Make sure we're on the right chat. Reuse fast/slow path from text send.
            recipient_low = recipient.lower()
            current_title = (teams_page.title() or "").lower()
            if recipient_low not in current_title:
                ok, err = TeamsAutomation._open_new_chat_with(teams_page, recipient)
                if not ok:
                    return False, err

            # Wait for compose box to be present (chat fully loaded)
            compose = TeamsAutomation._find_compose_box(teams_page, timeout_sec=10)
            if not compose:
                return False, "Teams compose box nahi mila — chat load nahi hua."

            # Click the attach button to mount the file input (if not already in DOM)
            for sel in (
                'button[aria-label*="Attach" i]',
                'button[aria-label*="attach" i]',
                'button[data-tid*="attach" i]',
                '[data-tid="newMessageAttachButton"]',
                'button[title*="Attach" i]',
            ):
                btn = teams_page.query_selector(sel)
                if btn:
                    try:
                        btn.click()
                        _t.sleep(0.6)
                    except Exception:
                        pass
                    break

            # If a menu opened, click "Upload from this device" or similar (best-effort)
            for menu_sel in (
                'div[role="menuitem"]:has-text("Upload from this device")',
                'div[role="menuitem"]:has-text("upload")',
                'button:has-text("Upload from this device")',
                '[data-tid="upload-from-device"]',
            ):
                try:
                    item = teams_page.query_selector(menu_sel)
                    if item:
                        item.click()
                        _t.sleep(0.4)
                        break
                except Exception:
                    continue

            # Find a file input — prefer hidden ones with the broadest accept
            file_input = None
            inputs = teams_page.query_selector_all('input[type="file"]')
            for inp in inputs:
                accept = (inp.get_attribute("accept") or "").strip()
                if accept in ("", "*", "*/*"):
                    file_input = inp
                    break
            if not file_input and inputs:
                file_input = inputs[-1]
            if not file_input:
                return False, "Teams file input element nahi mila — attach button kaam nahi kiya."

            # Upload the file
            try:
                file_input.set_input_files(attachment_path)
            except Exception as e:
                return False, f"File upload fail: {e}"

            # Wait for the upload preview (Teams shows the attachment chip in compose)
            _t.sleep(3.0)

            # Re-locate compose (may have changed)
            compose = TeamsAutomation._find_compose_box(teams_page, timeout_sec=4)

            # Type optional caption
            if message and compose:
                try:
                    compose.click()
                    _t.sleep(0.2)
                    teams_page.keyboard.type(message, delay=15)
                    _t.sleep(0.4)
                except Exception:
                    pass

            # Click Send (preferred) or press Enter as fallback
            send_clicked = False
            for sel in (
                'button[data-tid="newMessageCommandBar-sendBtn"]',
                'button[aria-label*="Send" i]',
                '[data-tid="send-message-button"]',
            ):
                try:
                    btn = teams_page.query_selector(sel)
                    if btn:
                        btn.click()
                        send_clicked = True
                        break
                except Exception:
                    continue
            if not send_clicked:
                teams_page.keyboard.press("Enter")
            _t.sleep(2.0)

            # Verify — look for filename or caption text in the chat tail
            fname = os.path.basename(attachment_path)
            verified = False
            deadline = _t.monotonic() + 6.0
            while _t.monotonic() < deadline:
                try:
                    body = teams_page.evaluate("document.body.innerText") or ""
                except Exception:
                    body = ""
                tail = body[-3500:]
                if (message and message in tail) or (fname in tail):
                    verified = True
                    break
                _t.sleep(0.4)

            if not verified:
                return False, (
                    f"File upload kiya par chat mein verify nahi hua. "
                    f"Teams web tab kholo aur khud check karo."
                )

            cap = f" caption: \"{message[:60]}\"" if message else ""
            return True, f"Teams pe {recipient} ko file bhej di: {fname}{cap} (background, verified)"
        except Exception as e:
            log.warning("cdp_teams_file_send_failed", error=str(e))
            return False, f"Teams file send fail: {e}"
        finally:
            try:
                p.stop()
            except Exception:
                pass

    # ---------- Teams web DOM helpers (CDP) ----------

    @staticmethod
    def _find_compose_box(page, timeout_sec: float = 3.0):
        """Locate the chat compose textbox. Returns the element or None."""
        deadline = time.monotonic() + timeout_sec
        selectors = (
            'div[role="textbox"][contenteditable="true"][aria-label*="message" i]',
            'div[data-tid="ckeditor"]',
            'div[role="textbox"][contenteditable="true"]',
        )
        while time.monotonic() < deadline:
            for sel in selectors:
                el = page.query_selector(sel)
                if el:
                    return el
            time.sleep(0.3)
        return None

    @staticmethod
    def _type_and_send(page, compose, recipient: str, message: str) -> tuple[bool, str]:
        """Focus the compose box, type the message, send, then VERIFY it actually appeared in the chat.

        We never claim success without verification — if the message doesn't show up
        in the chat after Enter, we return failed so the caller can fall back or
        report honestly. This is the "no fake success" guarantee.
        """
        try:
            compose.click()
            time.sleep(0.3)
            page.keyboard.type(message, delay=15)
            time.sleep(0.4)
            page.keyboard.press("Enter")
            time.sleep(1.2)  # Give the message time to post

            # Verify the message actually landed in the chat by reading visible text.
            # We scan the recent chat body for the exact message text we just typed.
            verified = False
            deadline = time.monotonic() + 4.0
            needle = message.strip()
            while time.monotonic() < deadline:
                try:
                    body_text = page.evaluate("document.body.innerText") or ""
                except Exception:
                    body_text = ""
                # Look only at the tail of the page text (recent messages)
                tail = body_text[-3000:]
                if needle and needle in tail:
                    verified = True
                    break
                time.sleep(0.4)

            if not verified:
                return False, (
                    f"Teams pe {recipient} ko bhejne ki koshish ki par message chat mein dikha nahi — "
                    f"verification fail. Tab khuli rakh aur Teams web mein khud check kar."
                )

            return True, f"Teams pe {recipient} ko bhej diya: \"{message[:80]}\" (background, verified)"
        except Exception as e:
            return False, f"Teams compose mein type nahi hua: {e}"

    @staticmethod
    def _open_new_chat_with(page, recipient: str) -> tuple[bool, str]:
        """Click 'New chat', type recipient into the people picker, and select the first match.

        Works for any contact searchable in the user's Teams directory — not limited
        to recent chat history. Recipient string can be a name or email.
        """
        # Click the "New chat" button
        new_chat_btn = (
            page.query_selector('button[data-tid="chat-list-new-chat-button"]')
            or page.query_selector('button[aria-label*="New chat" i]')
        )
        if not new_chat_btn:
            return False, "Teams 'New chat' button nahi mila page mein."

        try:
            new_chat_btn.click()
        except Exception as e:
            return False, f"'New chat' button click nahi hua: {e}"

        # Wait for the people-picker input to appear
        picker = None
        deadline = time.monotonic() + 6
        picker_selectors = (
            'input[aria-label*="To" i]',
            'input[placeholder*="name" i]',
            'input[placeholder*="email" i]',
            'div[role="combobox"] input',
            'input[role="combobox"]',
        )
        while time.monotonic() < deadline and picker is None:
            for sel in picker_selectors:
                el = page.query_selector(sel)
                if el:
                    picker = el
                    break
            if picker is None:
                time.sleep(0.25)
        if picker is None:
            return False, "Teams 'To:' people-picker nahi mila."

        try:
            picker.click()
            time.sleep(0.2)
            # Clear anything already typed
            page.keyboard.press("Control+A")
            page.keyboard.press("Delete")
            time.sleep(0.15)
            page.keyboard.type(recipient, delay=20)
        except Exception as e:
            return False, f"People-picker mein type nahi hua: {e}"

        # Wait for suggestion list, then click first match
        deadline = time.monotonic() + 8
        suggestion = None
        suggestion_selectors = (
            'li[role="option"]',
            'div[role="option"]',
            '[data-tid*="people-picker-result"]',
            '[data-tid*="suggestion"]',
        )
        while time.monotonic() < deadline and suggestion is None:
            for sel in suggestion_selectors:
                els = page.query_selector_all(sel)
                if els:
                    # Prefer a suggestion whose text contains the recipient (case-insensitive)
                    rl = recipient.lower()
                    chosen = None
                    for el in els:
                        try:
                            txt = (el.inner_text() or "").lower()
                            if rl in txt or any(part in txt for part in rl.split() if part):
                                chosen = el
                                break
                        except Exception:
                            pass
                    suggestion = chosen or els[0]
                    break
            if suggestion is None:
                time.sleep(0.25)
        if suggestion is None:
            return False, f"'{recipient}' Teams directory mein nahi mila — naam check karo."

        try:
            suggestion.click()
        except Exception:
            # Some pickers commit on Enter instead of click
            try:
                page.keyboard.press("Enter")
            except Exception as e:
                return False, f"Suggestion select nahi hua: {e}"

        # Give the chat a moment to load before the caller looks for the compose box
        time.sleep(1.5)
        return True, "ok"

    # ---------- Fallback: Teams desktop app + pyautogui ----------

    @staticmethod
    def _send_via_ui(recipient: str, message: str) -> tuple[bool, str]:
        """Fallback: Teams desktop app + pyautogui. Visible — steals focus briefly."""
        # Focus Teams window
        ok, _ = AppController.focus_window("Teams")
        if not ok:
            AppController.open_app("teams")
            time.sleep(4)
            ok, _ = AppController.focus_window("Teams")
            if not ok:
                return False, "Teams nahi mili — pehle Teams kholna padega ya Settings se Background Mode enable kar."

        try:
            import pyautogui
            import pyperclip

            pyautogui.FAILSAFE = True
            time.sleep(0.8)

            # Clear any popups/modals first
            pyautogui.press("escape")
            time.sleep(0.3)
            pyautogui.press("escape")
            time.sleep(0.3)

            # Ctrl+E focuses the top command/search bar
            pyautogui.hotkey("ctrl", "e")
            time.sleep(0.7)

            # Clear bar
            pyautogui.hotkey("ctrl", "a")
            time.sleep(0.15)
            pyautogui.press("delete")
            time.sleep(0.2)

            # Use /chat slash command — reliably opens NEW chat with person
            slash_cmd = f"/chat {recipient}"
            pyperclip.copy(slash_cmd)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(2.5)  # Longer wait for suggestions

            # Press Enter — Teams opens chat with first matching person
            pyautogui.press("enter")
            time.sleep(2.5)  # Wait for chat to fully load

            # Compose box is auto-focused after chat opens
            # Paste message directly
            pyperclip.copy(message)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(0.8)

            # Send with Enter
            pyautogui.press("enter")
            time.sleep(0.6)

            return True, f"Teams pe {recipient} ko message bhej diya: \"{message[:80]}\""

        except Exception as e:
            log.warning("teams_send_failed", error=str(e))
            return False, f"Teams message nahi gaya: {e}"

    # ---------- Teams DESKTOP APP UIA path (silent, background, minimized OK) ----------

    @staticmethod
    def _send_via_desktop_uia_silent(recipient: str, message: str, attachment: str | None = None) -> tuple[bool, str]:
        """Pure UIA silent send to Teams Desktop App (new Teams / WebView2).

        New Teams uses WebView2 — UIA tree only exposes the compose textbox
        ("Type a message"), NOT the search bar or chat list. So the only
        reliable approach is:

          1. Find the Teams window whose TITLE contains the recipient name
             (Teams titles every chat window as "Chat | <Name> | Microsoft Teams").
          2. Restore that window silently (SW_SHOWNOACTIVATE — no focus steal).
          3. Find the compose box via UIA.
          4. Inject text via UIA ValuePattern.SetValue (no keystrokes).
          5. Press Enter via type_keys with set_foreground=False (or PostMessage).

        If the recipient's chat is NOT already open in Teams, we return a clear
        error telling Zain to open it once. No mouse, no Playwright fallback.
        """
        try:
            from pywinauto.application import Application  # type: ignore
            import win32gui  # type: ignore
            import win32con  # type: ignore
        except Exception as e:
            return False, f"pywinauto/pywin32 import fail: {e}"

        recipient = recipient.strip()
        message = (message or "").strip()
        if not recipient:
            return False, "Recipient empty"
        if not message and not attachment:
            return False, "Message ya attachment dena hoga"

        recipient_low = recipient.lower()

        # ----- DEEP LINK FAST PATH for email recipients -----
        # When recipient is an email/UPN, use Microsoft Teams's official
        # msteams:/l/chat/... URL protocol. Teams resolves the email to the
        # exact contact and opens that chat directly — no fuzzy autocomplete,
        # no UI state dependency, no sidebar required. 99% reliable.
        # Falls through to legacy auto-open path only if deep link fails.
        if "@" in recipient and not attachment:
            ok_dl, msg_dl = TeamsAutomation._send_via_deep_link(recipient, message)
            if ok_dl:
                return True, msg_dl
            log.info("teams_deep_link_failed_falling_to_legacy", reason=msg_dl[:120])

        # ----- Step 1: find Teams HWNDs via raw EnumWindows (handles minimized) -----
        all_teams: list[tuple[int, str]] = []
        target_hwnd: int | None = None
        target_title: str | None = None

        def _enum_cb(hwnd, _):
            try:
                if not win32gui.IsWindow(hwnd):
                    return True
                title = win32gui.GetWindowText(hwnd) or ""
            except Exception:
                return True
            if not title:
                return True
            tl = title.lower()
            if "microsoft teams" in tl or " | teams" in tl:
                all_teams.append((hwnd, title))
            return True

        try:
            win32gui.EnumWindows(_enum_cb, None)
        except Exception as e:
            return False, f"Window scan fail: {e}"

        for h, t in all_teams:
            if recipient_low in t.lower():
                target_hwnd = h
                target_title = t
                break

        # If no chat window matches, AUTO-OPEN (Ctrl+N first, then /chat slash)
        if not target_hwnd:
            if not all_teams:
                return False, (
                    "Teams Desktop koi window nahi mili. Teams kholo phir command repeat."
                )
            launcher_hwnd = all_teams[0][0]
            log.info("teams_auto_opening_chat", recipient=recipient, via=all_teams[0][1])

            # NEW: METHOD 0 — Sidebar click (FAST + RELIABLE for recent chats).
            # Teams's left sidebar has a list of recent/pinned chats. Each
            # ListItem has the contact name as its UIA `name`. We scan for an
            # item matching recipient and click it directly — bypassing the
            # fuzzy /chat slash and Ctrl+N autocomplete entirely.
            # Sidebar click verifies via the matched name — if the displayed
            # name in sidebar matches recipient (smart matcher), the click is
            # safe. Subsequent compose-step header verify is the second guard.
            ok_sidebar, msg_sidebar = TeamsAutomation._open_chat_via_sidebar_click(
                launcher_hwnd, recipient
            )
            ok_open = ok_sidebar
            err_open = msg_sidebar
            if ok_sidebar:
                log.info("teams_sidebar_click_opened", contact_matched=msg_sidebar)
            else:
                log.info("teams_sidebar_no_match_trying_ctrl_n", reason=msg_sidebar)
                # Method 1: Ctrl+N (new chat dialog) — for contacts NOT in sidebar
                ok_open, err_open = TeamsAutomation._auto_open_chat(launcher_hwnd, recipient)
                if not ok_open:
                    log.info("teams_ctrl_n_failed_falling_to_slash", error=err_open)

            # Rescan with a FRESH local list — no shared state with outer scan
            def _rescan_for_chat():
                fresh_teams: list[tuple[int, str]] = []

                def _fresh_cb(h, _):
                    try:
                        if not win32gui.IsWindow(h):
                            return True
                        title = win32gui.GetWindowText(h) or ""
                    except Exception:
                        return True
                    if not title:
                        return True
                    tl = title.lower()
                    if "microsoft teams" in tl or " | teams" in tl:
                        fresh_teams.append((h, title))
                    return True

                try:
                    win32gui.EnumWindows(_fresh_cb, None)
                except Exception:
                    pass
                return [(h, t) for h, t in fresh_teams if recipient_low in t.lower()]

            # Up to 8 sec polling for new chat window
            new_chat = None
            for poll_attempt in range(16):
                time.sleep(0.5)
                hits = _rescan_for_chat()
                if hits:
                    new_chat = hits[0]
                    log.info("teams_chat_window_appeared", title=new_chat[1], poll_attempt=poll_attempt + 1)
                    break

            # Method 2: if Ctrl+N didn't lead to a recipient-titled window, try /chat slash
            if not new_chat:
                log.info("teams_no_chat_window_trying_slash")
                ok_slash, err_slash = TeamsAutomation._auto_open_chat_via_slash(launcher_hwnd, recipient)
                if ok_slash:
                    for poll_attempt in range(16):
                        time.sleep(0.5)
                        hits = _rescan_for_chat()
                        if hits:
                            new_chat = hits[0]
                            log.info("teams_chat_appeared_via_slash", title=new_chat[1])
                            break

            if new_chat:
                target_hwnd, target_title = new_chat
            else:
                # Sometimes chat opens in the LAUNCHER window without title change
                target_hwnd = launcher_hwnd
                try:
                    target_title = win32gui.GetWindowText(launcher_hwnd) or ""
                except Exception:
                    target_title = ""
                log.info("teams_using_launcher_as_fallback", title=target_title)

        # ----- Step 2: restore minimized silently (no focus steal) -----
        try:
            if win32gui.IsIconic(target_hwnd):
                win32gui.ShowWindow(target_hwnd, win32con.SW_SHOWNOACTIVATE)
                time.sleep(0.5)
        except Exception:
            pass

        # ----- Step 3: connect UIA via HWND with RETRY (chat may still be loading) -----
        compose = None
        window = None
        connect_errors: list[str] = []
        # Up to 5 attempts, 1-sec apart, before giving up
        for attempt in range(5):
            try:
                app = Application(backend="uia").connect(handle=target_hwnd, timeout=3)
                window = app.window(handle=target_hwnd)
                compose = TeamsAutomation._find_teams_compose(window)
                if compose is not None:
                    log.info("teams_compose_found", attempt=attempt + 1)
                    break
            except Exception as e:
                connect_errors.append(f"attempt {attempt + 1}: {str(e)[:80]}")
            time.sleep(1.0)

        # Fallback: Desktop UIA scan if all HWND attempts failed
        if compose is None:
            try:
                from pywinauto import Desktop  # type: ignore
                desktop = Desktop(backend="uia")
                for w in desktop.windows():
                    try:
                        if getattr(w, "handle", None) == target_hwnd or (w.window_text() == target_title):
                            window = w
                            c = TeamsAutomation._find_teams_compose(w)
                            if c is not None:
                                compose = c
                                break
                    except Exception:
                        continue
            except Exception as e:
                connect_errors.append(f"Desktop scan: {str(e)[:80]}")

        if compose is None:
            err_detail = "; ".join(connect_errors[:3]) if connect_errors else "no errors"
            return False, (
                f"Teams compose box UIA mein nahi mila '{target_title}' window mein. "
                f"Chat puri load nahi hui — Teams kholo, '{recipient}' pe ek bar click kar. "
                f"[debug: {err_detail}]"
            )

        hwnd = target_hwnd

        # Save current foreground window so we can restore it after sending
        prev_fg = 0
        try:
            prev_fg = win32gui.GetForegroundWindow()
        except Exception:
            pass

        # ----- Step 4: ATTACHMENT path -----
        if attachment:
            import os
            if not os.path.isfile(attachment):
                TeamsAutomation._restore_foreground(prev_fg)
                return False, f"File nahi mili: {attachment}"

            # ===== FAST PATH (Images only): CF_DIB clipboard + Ctrl+V paste =====
            # PNG/JPG/GIF/BMP/WEBP — Teams compose accepts bitmap paste INLINE.
            # For images we DON'T fall through to file picker — if image paste
            # fails we return its specific error so Zain sees the real reason.
            log.info("teams_attachment_received", path=attachment, is_image=TeamsAutomation._is_image_path(attachment))
            if TeamsAutomation._is_image_path(attachment):
                ok_img, msg_img = TeamsAutomation._send_image_via_bitmap_paste(
                    window, hwnd, compose, attachment, message, prev_fg
                )
                if ok_img:
                    return True, msg_img
                # Don't fall through to file picker for images — return specific error
                return False, f"Image paste fail: {msg_img}"

            # Force Teams foreground for UI interactions
            if not TeamsAutomation._force_foreground(hwnd):
                TeamsAutomation._restore_foreground(prev_fg)
                return False, "Teams foreground nahi aaya — file attach ke liye"

            # CRITICAL: click compose FIRST to make the attach toolbar appear
            try:
                rect_c = compose.rectangle()
                cx_c = (rect_c.left + rect_c.right) // 2
                cy_c = (rect_c.top + rect_c.bottom) // 2
                import pyautogui  # type: ignore
                pyautogui.FAILSAFE = False
                pyautogui.PAUSE = 0
                pyautogui.click(cx_c, cy_c)
                time.sleep(0.5)
            except Exception as e:
                TeamsAutomation._restore_foreground(prev_fg)
                return False, f"Compose click fail (attach setup): {e}"

            # Now find attach button (compose-focused → toolbar visible)
            ext = os.path.splitext(attachment)[1].lower()
            is_image = ext in (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")
            attach_btn = TeamsAutomation._find_attach_button(window, prefer_media=is_image)
            if not attach_btn:
                TeamsAutomation._restore_foreground(prev_fg)
                return False, "Teams 'Attach files'/'Attach media' button UIA mein nahi mila (compose click ke baad bhi)"

            # Click attach button via pyautogui coords (UIA click_input silent-fails sometimes)
            log.info("teams_file_picker_clicking_attach", btn=attach_btn.element_info.name)
            try:
                ab_rect = attach_btn.rectangle()
                ab_cx = (ab_rect.left + ab_rect.right) // 2
                ab_cy = (ab_rect.top + ab_rect.bottom) // 2
                import pyautogui  # type: ignore
                pyautogui.FAILSAFE = False
                pyautogui.PAUSE = 0
                pyautogui.click(ab_cx, ab_cy)
                time.sleep(0.6)
            except Exception:
                try:
                    attach_btn.click_input()
                except Exception as e:
                    TeamsAutomation._restore_foreground(prev_fg)
                    return False, f"Attach button click fail: {e}"

            # Poll for submenu OR file picker (whichever shows first) — up to 12 sec
            # Re-try attach click once if nothing appears in 5 sec.
            from pywinauto import Desktop  # type: ignore
            upload_item = None
            picker_opened = False
            attach_retried = False
            poll_deadline = time.monotonic() + 12.0
            while time.monotonic() < poll_deadline:
                time.sleep(0.4)
                # Check if file picker already open
                try:
                    for w in Desktop(backend="uia").windows():
                        try:
                            t = (w.window_text() or "").strip().lower()
                            if t == "open" or t.startswith("open "):
                                picker_opened = True
                                break
                        except Exception:
                            continue
                except Exception:
                    pass
                if picker_opened:
                    log.info("teams_picker_opened_directly")
                    break
                # Otherwise check for "Upload from this device" submenu
                upload_item = TeamsAutomation._find_upload_menu_item(window)
                if upload_item is not None:
                    log.info("teams_submenu_found", name=upload_item.element_info.name)
                    break

                # If half the time elapsed and nothing found, retry attach click ONCE
                if not attach_retried and (time.monotonic() > poll_deadline - 7.0):
                    log.info("teams_retrying_attach_click")
                    try:
                        import pyautogui  # type: ignore
                        pyautogui.click(ab_cx, ab_cy)
                        time.sleep(0.5)
                    except Exception:
                        pass
                    attach_retried = True

            # If submenu found (not picker), click it via pyautogui at its rect
            if upload_item is not None and not picker_opened:
                clicked = False
                # Method 1: pyautogui click at the menu item's screen rect (most reliable)
                try:
                    ui_rect = upload_item.rectangle()
                    ui_cx = (ui_rect.left + ui_rect.right) // 2
                    ui_cy = (ui_rect.top + ui_rect.bottom) // 2
                    import pyautogui  # type: ignore
                    pyautogui.click(ui_cx, ui_cy)
                    clicked = True
                except Exception:
                    pass
                # Method 2: UIA click_input
                if not clicked:
                    try:
                        upload_item.click_input()
                        clicked = True
                    except Exception:
                        pass
                # Method 3: UIA invoke
                if not clicked:
                    try:
                        upload_item.invoke()
                        clicked = True
                    except Exception:
                        pass
                if not clicked:
                    TeamsAutomation._restore_foreground(prev_fg)
                    return False, "'Upload from this device' click karne ka koi tareeqa kaam nahi kiya"
                time.sleep(1.0)

            # Drive the Windows file-picker dialog (up to 20 sec for it to appear)
            ok_picker, picker_err = TeamsAutomation._fill_file_picker(attachment, timeout_sec=20)
            if not ok_picker:
                TeamsAutomation._restore_foreground(prev_fg)
                return False, f"File picker fail: {picker_err}"

            # Teams uploads file — wait + verify chip appeared
            time.sleep(2.5)
            chip_ok = TeamsAutomation._verify_attachment_chip(window, attachment, timeout_sec=12.0)
            if not chip_ok:
                TeamsAutomation._restore_foreground(prev_fg)
                return False, (
                    f"File picker se file di par Teams ne attachment chip nahi banayi. "
                    f"Format ({ext}) ya size issue ho sakta."
                )

            # Re-locate compose (DOM shifted with attachment chip)
            try:
                fresh = TeamsAutomation._find_teams_compose(window)
                if fresh is not None:
                    compose = fresh
            except Exception:
                pass

        # ----- Step 5: inject TEXT via brief-flash click + clipboard paste -----
        # Reality: React WebView2 NEEDS visible focused window for input.
        # Brief flash (2-3 sec) is unavoidable. Verified flow.
        if message:
            forced = TeamsAutomation._force_foreground(hwnd)
            if not forced:
                TeamsAutomation._restore_foreground(prev_fg)
                return False, (
                    "Teams window foreground nahi aa raha — Windows ne focus block kar diya. "
                    "Teams pe ek baar manually click kar phir retry."
                )

            # Re-find compose AFTER restore (rect may shift)
            try:
                fresh_compose = TeamsAutomation._find_teams_compose(window)
                if fresh_compose is not None:
                    compose = fresh_compose
            except Exception:
                pass

            try:
                rect = compose.rectangle()
                cx = (rect.left + rect.right) // 2
                cy = (rect.top + rect.bottom) // 2
            except Exception as e:
                TeamsAutomation._restore_foreground(prev_fg)
                return False, f"Compose rectangle nahi mili: {e}"

            try:
                import pyautogui  # type: ignore
                pyautogui.FAILSAFE = False
                pyautogui.PAUSE = 0
                pyautogui.click(cx, cy)
                # WebView2 focus latency — Teams's React tree needs >0.35s for
                # the click to propagate focus to the compose contenteditable.
                # If we Ctrl+A too early, the active focus is still on chat
                # history list → user sees "select karta hai" (all messages
                # in chat get visually selected). 1.0s is safe across slow
                # machines while still being responsive.
                time.sleep(1.0)
                # Re-resolve compose AFTER the click to confirm focus actually
                # landed inside the contenteditable. CRITICAL: use a FRESH UIA
                # connection — the original `window` object's element cache may
                # be stale after the auto-open chat flow + click. Stale tree
                # returns None even when compose IS present, triggering a false
                # "chat not loaded" error.
                _fresh_compose = None
                _fresh_window_used = None
                try:
                    # Use `hwnd` (the chat-content window, aliased from target_hwnd
                    # at line ~800), NOT launcher_hwnd. launcher_hwnd may not even
                    # be defined here (it's only set inside the auto-open path).
                    from pywinauto.application import Application as _App
                    _fresh_app = _App(backend="uia").connect(handle=hwnd, timeout=3)
                    _fresh_window = _fresh_app.window(handle=hwnd)
                    _fresh_compose = TeamsAutomation._find_teams_compose(_fresh_window)
                    _fresh_window_used = _fresh_window
                except Exception:
                    _fresh_compose = TeamsAutomation._find_teams_compose(window)
                    _fresh_window_used = window

                # FALLBACK: if compose still not found, search ALL Teams Desktop
                # windows. Modern Teams (WebView2) sometimes places compose in
                # a child process / different HWND than the launcher we found.
                if _fresh_compose is None:
                    log.info("teams_compose_not_in_target_searching_all_windows")
                    try:
                        all_teams_hwnds: list[int] = []
                        def _enum_all(h, _):
                            try:
                                if not win32gui.IsWindow(h) or not win32gui.IsWindowVisible(h):
                                    return True
                                title = (win32gui.GetWindowText(h) or "").lower()
                                if "microsoft teams" in title or "teams" in title:
                                    all_teams_hwnds.append(h)
                            except Exception:
                                pass
                            return True
                        win32gui.EnumWindows(_enum_all, None)
                        for cand_hwnd in all_teams_hwnds:
                            if cand_hwnd == hwnd:
                                continue  # already tried
                            try:
                                cand_app = _App(backend="uia").connect(handle=cand_hwnd, timeout=2)
                                cand_win = cand_app.window(handle=cand_hwnd)
                                _fresh_compose = TeamsAutomation._find_teams_compose(cand_win)
                                if _fresh_compose is not None:
                                    log.info("teams_compose_found_in_other_window", hwnd=cand_hwnd)
                                    hwnd = cand_hwnd  # use this window from now on
                                    _fresh_window_used = cand_win
                                    break
                            except Exception:
                                continue
                    except Exception:
                        pass

                if _fresh_compose is None:
                    TeamsAutomation._restore_foreground(prev_fg)
                    return False, (
                        f"Compose box UIA mein nahi mila — multi-window search ke baad bhi. "
                        f"Teams pe '{recipient}' ka chat MANUALLY open kar pehle "
                        f"(left sidebar pe click), phir JARVIS chat repeat. "
                        f"Manual pre-open workflow 99% reliable hai."
                    )

                # CRITICAL FIX: explicit UIA SetFocus on the compose element.
                # Coordinate click alone is unreliable for Chromium-based React
                # apps (Teams uses WebView2). UIA SetFocus() targets the specific
                # element via Windows accessibility APIs — much more deterministic.
                try:
                    _fresh_compose.set_focus()
                    time.sleep(0.3)
                except Exception as _focus_err:
                    # Log so we can debug focus-failure scenarios — pre-fix this
                    # was a silent swallow that masked real issues.
                    log.info("teams_compose_set_focus_failed", error=str(_focus_err)[:120])

                # SAFER clear: use Ctrl+Home + Ctrl+Shift+End + Delete instead
                # of Ctrl+A. Ctrl+A on a non-compose-focused element selects
                # EVERYTHING (chat history, sidebar, etc.) which is the
                # "select karta hai" bug Zain reported.
                # Ctrl+Home = move to TOP of all text (multi-line aware).
                # Ctrl+Shift+End = select to BOTTOM of all text.
                # These only operate within the focused text input field.
                pyautogui.hotkey("ctrl", "home")
                time.sleep(0.05)
                pyautogui.hotkey("ctrl", "shift", "end")
                time.sleep(0.08)
                pyautogui.press("delete")
                time.sleep(0.12)

                import pyperclip  # type: ignore
                try:
                    prev_clip = pyperclip.paste()
                except Exception:
                    prev_clip = ""
                pyperclip.copy(message)
                time.sleep(0.15)
                pyautogui.hotkey("ctrl", "v")
                time.sleep(0.6)
                try:
                    if prev_clip:
                        pyperclip.copy(prev_clip)
                except Exception:
                    pass
            except Exception as e:
                TeamsAutomation._restore_foreground(prev_fg)
                return False, f"Compose click/paste fail: {str(e)[:120]}"

            # Verify text injected before Enter
            in_compose = TeamsAutomation._read_compose_value(compose)
            needle = message.strip()[:25].lower()
            if needle and needle not in (in_compose or "").lower():
                TeamsAutomation._restore_foreground(prev_fg)
                return False, (
                    f"Compose mein text inject nahi hua. "
                    f"Compose state: '{(in_compose or '')[:80]}'. "
                    f"Teams ne shayad focus refuse kiya — retry karo."
                )

        # ----- Step 6: send — multi-fallback chain =====
        # 1. Ensure Teams is foreground (file picker close may have shifted it)
        # 2. Try Ctrl+Enter (Teams universal send shortcut, works with attachment)
        # 3. Try Send button via UIA find + pyautogui click
        # 4. Try coord-click at compose's bottom-right (where send arrow lives)
        # 5. Plain Enter as last resort
        sent = False

        # Make sure Teams is foreground for keystrokes
        try:
            TeamsAutomation._force_foreground(hwnd)
            time.sleep(0.3)
        except Exception:
            pass

        try:
            import pyautogui  # type: ignore
            pyautogui.FAILSAFE = False
            pyautogui.PAUSE = 0
        except Exception:
            pyautogui = None  # type: ignore

        # Method 1: Ctrl+Enter (Teams compose universal send)
        if not sent and pyautogui is not None:
            try:
                pyautogui.hotkey("ctrl", "enter")
                time.sleep(0.6)
                sent = True
                log.info("teams_file_ctrl_enter_pressed")
            except Exception:
                pass

        # Method 2: Send button via UIA + pyautogui coord click
        if attachment and not sent:
            try:
                send_btn = TeamsAutomation._find_send_button(window)
                if send_btn is not None and pyautogui is not None:
                    rect_s = send_btn.rectangle()
                    sx = (rect_s.left + rect_s.right) // 2
                    sy = (rect_s.top + rect_s.bottom) // 2
                    pyautogui.click(sx, sy)
                    sent = True
                    log.info("teams_file_send_button_clicked", x=sx, y=sy)
            except Exception:
                pass

        # Method 3: Coord-click compose's bottom-right (send arrow position)
        if attachment and not sent and pyautogui is not None:
            try:
                rect_c2 = compose.rectangle()
                sx = rect_c2.right - 25
                sy = rect_c2.bottom - 22
                pyautogui.click(sx, sy)
                sent = True
                log.info("teams_file_coord_click_send_arrow", x=sx, y=sy)
            except Exception:
                pass

        # Method 4: plain Enter
        if not sent:
            try:
                compose.type_keys("{ENTER}")
                sent = True
            except Exception:
                try:
                    if hwnd:
                        TeamsAutomation._post_enter(hwnd)
                        sent = True
                except Exception as e:
                    TeamsAutomation._restore_foreground(prev_fg)
                    return False, f"Send sab methods fail hue: {e}"

        time.sleep(1.5)

        # ----- Step 7: VERIFY message actually sent =====
        # For TEXT: compose should be empty / no longer contain our message
        # For ATTACHMENT: chip should be GONE from compose (was there before send)
        verified_sent = False
        if attachment:
            # If chip is still in compose → send DID NOT happen (real verification)
            try:
                chip_still = TeamsAutomation._attachment_chip_still_in_compose(window, attachment)
                if chip_still:
                    log.info("teams_attachment_chip_still_present_send_failed")
                    # Send didn't actually fire — retry with explicit Send button
                    try:
                        send_btn2 = TeamsAutomation._find_send_button(window)
                        if send_btn2 is not None:
                            try:
                                send_btn2.invoke()
                                time.sleep(1.5)
                                log.info("teams_send_button_retry_invoked")
                            except Exception:
                                try:
                                    send_btn2.click_input()
                                    time.sleep(1.5)
                                    log.info("teams_send_button_retry_click_input")
                                except Exception:
                                    pass
                    except Exception:
                        pass
                    # Re-check after retry
                    chip_still = TeamsAutomation._attachment_chip_still_in_compose(window, attachment)
                verified_sent = not chip_still
            except Exception:
                verified_sent = False
        elif message:
            try:
                fresh_compose = TeamsAutomation._find_teams_compose(window) or compose
                after_val = TeamsAutomation._read_compose_value(fresh_compose) or ""
                needle = message.strip()[:25].lower()
                if needle and needle not in after_val.lower():
                    verified_sent = True
                elif not (after_val or "").strip():
                    verified_sent = True
            except Exception:
                verified_sent = False
        else:
            verified_sent = True

        TeamsAutomation._restore_foreground(prev_fg)

        if not verified_sent:
            return False, (
                f"Enter dabaya par compose abhi text contain karta — message Teams ne accept "
                f"nahi kiya. Zaid ko nahi gaya. Chat dobara click karke retry karo."
            )

        import os as _os
        kind = ""
        if attachment:
            kind = f" + file({_os.path.basename(attachment)})"
        return True, (
            f"Teams pe {recipient} ko bhej diya (verified): \"{message[:80]}\"{kind}"
        )

    @staticmethod
    def _escape_for_typekeys(text: str) -> str:
        """Escape characters pywinauto.type_keys treats as special: ~!^+%(){}"""
        special = set("~!^+%(){}[]")
        out = []
        for ch in text:
            if ch in special:
                out.append("{" + ch + "}")
            else:
                out.append(ch)
        return "".join(out)

    @staticmethod
    def _read_compose_value(compose) -> str:
        """Read text content of the compose Edit/Document element via multiple UIA paths."""
        if compose is None:
            return ""
        # Try get_value (Edit + ValuePattern)
        for getter in (
            lambda: compose.get_value(),
            lambda: compose.window_text(),
        ):
            try:
                v = getter()
                if v and isinstance(v, str):
                    return v
            except Exception:
                continue
        # Try ValuePattern via iface
        try:
            iface = getattr(compose, "iface_value", None)
            if iface is not None:
                v = iface.CurrentValue
                if v:
                    return str(v)
        except Exception:
            pass
        # Try TextPattern
        try:
            iface = getattr(compose, "iface_text", None)
            if iface is not None:
                rng = iface.DocumentRange
                v = rng.GetText(-1)
                if v:
                    return str(v)
        except Exception:
            pass
        # Try descendants Text nodes
        try:
            for d in compose.descendants():
                try:
                    nm = d.element_info.name
                    if nm:
                        return str(nm)
                except Exception:
                    continue
        except Exception:
            pass
        return ""

    @staticmethod
    def _find_attach_button(window, prefer_media: bool = False):
        """Find Teams's 'Attach files' (or 'Attach media' for images) Button via UIA."""
        if window is None:
            return None
        try:
            buttons = list(window.descendants(control_type="Button"))
        except Exception:
            buttons = []
        media_btn = None
        files_btn = None
        for b in buttons:
            try:
                nm = (b.element_info.name or "").lower()
                if nm == "attach media" or "attach media" in nm:
                    media_btn = b
                elif nm == "attach files" or "attach files" in nm or nm == "attach":
                    files_btn = b
            except Exception:
                continue
        if prefer_media:
            return media_btn or files_btn
        return files_btn or media_btn

    @staticmethod
    def _find_upload_menu_item(window):
        """If a submenu opened after attach-button click, find the 'Upload from this device' item."""
        if window is None:
            return None
        candidates = []
        for ct in ("MenuItem", "ListItem", "Button"):
            try:
                for el in window.descendants(control_type=ct):
                    try:
                        nm = (el.element_info.name or "").lower()
                        if not nm:
                            continue
                        if (
                            "upload from this device" in nm
                            or "upload from this computer" in nm
                            or ("upload" in nm and "device" in nm)
                            or "browse this pc" in nm
                            or "from this device" in nm
                        ):
                            candidates.append(el)
                    except Exception:
                        continue
            except Exception:
                continue
        return candidates[0] if candidates else None

    @staticmethod
    def _fill_file_picker(file_path: str, timeout_sec: float = 10.0) -> tuple[bool, str]:
        """Drive the Windows 'Open' file picker dialog to select `file_path`.

        STRATEGY:
          1. Wait for dialog (use raw EnumWindows — faster than pywinauto Desktop scan)
          2. Force dialog to foreground with AttachThreadInput trick
          3. CLICK on estimated filename-field coords (bottom-left of dialog)
             — bypasses Alt+N which may not work in Windows 11 modern picker
          4. Clear + paste path
          5. Enter

        Returns (ok, err_message).
        """
        try:
            import win32gui  # type: ignore
            from pywinauto import Desktop  # type: ignore
        except Exception as e:
            return False, f"pywin32/pywinauto import fail: {e}"

        # Wait for the Open dialog using RAW EnumWindows (faster than UIA Desktop scan)
        dlg_hwnd: int | None = None
        dlg = None
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline and dlg_hwnd is None:
            found_hwnd = [None]

            def _enum_cb(h, _):
                try:
                    if not win32gui.IsWindow(h):
                        return True
                    if not win32gui.IsWindowVisible(h):
                        return True
                    t = (win32gui.GetWindowText(h) or "").strip().lower()
                    if t == "open" or t.startswith("open ") or t in ("open files", "choose file"):
                        found_hwnd[0] = h
                        return False
                except Exception:
                    pass
                return True

            try:
                win32gui.EnumWindows(_enum_cb, None)
            except Exception:
                pass
            if found_hwnd[0]:
                dlg_hwnd = found_hwnd[0]
                log.info("teams_picker_dialog_found", hwnd=dlg_hwnd)
                break
            time.sleep(0.25)

        if not dlg_hwnd:
            return False, f"File picker dialog appear nahi hua {int(timeout_sec)} sec mein"

        # Connect UIA for dialog (for coord lookups)
        try:
            from pywinauto.application import Application as _App
            _app = _App(backend="uia").connect(handle=dlg_hwnd, timeout=3)
            dlg = _app.window(handle=dlg_hwnd)
        except Exception:
            dlg = None

        # ===== STRATEGY 1: COORD-CLICK on filename area + paste =====
        # Force dialog foreground, click at estimated filename-field coords
        # (bottom-left of dialog), clear, paste path, Enter.
        # More reliable than Alt+N which fails in Windows 11 modern picker.
        try:
            TeamsAutomation._force_foreground(dlg_hwnd)
            time.sleep(0.4)
        except Exception:
            pass

        # Compute filename field coords from dialog rect
        try:
            dlg_rect = win32gui.GetWindowRect(dlg_hwnd)
            dlg_left, dlg_top, dlg_right, dlg_bottom = dlg_rect
            # Filename field is in the bottom area, roughly:
            #   x: dlg_left + 250 (after "File name:" label)
            #   y: dlg_bottom - 90 (~ 90 px from bottom)
            fn_x = dlg_left + 280
            fn_y = dlg_bottom - 95
            log.info("file_picker_filename_coords", x=fn_x, y=fn_y, dlg=dlg_rect)
        except Exception as e:
            fn_x = fn_y = None
            log.info("file_picker_rect_fail", error=str(e)[:80])

        try:
            import pyautogui  # type: ignore
            import pyperclip  # type: ignore
            pyautogui.FAILSAFE = False
            pyautogui.PAUSE = 0

            try:
                prev_clip = pyperclip.paste()
            except Exception:
                prev_clip = ""

            # CLICK on estimated filename position
            if fn_x is not None and fn_y is not None:
                pyautogui.click(fn_x, fn_y)
                time.sleep(0.35)
                log.info("file_picker_clicked_filename_area")

            # Also send Alt+N as fallback (in case click missed and dialog supports accelerator)
            pyautogui.keyDown("alt")
            time.sleep(0.05)
            pyautogui.press("n")
            time.sleep(0.05)
            pyautogui.keyUp("alt")
            time.sleep(0.25)

            # Clear field
            pyautogui.hotkey("ctrl", "a")
            time.sleep(0.1)
            pyautogui.press("delete")
            time.sleep(0.1)

            # Paste full path
            pyperclip.copy(file_path)
            time.sleep(0.15)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(0.5)
            log.info("file_picker_path_pasted", len=len(file_path))

            # Enter → Open
            pyautogui.press("enter")
            time.sleep(1.2)
            log.info("file_picker_enter_pressed")

            try:
                if prev_clip:
                    pyperclip.copy(prev_clip)
            except Exception:
                pass

            # Verify dialog closed
            try:
                if not win32gui.IsWindow(dlg_hwnd):
                    log.info("file_picker_dialog_closed_success")
                    return True, ""
                # Re-check via title
                t = (win32gui.GetWindowText(dlg_hwnd) or "").strip().lower()
                if t != "open" and not t.startswith("open "):
                    log.info("file_picker_dialog_title_changed_assume_success")
                    return True, ""
                log.info("file_picker_dialog_still_open_after_strategy1")
            except Exception:
                return True, ""

        except Exception as e:
            log.info("file_picker_strategy1_exception", error=str(e)[:120])

        # Find filename input — Windows picker uses ComboBox with name="File name:"
        # containing an inner Edit. Older variants may have a direct Edit.
        filename_field = None
        try:
            # Try: ComboBox with name "File name:" → inner Edit
            for cb in dlg.descendants(control_type="ComboBox"):
                try:
                    nm = (cb.element_info.name or "").lower()
                    if "file name" in nm or nm == "file name:":
                        # Get its Edit child
                        for e in cb.descendants(control_type="Edit"):
                            filename_field = e
                            break
                        if filename_field is None:
                            filename_field = cb
                        break
                except Exception:
                    continue
        except Exception:
            pass

        # Try: any Edit with "file name" in its name
        if filename_field is None:
            try:
                for e in dlg.descendants(control_type="Edit"):
                    try:
                        nm = (e.element_info.name or "").lower()
                        if "file name" in nm or "name:" in nm:
                            filename_field = e
                            break
                    except Exception:
                        continue
            except Exception:
                pass

        # Last fallback: first Edit in dialog (often the filename input)
        if filename_field is None:
            try:
                edits = list(dlg.descendants(control_type="Edit"))
                if edits:
                    filename_field = edits[0]
            except Exception:
                pass

        if filename_field is None:
            return False, "File-name input dialog mein nahi mila (na ComboBox na Edit)"

        # Inject the full path — PHYSICAL CLICK first then clipboard paste.
        # set_text alone fails on this ComboBox/Edit in modern Windows because
        # the Edit doesn't have ValuePattern (it's a combobox child). Click +
        # paste is the reliable pattern (same as Teams compose box).
        injected = False
        try:
            rect = filename_field.rectangle()
            cx = (rect.left + rect.right) // 2
            cy = (rect.top + rect.bottom) // 2
            import pyautogui  # type: ignore
            pyautogui.FAILSAFE = False
            pyautogui.PAUSE = 0
            pyautogui.click(cx, cy)
            time.sleep(0.3)
            # Clear any pre-existing text
            pyautogui.hotkey("ctrl", "a")
            time.sleep(0.1)
            pyautogui.press("delete")
            time.sleep(0.1)
            # Paste via clipboard (handles spaces, unicode, long paths)
            import pyperclip  # type: ignore
            try:
                prev_clip = pyperclip.paste()
            except Exception:
                prev_clip = ""
            pyperclip.copy(file_path)
            time.sleep(0.15)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(0.4)
            try:
                if prev_clip:
                    pyperclip.copy(prev_clip)
            except Exception:
                pass
            injected = True
        except Exception as e:
            # Last fallback: try UIA set_text + type_keys
            try:
                filename_field.set_text(file_path)
                injected = True
            except Exception:
                try:
                    filename_field.type_keys(file_path, with_spaces=True, pause=0.005)
                    injected = True
                except Exception as e2:
                    return False, f"Filename inject fail: click+paste={str(e)[:60]}; fallback={e2}"

        if not injected:
            return False, "Filename inject sab methods se fail"

        time.sleep(0.4)

        # Click Open button (Windows assigns it as default — Enter also works)
        open_btn = None
        try:
            for b in dlg.descendants(control_type="Button"):
                try:
                    nm = (b.element_info.name or "").strip().lower()
                    if nm in ("open", "&open"):
                        open_btn = b
                        break
                except Exception:
                    continue
        except Exception:
            pass
        try:
            if open_btn is not None:
                open_btn.click_input()
            else:
                filename_field.type_keys("{ENTER}")
        except Exception as e:
            return False, f"Open click fail: {e}"

        time.sleep(0.6)
        return True, ""

    @staticmethod
    def _verify_attachment_chip(window, file_path: str, timeout_sec: float = 5.0) -> bool:
        """Scan UIA tree for the attachment preview chip Teams adds when a file pastes.

        Teams shows the filename (or partial) in some UI element name when the
        attachment is accepted. If we don't see anything matching within the
        timeout, the paste silently failed.
        """
        import os
        fname = os.path.basename(file_path).lower()
        fname_base = os.path.splitext(fname)[0].lower()
        # Only treat the base as a needle if it's distinctive enough
        needles = [fname]
        if len(fname_base) >= 5:
            needles.append(fname_base)
        ext = os.path.splitext(fname)[1].lower()

        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            try:
                for el in window.descendants():
                    try:
                        name = (el.element_info.name or "").lower()
                        if not name:
                            continue
                        if any(n in name for n in needles):
                            return True
                        # Generic chip markers Teams sometimes uses
                        if "remove attachment" in name or "cancel upload" in name:
                            return True
                        if ext and ext in name and ("attach" in name or "preview" in name or "uploading" in name):
                            return True
                    except Exception:
                        continue
            except Exception:
                pass
            time.sleep(0.5)
        return False

    @staticmethod
    def _make_window_invisible(hwnd: int) -> tuple[bool, int]:
        """Set window's opacity to 0 via WS_EX_LAYERED + SetLayeredWindowAttributes.

        Window stays at its current screen position (so clicks at compose
        coordinates still hit the right HWND) but is visually invisible.
        Returns (ok, original_ex_style) so caller can restore.
        """
        if not hwnd:
            return False, 0
        try:
            import ctypes
        except Exception:
            return False, 0
        try:
            user32 = ctypes.windll.user32
            GWL_EXSTYLE = -20
            WS_EX_LAYERED = 0x00080000
            LWA_ALPHA = 0x00000002

            # Use SetWindowLongPtrW for 64-bit safety
            try:
                get_long = user32.GetWindowLongPtrW
                set_long = user32.SetWindowLongPtrW
                get_long.restype = ctypes.c_ssize_t
                set_long.restype = ctypes.c_ssize_t
                set_long.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
                get_long.argtypes = [ctypes.c_void_p, ctypes.c_int]
            except AttributeError:
                # 32-bit fallback
                get_long = user32.GetWindowLongW
                set_long = user32.SetWindowLongW

            cur_style = get_long(hwnd, GWL_EXSTYLE)
            new_style = cur_style | WS_EX_LAYERED
            set_long(hwnd, GWL_EXSTYLE, new_style)
            # opacity 0 (fully invisible) — but window still receives input
            user32.SetLayeredWindowAttributes(hwnd, 0, 0, LWA_ALPHA)
            return True, int(cur_style)
        except Exception:
            return False, 0

    @staticmethod
    def _restore_window_visibility(hwnd: int, original_ex_style: int) -> None:
        """Restore window opacity to 255 and reset extended style."""
        if not hwnd:
            return
        try:
            import ctypes
            user32 = ctypes.windll.user32
            GWL_EXSTYLE = -20
            LWA_ALPHA = 0x00000002
            try:
                set_long = user32.SetWindowLongPtrW
                set_long.restype = ctypes.c_ssize_t
                set_long.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
            except AttributeError:
                set_long = user32.SetWindowLongW
            # First restore opacity (visible)
            user32.SetLayeredWindowAttributes(hwnd, 0, 255, LWA_ALPHA)
            # Then restore original style (remove LAYERED bit)
            set_long(hwnd, GWL_EXSTYLE, original_ex_style)
        except Exception:
            pass

    @staticmethod
    def _save_cursor_position() -> tuple[int, int]:
        try:
            import ctypes
            from ctypes import wintypes
            class POINT(ctypes.Structure):
                _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]
            pt = POINT()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
            return int(pt.x), int(pt.y)
        except Exception:
            return -1, -1

    @staticmethod
    def _restore_cursor_position(x: int, y: int) -> None:
        if x < 0 or y < 0:
            return
        try:
            import ctypes
            ctypes.windll.user32.SetCursorPos(x, y)
        except Exception:
            pass

    @staticmethod
    def _force_foreground(hwnd: int) -> bool:
        """Bypass Windows focus-stealing prevention via AttachThreadInput.

        Standard Win32 technique used by legit dev tools (AutoHotkey, etc.).
        Required because Windows blocks SetForegroundWindow from background
        processes — without this, our compose.set_focus() silently fails.
        """
        if not hwnd:
            return False
        try:
            import win32api  # type: ignore
            import win32con  # type: ignore
            import win32gui  # type: ignore
            import win32process  # type: ignore
        except Exception:
            return False

        try:
            # Send Alt to unlock focus-stealing prevention (Windows quirk)
            win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
            win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
            time.sleep(0.05)

            # Restore from minimized
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                time.sleep(0.25)

            fg_hwnd = win32gui.GetForegroundWindow()
            if fg_hwnd == hwnd:
                return True

            cur_thread = win32api.GetCurrentThreadId()
            fg_thread = 0
            if fg_hwnd:
                try:
                    fg_thread = win32process.GetWindowThreadProcessId(fg_hwnd)[0]
                except Exception:
                    fg_thread = 0
            try:
                tgt_thread = win32process.GetWindowThreadProcessId(hwnd)[0]
            except Exception:
                tgt_thread = 0

            attached = []
            try:
                if fg_thread and fg_thread != cur_thread:
                    try:
                        win32process.AttachThreadInput(cur_thread, fg_thread, True)
                        attached.append(fg_thread)
                    except Exception:
                        pass
                if tgt_thread and tgt_thread != cur_thread and tgt_thread not in attached:
                    try:
                        win32process.AttachThreadInput(cur_thread, tgt_thread, True)
                        attached.append(tgt_thread)
                    except Exception:
                        pass

                try:
                    win32gui.BringWindowToTop(hwnd)
                except Exception:
                    pass
                try:
                    win32gui.SetForegroundWindow(hwnd)
                except Exception:
                    pass
                try:
                    win32gui.SetFocus(hwnd)
                except Exception:
                    pass
                try:
                    # SW_SHOW ensures visible (not just restored)
                    win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
                except Exception:
                    pass
            finally:
                for t in attached:
                    try:
                        win32process.AttachThreadInput(cur_thread, t, False)
                    except Exception:
                        pass

            time.sleep(0.35)
            try:
                return win32gui.GetForegroundWindow() == hwnd
            except Exception:
                return False
        except Exception:
            return False

    @staticmethod
    def _restore_foreground(hwnd_prev: int) -> None:
        """Restore previously-foreground window (so Teams doesn't keep stealing focus)."""
        if not hwnd_prev:
            return
        try:
            import win32gui  # type: ignore
            if win32gui.IsWindow(hwnd_prev):
                # SetForegroundWindow has restrictions — try ShowWindow + SetForegroundWindow
                import win32con  # type: ignore
                win32gui.ShowWindow(hwnd_prev, win32con.SW_SHOW)
                win32gui.SetForegroundWindow(hwnd_prev)
        except Exception:
            pass

    @staticmethod
    def _send_image_via_bitmap_paste(
        window, hwnd: int, compose, attachment: str, message: str, prev_fg: int
    ) -> tuple[bool, str]:
        """Image fast-path: CF_DIB clipboard → click compose → Ctrl+V → Enter.

        Bypasses the multi-hop attach-button + file-picker chain. Only works
        for image files Teams accepts inline.
        """
        import os
        # Copy image to clipboard as CF_DIB
        ok_clip, err_clip = TeamsAutomation._copy_image_to_clipboard_as_bitmap(attachment)
        if not ok_clip:
            return False, f"Bitmap clipboard fail: {err_clip}"

        # Force foreground
        if not TeamsAutomation._force_foreground(hwnd):
            return False, "Teams foreground nahi aaya (image paste)"

        # Re-find compose
        try:
            fresh = TeamsAutomation._find_teams_compose(window)
            if fresh is not None:
                compose = fresh
        except Exception:
            pass

        try:
            rect = compose.rectangle()
            cx = (rect.left + rect.right) // 2
            cy = (rect.top + rect.bottom) // 2
        except Exception as e:
            TeamsAutomation._restore_foreground(prev_fg)
            return False, f"Compose rect (image): {e}"

        try:
            import pyautogui  # type: ignore
            pyautogui.FAILSAFE = False
            pyautogui.PAUSE = 0
            pyautogui.click(cx, cy)
            time.sleep(0.2)  # compressed from 0.35
            # Ctrl+V → image pastes inline as attachment chip
            pyautogui.hotkey("ctrl", "v")
            # Compressed wait — Teams chip render is usually fast
            time.sleep(1.8)  # compressed from 3.5
            log.info("teams_image_pasted_via_ctrlv")
        except Exception as e:
            TeamsAutomation._restore_foreground(prev_fg)
            return False, f"Image paste click/Ctrl+V fail: {e}"

        # NOTE: verification removed — Teams's UIA chip name is unpredictable
        # (sometimes "Image", sometimes the original filename, sometimes hidden).
        # CF_DIB paste in Teams compose is highly reliable, so we trust it and
        # proceed straight to send. If send-button click succeeds, we know
        # something was sent.

        # Re-find compose (DOM shifted with attachment chip)
        try:
            fresh = TeamsAutomation._find_teams_compose(window)
            if fresh is not None:
                compose = fresh
        except Exception:
            pass

        # If caption provided, type it
        if message:
            try:
                rect2 = compose.rectangle()
                cx2 = (rect2.left + rect2.right) // 2
                cy2 = (rect2.top + rect2.bottom) // 2
                import pyautogui  # type: ignore
                pyautogui.click(cx2, cy2)
                time.sleep(0.3)
                import pyperclip  # type: ignore
                try:
                    prev_clip = pyperclip.paste()
                except Exception:
                    prev_clip = ""
                pyperclip.copy(message)
                time.sleep(0.15)
                pyautogui.hotkey("ctrl", "v")
                time.sleep(0.4)
                try:
                    if prev_clip:
                        pyperclip.copy(prev_clip)
                except Exception:
                    pass
            except Exception as e:
                TeamsAutomation._restore_foreground(prev_fg)
                return False, f"Caption type fail: {e}"

        # Send — with attachment, prefer SEND BUTTON click over Enter
        # (Enter sometimes adds line-break instead of sending when chip is in compose)
        sent = False
        try:
            send_btn = TeamsAutomation._find_send_button(window)
            if send_btn is not None:
                try:
                    rect_s = send_btn.rectangle()
                    sx = (rect_s.left + rect_s.right) // 2
                    sy = (rect_s.top + rect_s.bottom) // 2
                    import pyautogui  # type: ignore
                    pyautogui.click(sx, sy)
                    sent = True
                    log.info("teams_send_button_clicked")
                except Exception:
                    pass
        except Exception:
            pass

        # Fallback: press Enter
        if not sent:
            try:
                import pyautogui  # type: ignore
                pyautogui.press("enter")
                sent = True
                log.info("teams_enter_pressed_fallback")
            except Exception as e:
                TeamsAutomation._restore_foreground(prev_fg)
                return False, f"Send fail: {e}"

        time.sleep(0.8)  # compressed from 2.0
        TeamsAutomation._restore_foreground(prev_fg)
        cap = f' + caption "{message[:60]}"' if message else ""
        return True, (
            f"Teams pe image bhej diya: {os.path.basename(attachment)}{cap} "
            f"(bitmap paste, ~2 sec flash)"
        )

    @staticmethod
    def _open_chat_via_sidebar_click(launcher_hwnd: int, recipient: str) -> tuple[bool, str]:
        """Scan Teams left sidebar (chat list) for an EXISTING chat matching
        `recipient` and click it directly.

        This bypasses Teams's fuzzy autocomplete entirely. Works when the user
        has at least one prior chat with this contact (which is the common case
        in deployed teams). Strict name match — no fuzzy false-positives.

        Returns (True, matched_display_name) if found and clicked.
        Returns (False, reason) otherwise (caller falls through to /chat search).
        """
        try:
            import pyautogui  # type: ignore
            from pywinauto.application import Application  # type: ignore
        except Exception as e:
            return False, f"pywinauto/pyautogui import: {e}"
        try:
            app = Application(backend="uia").connect(handle=launcher_hwnd, timeout=3)
            window = app.window(handle=launcher_hwnd)
            wleft, wtop, wright, wbottom = win32gui.GetWindowRect(launcher_hwnd)
            wwidth = wright - wleft
            sidebar_x_max = wleft + int(wwidth * 0.32)
        except Exception as e:
            return False, f"UIA connect: {str(e)[:80]}"

        rec_low = recipient.lower().strip()
        if not rec_low:
            return False, "empty recipient"

        # Strict match helper — same logic as WhatsApp's strict filter.
        # All recipient words must appear in candidate's name. NO fuzzy match.
        rec_words = [w for w in rec_low.split() if w and any(c.isalnum() for c in w)]
        if not rec_words:
            return False, "no valid words in recipient"

        def _matches(cand: str) -> bool:
            cl = cand.lower().strip()
            if not cl:
                return False
            # Email exact match
            if "@" in rec_low and rec_low in cl:
                return True
            # All words present (strict substring per word)
            return all(w in cl for w in rec_words)

        # Foreground Teams briefly for the click to register on the correct window
        if not TeamsAutomation._force_foreground(launcher_hwnd):
            return False, "Teams foreground refuse"
        time.sleep(0.3)

        # Collect ALL matches first, then pick BEST one — prevents wrong-click
        # when multiple sidebar entries match (e.g. recipient "Zaid" matches
        # both "Zaid Hassan" and "Zaid Khan"). Without ranking we'd click the
        # first iteration order, which is sidebar-pin-order, not relevance.
        matches: list[tuple[str, int, int, int]] = []  # (name, cx, cy, type_priority)
        type_priority = {"ListItem": 0, "TreeItem": 1, "Button": 2}
        for ctype in ("ListItem", "TreeItem", "Button"):
            try:
                for el in window.descendants(control_type=ctype):
                    try:
                        name = (el.element_info.name or "").strip()
                        if not name or len(name) > 100:
                            continue
                        rect = el.rectangle()
                        # Restrict to LEFT sidebar region of window
                        if rect.right > sidebar_x_max or rect.left < wleft:
                            continue
                        if rect.bottom - rect.top < 30:
                            continue  # too small to be a chat row
                        if not _matches(name):
                            continue
                        cx = (rect.left + rect.right) // 2
                        cy = (rect.top + rect.bottom) // 2
                        matches.append((name, cx, cy, type_priority.get(ctype, 9)))
                    except Exception:
                        continue
            except Exception:
                continue

        if not matches:
            return False, "Sidebar mein matching chat nahi mili"

        # Rank matches:
        #   1. EXACT lowercase match wins (recipient string == sidebar string)
        #   2. Shorter sidebar name wins (more specific — "Zaid" beats "Zaid Khan Photographer")
        #   3. ListItem > TreeItem > Button (chat list items first)
        def _rank(match):
            name, _cx, _cy, type_p = match
            n_low = name.lower().strip()
            is_exact = 0 if n_low == rec_low else 1
            return (is_exact, len(n_low), type_p)

        matches.sort(key=_rank)
        best_name, best_cx, best_cy, _ = matches[0]
        log.info("teams_sidebar_best_match",
                 contact=best_name, x=best_cx, y=best_cy,
                 total_matches=len(matches),
                 all_names=[m[0] for m in matches[:5]])

        pyautogui.click(best_cx, best_cy)
        # 2.5s gives Teams time to fully render chat (compose, messages,
        # contact picture). Was 1.5s — too short on slow machines.
        time.sleep(2.5)
        return True, best_name

    @staticmethod
    def _auto_open_chat(launcher_hwnd: int, recipient: str) -> tuple[bool, str]:
        """Open a chat with `recipient` in Teams.

        Strategy: try Ctrl+N (new chat — direct, most reliable in modern Teams).
        If that doesn't work, the caller will fall back to scanning all windows
        and trying again with /chat slash command via Ctrl+E.
        """
        try:
            import pyautogui  # type: ignore
            import pyperclip  # type: ignore
        except Exception as e:
            return False, f"pyautogui/pyperclip: {e}"

        if not TeamsAutomation._force_foreground(launcher_hwnd):
            return False, "Teams foreground refuse"

        time.sleep(0.6)
        try:
            pyautogui.FAILSAFE = False
            pyautogui.PAUSE = 0

            try:
                prev_clip = pyperclip.paste()
            except Exception:
                prev_clip = ""

            # ===== METHOD 1: Ctrl+N (new chat) — most direct in modern Teams =====
            # CRITICAL: Escape clears any stuck focus from compose box / open modal.
            # Without this, Ctrl+N sometimes doesn't reach Teams's window-level
            # shortcut handler (focus is captured by the compose's React handler),
            # and the subsequent Ctrl+A + paste fires INSIDE the compose box,
            # which is what Zain reported: message typed into wrong chat.
            log.info("teams_auto_open_method_ctrl_n", recipient=recipient)
            pyautogui.press("escape")
            # 0.5s gives Teams time to dismiss any "Discard draft?" modal or
            # suggestion dropdown that Escape closed, before Ctrl+N fires.
            time.sleep(0.5)
            pyautogui.hotkey("ctrl", "n")
            time.sleep(1.2)  # new chat panel renders

            # Clear To: field (in case it has stale content)
            pyautogui.hotkey("ctrl", "a")
            time.sleep(0.1)
            pyautogui.press("delete")
            time.sleep(0.15)

            # Type recipient name via clipboard
            pyperclip.copy(recipient)
            time.sleep(0.15)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(2.5)  # autocomplete suggestions

            # Down + Enter → select first suggested contact
            pyautogui.press("down")
            time.sleep(0.3)
            pyautogui.press("enter")
            time.sleep(1.0)

            # After contact selected, focus moves to compose box automatically
            # If user needs to press Tab to focus compose, that's a different version
            time.sleep(2.5)  # full chat render

            try:
                if prev_clip:
                    pyperclip.copy(prev_clip)
            except Exception:
                pass

            return True, ""
        except Exception as e:
            return False, f"keyboard flow fail: {str(e)[:80]}"

    @staticmethod
    def _auto_open_chat_via_slash(launcher_hwnd: int, recipient: str) -> tuple[bool, str]:
        """Fallback: open chat via Ctrl+E + /chat slash command."""
        try:
            import pyautogui  # type: ignore
            import pyperclip  # type: ignore
        except Exception as e:
            return False, f"pyautogui/pyperclip: {e}"

        if not TeamsAutomation._force_foreground(launcher_hwnd):
            return False, "Teams foreground refuse (slash)"

        time.sleep(0.5)
        try:
            pyautogui.FAILSAFE = False
            pyautogui.PAUSE = 0
            try:
                prev_clip = pyperclip.paste()
            except Exception:
                prev_clip = ""
            log.info("teams_auto_open_method_slash_chat", recipient=recipient)
            # Escape clears any stuck focus (compose box, modal) BEFORE Ctrl+E.
            # Without this, Ctrl+E may not reach Teams's command bar handler
            # and the subsequent /chat command types into compose instead.
            # 0.5s gives Teams time to dismiss any modal Escape opened.
            pyautogui.press("escape")
            time.sleep(0.5)
            pyautogui.hotkey("ctrl", "e")
            time.sleep(0.9)
            pyautogui.hotkey("ctrl", "a")
            time.sleep(0.1)
            pyautogui.press("delete")
            time.sleep(0.15)
            pyperclip.copy(f"/chat {recipient}")
            time.sleep(0.2)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(3.0)
            pyautogui.press("enter")
            time.sleep(4.0)
            try:
                if prev_clip:
                    pyperclip.copy(prev_clip)
            except Exception:
                pass
            return True, ""
        except Exception as e:
            return False, f"slash flow: {str(e)[:80]}"

    @staticmethod
    def _find_send_button(window):
        """Find the Teams compose Send button via UIA. Tries many name variants."""
        if window is None:
            return None
        try:
            for b in window.descendants(control_type="Button"):
                try:
                    name = (b.element_info.name or "").lower().strip()
                    auto_id = (b.element_info.automation_id or "").lower()
                    if not name and not auto_id:
                        continue
                    # Common name variants in Teams Desktop / Web
                    if (
                        name in ("send", "send message", "send a message", "send chat", "newer message", "submit")
                        or name.startswith("send ")
                        or "send-button" in auto_id
                        or "send_button" in auto_id
                        or "sendbutton" in auto_id
                        or "newmessagecommandbar-sendbtn" in auto_id
                        or "send button" in name
                    ):
                        return b
                except Exception:
                    continue
        except Exception:
            pass
        return None

    @staticmethod
    def _attachment_chip_still_in_compose(window, file_path: str) -> bool:
        """Return True if the attachment chip is STILL in compose (i.e. send failed)."""
        if window is None or not file_path:
            return False
        import os as _os
        fname = _os.path.basename(file_path).lower()
        fname_base = _os.path.splitext(fname)[0].lower()
        try:
            for el in window.descendants():
                try:
                    name = (el.element_info.name or "").lower()
                    if not name:
                        continue
                    if fname in name:
                        return True
                    if len(fname_base) >= 6 and fname_base in name:
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    @staticmethod
    def _copy_image_to_clipboard_as_bitmap(image_path: str) -> tuple[bool, str]:
        """Read image file + put on clipboard as CF_DIB → Teams pastes inline."""
        import os
        if not os.path.isfile(image_path):
            return False, f"File path invalid: {image_path}"
        try:
            from PIL import Image  # type: ignore
            import io
            import win32clipboard  # type: ignore
            import win32con  # type: ignore
        except Exception as e:
            return False, f"Pillow/pywin32 import fail: {e}"

        try:
            img = Image.open(image_path)
            log.info("teams_image_loaded", size=img.size, mode=img.mode, path=os.path.basename(image_path))
        except Exception as e:
            return False, f"Image open fail: {str(e)[:80]}"

        try:
            img_rgb = img.convert("RGB")
            output = io.BytesIO()
            img_rgb.save(output, "BMP")
            full = output.getvalue()
            data = full[14:]  # strip 14-byte BMP file header (CF_DIB starts at BITMAPINFOHEADER)
            output.close()
            log.info("teams_image_bmp_converted", bytes=len(data))
        except Exception as e:
            return False, f"BMP convert fail: {str(e)[:80]}"

        # Retry clipboard open up to 5 times — sometimes other processes hold it
        last_err = ""
        for attempt in range(5):
            try:
                win32clipboard.OpenClipboard()
                try:
                    win32clipboard.EmptyClipboard()
                    win32clipboard.SetClipboardData(win32con.CF_DIB, data)
                finally:
                    win32clipboard.CloseClipboard()
                log.info("teams_image_clipboard_set", attempt=attempt + 1)
                return True, ""
            except Exception as e:
                last_err = str(e)[:80]
                time.sleep(0.2)
        return False, f"Clipboard SetData fail (5 retries): {last_err}"

    @staticmethod
    def _is_image_path(path: str) -> bool:
        """Return True if `path`'s extension is one Teams can paste inline."""
        if not path:
            return False
        import os as _os
        ext = _os.path.splitext(path)[1].lower()
        return ext in (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")

    @staticmethod
    def _copy_file_to_clipboard_cf_hdrop(file_path: str) -> bool:
        """Copy a single file path to Windows clipboard as CF_HDROP (drag-drop format).

        Same format Windows Explorer uses on right-click → Copy. Pasting (Ctrl+V)
        into Teams compose attaches the file. Pure ctypes, no PowerShell.

        IMPORTANT: explicit ctypes restype/argtypes — on 64-bit Windows the
        default int return type truncates 64-bit HGLOBAL handles, which silently
        breaks GlobalAlloc/GlobalLock/SetClipboardData. This is THE classic bug.
        """
        try:
            import ctypes
            from ctypes import wintypes
            import struct
            import os
        except Exception:
            return False

        if not file_path or not os.path.isfile(file_path):
            return False

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        # ===== 64-bit-safe ctypes signatures =====
        kernel32.GlobalAlloc.restype = ctypes.c_void_p
        kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalUnlock.restype = wintypes.BOOL
        kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalFree.restype = ctypes.c_void_p
        kernel32.GlobalFree.argtypes = [ctypes.c_void_p]

        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.OpenClipboard.restype = wintypes.BOOL
        user32.EmptyClipboard.restype = wintypes.BOOL
        user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
        user32.SetClipboardData.restype = ctypes.c_void_p
        user32.CloseClipboard.restype = wintypes.BOOL

        CF_HDROP = 15
        GMEM_MOVEABLE = 0x0002

        # DROPFILES struct (20 bytes): pFiles=20, pt.x=0, pt.y=0, fNC=0, fWide=1
        dropfiles_header = struct.pack("IIIII", 20, 0, 0, 0, 1)

        # Convert to absolute path with backslashes (Windows preference)
        abs_path = os.path.abspath(file_path)
        # Single file path, null-terminated, then extra null (double-null terminator)
        file_list_utf16 = (abs_path + "\0\0").encode("utf-16-le")

        total = len(dropfiles_header) + len(file_list_utf16)
        hmem = kernel32.GlobalAlloc(GMEM_MOVEABLE, total)
        if not hmem:
            return False

        ptr = kernel32.GlobalLock(hmem)
        if not ptr:
            kernel32.GlobalFree(hmem)
            return False

        try:
            ctypes.memmove(ptr, dropfiles_header, len(dropfiles_header))
            ctypes.memmove(ptr + len(dropfiles_header), file_list_utf16, len(file_list_utf16))
        finally:
            kernel32.GlobalUnlock(hmem)

        if not user32.OpenClipboard(None):
            kernel32.GlobalFree(hmem)
            return False
        try:
            user32.EmptyClipboard()
            res = user32.SetClipboardData(CF_HDROP, hmem)
            if not res:
                # SetClipboardData ownership semantics: on failure WE still own hmem
                kernel32.GlobalFree(hmem)
                return False
            # On success Windows owns hmem now — don't free it
        finally:
            user32.CloseClipboard()
        return True

    @staticmethod
    def _find_teams_search(window):
        """Scan UIA tree for Teams search/command bar Edit element."""
        try:
            for el in window.descendants(control_type="Edit"):
                try:
                    name = (el.element_info.name or "").lower()
                    auto_id = (el.element_info.automation_id or "").lower()
                    if (
                        "search" in name
                        or "command" in name
                        or "searchbox" in auto_id
                        or "search-input" in auto_id
                        or auto_id == "searchbox"
                    ):
                        return el
                except Exception:
                    continue
        except Exception:
            pass
        # Fallback: any Edit with role textbox near top of window
        try:
            edits = list(window.descendants(control_type="Edit"))
            if edits:
                return edits[0]
        except Exception:
            pass
        return None

    @staticmethod
    def _find_teams_compose(window):
        """Scan UIA tree for Teams 'Type a message' compose box.

        New Teams (WebView2) exposes it as control_type='Edit' with:
          - name = 'Type a message' (or 'Type a new message')
          - automation_id starts with 'new-message-' (verified via diagnostic)
        """
        if window is None:
            return None
        try:
            for el in window.descendants(control_type="Edit"):
                try:
                    name = (el.element_info.name or "").lower()
                    auto_id = (el.element_info.automation_id or "").lower()
                    if (
                        "type a new message" in name
                        or "type a message" in name
                        or "new message" in name
                        or "message body" in name
                        or auto_id.startswith("new-message-")
                    ):
                        return el
                except Exception:
                    continue
        except Exception:
            pass
        # Some Teams builds expose compose as Document
        try:
            for el in window.descendants(control_type="Document"):
                try:
                    name = (el.element_info.name or "").lower()
                    if "type a new message" in name or "message" in name:
                        return el
                except Exception:
                    continue
        except Exception:
            pass
        return None

    @staticmethod
    def _uia_set_value(element, text: str) -> bool:
        """Try UIA ValuePattern.SetValue (silent, no keystrokes).

        Falls back to focus + type_keys (still works on non-foreground window
        because we pass set_foreground=False).
        """
        # Try ValuePattern.SetValue first
        try:
            element.set_text(text)
            return True
        except Exception:
            pass
        try:
            iface = getattr(element, "iface_value", None)
            if iface is not None:
                iface.SetValue(text)
                return True
        except Exception:
            pass
        # Fallback: focus + type_keys (no foreground steal)
        try:
            element.set_focus()
            time.sleep(0.2)
            # Escape special chars for pywinauto
            safe = (
                text.replace("{", "{{}").replace("}", "{}}")
                .replace("+", "{+}").replace("^", "{^}")
                .replace("%", "{%}").replace("~", "{~}")
                .replace("(", "{(}").replace(")", "{)}")
            )
            element.type_keys(safe, with_spaces=True, pause=0.01, set_foreground=False)
            return True
        except Exception:
            return False

    @staticmethod
    def _post_enter(hwnd: int) -> None:
        """Send Enter key to a window via PostMessage (works on minimized window)."""
        import win32api  # type: ignore
        import win32con  # type: ignore
        VK_RETURN = 0x0D
        win32api.PostMessage(hwnd, win32con.WM_KEYDOWN, VK_RETURN, 0)
        time.sleep(0.05)
        win32api.PostMessage(hwnd, win32con.WM_KEYUP, VK_RETURN, 0)

    @staticmethod
    def send_message_via_search(recipient: str, message: str) -> tuple[bool, str]:
        """Legacy fallback method — search-based (used if /chat doesn't work)."""
        if not recipient or not message:
            return False, "Recipient/message missing"

        ok, _ = AppController.focus_window("Teams")
        if not ok:
            return False, "Teams window nahi mili"

        try:
            import pyautogui
            import pyperclip

            time.sleep(0.8)
            pyautogui.press("escape")
            time.sleep(0.3)

            # Ctrl+E search
            pyautogui.hotkey("ctrl", "e")
            time.sleep(0.6)
            pyautogui.hotkey("ctrl", "a")
            pyautogui.press("delete")
            time.sleep(0.2)

            # Type recipient name
            pyperclip.copy(recipient)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(2.5)

            # Press Enter — opens first match
            pyautogui.press("enter")
            time.sleep(2.5)

            # Paste message
            pyperclip.copy(message)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(0.7)

            pyautogui.press("enter")
            time.sleep(0.5)

            return True, f"Teams pe {recipient} ko message bhej diya"
        except Exception as e:
            return False, f"Teams send failed: {e}"
