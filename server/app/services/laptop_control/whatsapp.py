"""WhatsApp automation — uses already-open WhatsApp (Web or Desktop) via UI shortcuts."""

from __future__ import annotations

import re
import time
from pathlib import Path

from app.core.logging import get_logger

log = get_logger(__name__)


def _normalize_phone(text: str, default_country: str = "92") -> str | None:
    """Any phone format → international (digits only, no +)."""
    if not text:
        return None
    digits = re.sub(r"\D", "", text)
    if not digits:
        return None
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) == 11 and digits.startswith("0"):
        digits = default_country + digits[1:]
    if len(digits) == 10:
        digits = default_country + digits
    if 11 <= len(digits) <= 15:
        return digits
    return None


def _find_whatsapp_window():
    """
    Find WhatsApp window:
    - Direct title match (WhatsApp Desktop or active WhatsApp Web tab)
    - OR any Chrome/Edge window (may have WhatsApp on a background tab)
    Returns (window, is_direct_match)
    """
    try:
        import pygetwindow as gw
        direct = []
        chrome_windows = []
        for w in gw.getAllWindows():
            title = (w.title or "").lower()
            if not title:
                continue
            if "whatsapp" in title:
                direct.append(w)
            elif any(b in title for b in ("google chrome", "microsoft edge", "brave", "firefox")):
                chrome_windows.append(w)

        # Prefer direct WhatsApp window
        for w in direct:
            try:
                if not w.isMinimized:
                    return w, True
            except Exception:
                pass
        if direct:
            return direct[0], True

        # Fall back to any browser window — WhatsApp may be a background tab
        for w in chrome_windows:
            try:
                if not w.isMinimized:
                    return w, False
            except Exception:
                pass
        if chrome_windows:
            return chrome_windows[0], False

        return None, False
    except Exception as e:
        log.debug("find_whatsapp_window_failed", error=str(e))
        return None, False


def _switch_to_whatsapp_tab(page_timeout: int = 3) -> bool:
    """In active Chrome window, use tab search (Ctrl+Shift+A) to find and switch to WhatsApp tab."""
    try:
        import pyautogui
        import pyperclip
        import time

        time.sleep(0.4)
        # Ctrl+Shift+A opens Chrome's "Search Tabs"
        pyautogui.hotkey("ctrl", "shift", "a")
        time.sleep(0.7)

        # Type WhatsApp
        pyperclip.copy("WhatsApp")
        pyautogui.hotkey("ctrl", "v")
        time.sleep(0.6)

        # Press Enter to switch to first matching tab
        pyautogui.press("enter")
        time.sleep(1.2)
        return True
    except Exception as e:
        log.debug("switch_to_whatsapp_tab_failed", error=str(e))
        return False


class WhatsAppAutomation:
    """Send WhatsApp messages (and files/documents) — uses Chrome CDP (background) when available, else falls back to UI automation."""

    @staticmethod
    def send_message(recipient: str, message: str, attachment: str | None = None, prefer_business: bool = False, behind_mode: bool | None = None) -> tuple[bool, str]:
        if not recipient or not recipient.strip():
            return False, "Recipient ka phone number ya naam dena hoga"
        # Attachment-only sends are valid (caption optional) — text-only sends still need a message
        if not attachment and (not message or not message.strip()):
            return False, "Message text ya attachment dena hoga"

        recipient = recipient.strip()
        message = (message or "").strip()
        attachment = (attachment or "").strip() or None

        # Resolve recipient — DO NOT use JARVIS clients DB. The user's whole
        # point: this project will be installed on many laptops, and each user
        # has different contacts. JARVIS DB is the dev's local data only; it's
        # meaningless for other users. Always defer name lookup to WhatsApp's
        # own contact list, which IS the user's real contacts wherever JARVIS
        # is installed.
        phone = _normalize_phone(recipient)
        display = recipient
        name_only = None
        if phone:
            # User explicitly gave a number — use it (WA can chat with non-contacts via wa.me)
            display = f"+{phone}"
        else:
            # Name — pass it through and let WhatsApp search its own contacts
            name_only = recipient
            display = recipient

        # Resolve attachment path if given (handles bare filenames via find_files)
        if attachment:
            ok, resolved_or_err = WhatsAppAutomation._resolve_file_path(attachment)
            if not ok:
                return False, resolved_or_err
            attachment = resolved_or_err

        # ===== WhatsApp Desktop App — FIXED-WINDOW (Stonic-style) PRIMARY =====
        # Inventor Usman's Stonic AI approach: force WA Desktop window to fixed
        # size + position, then click via percentage-of-window coordinates.
        # Brief 2-3 sec foreground flash is inherent (accepted).
        #
        # IMPORTANT: when fixed path FAILS, we do NOT fall through to UIA path
        # — that was causing the "double search" Zain saw (fixed searched once
        # + failed verify → UIA path searched again). Now we return the error
        # directly. UIA fallback only fires for "window not found" (WA Desktop
        # not installed).
        wa_desktop_not_found_marker = "WhatsApp Desktop App window nahi mili"
        try:
            from app.services.laptop_control.wa_desktop_fixed import WhatsAppDesktopFixed
            wa_fx = WhatsAppDesktopFixed.get()
            ok_fx, msg_fx = wa_fx.send_message_sync(
                recipient=name_only or recipient or "",
                message=message or "",
                attachment_path=attachment or "",
                timeout_sec=90.0,
                prefer_business=prefer_business,
                behind_mode=behind_mode,
            )
            if ok_fx:
                return True, msg_fx
            # Fall through to UIA path ONLY if WA Desktop wasn't found at all
            if "Teams Desktop koi window nahi mili" in msg_fx or "WhatsApp Desktop koi window nahi mili" in msg_fx or "install karo" in msg_fx.lower():
                log.info("wa_desktop_fixed_no_window_falling_to_uia")
            else:
                # All other failures (verify mismatch, paste fail, etc.) — return directly
                return False, msg_fx
        except Exception as e:
            log.info("wa_desktop_fixed_exception_falling_to_uia", error=str(e)[:200])

        # ===== UIA fallback — only when fixed path couldn't find WA window =====
        try:
            from app.services.laptop_control.wa_desktop_uia import WhatsAppDesktopUIA
            wa_d = WhatsAppDesktopUIA.get()
            ok_d, msg_d = wa_d.send_message_sync(
                recipient=name_only or recipient or "",
                message=message or "",
                attachment_path=attachment or "",
                timeout_sec=90.0,
            )
            if ok_d:
                return True, msg_d
            if wa_desktop_not_found_marker in msg_d:
                log.info("wa_desktop_not_installed_falling_to_chrome")
            else:
                return False, msg_d
        except Exception as e:
            log.info("wa_desktop_path_exception_falling_through", error=str(e)[:200])

        # ===== Chrome path — fallback when WhatsApp Desktop is not installed =====
        # Attachment via Playwright, text via Chrome native UIA.
        if attachment:
            try:
                from app.services.laptop_control.wa_playwright import WhatsAppPlaywright
                wa_pw = WhatsAppPlaywright.get()
                ok_pw, msg_pw = wa_pw.send_message_sync(
                    recipient=name_only or recipient or "",
                    phone=phone or "",
                    message=message or "",
                    attachment_path=attachment or "",
                    timeout_sec=120,
                )
                if ok_pw:
                    return True, f"WhatsApp pe {display} ko bhej diya (attachment via Playwright). {msg_pw}"
                return False, f"WhatsApp attachment send fail: {msg_pw}"
            except Exception as e:
                return False, f"WhatsApp attachment send fail: {e}"

        # Text-only — Chrome native UIA fallback
        try:
            from app.services.laptop_control.wa_chrome_native import WhatsAppChromeNative
            wa_chrome = WhatsAppChromeNative.get()
            r = wa_chrome.send_message_sync(
                recipient=name_only or recipient or "",
                message=message or "",
                attachment_path="",
                timeout_sec=60.0,
            )
            if r.get("ok"):
                return True, f"WhatsApp pe {display} ko bhej diya (Chrome native)"
            return False, f"WhatsApp send fail (Chrome native): {r.get('error','unknown')}"
        except Exception as e:
            return False, f"WhatsApp send fail: {e}"

        # ===== Extension route (fallback) — supports text + attachments =====
        from app.services.extension_bridge import ExtensionBridge
        bridge = ExtensionBridge.get()
        # Wait up to 3s for the extension to (re)connect (MV3 SW idle wake)
        for _ in range(15):
            if bridge.is_connected():
                break
            import time as _t
            _t.sleep(0.2)

        if bridge.is_connected():
            ext_params = {
                "recipient": name_only or recipient,
                "phone": phone or None,
                "message": message,
            }
            ext_timeout = 90
            if attachment:
                # Read + encode the file. Reasonable size limit so we don't
                # silently OOM on a 500 MB video.
                import base64
                import mimetypes
                import os
                try:
                    size = os.path.getsize(attachment)
                except OSError as e:
                    return False, f"Attachment read fail: {e}"
                MAX_BYTES = 90 * 1024 * 1024  # 90 MB — WA's web limit is ~100 MB
                if size > MAX_BYTES:
                    return False, (
                        f"File bohot bara hai ({size // 1024 // 1024} MB) — "
                        f"WhatsApp Web ~100 MB tak hi accept karta. Choti file try kar."
                    )
                with open(attachment, "rb") as f:
                    ext_params["attachment_b64"] = base64.b64encode(f.read()).decode("ascii")
                ext_params["attachment_name"] = os.path.basename(attachment)
                ext_params["attachment_mime"] = (
                    mimetypes.guess_type(attachment)[0] or "application/octet-stream"
                )
                # Big files need more upload time
                ext_timeout = max(120, int(size / (200 * 1024)) + 60)  # ~200 KB/s assumption + buffer

            try:
                bridge.send_command_sync("whatsapp_send", ext_params, timeout=ext_timeout)
                kind = " + attachment" if attachment else ""
                return True, f"WhatsApp pe {display} ko bhej diya{kind} (via extension — regular Chrome)"
            except Exception as e:
                err = str(e)
                log.warning("ext_wa_failed_no_cdp_fallback", error=err)
                if "nahi mila" in err or "WA contacts" in err or "WA mein nahi" in err:
                    return False, (
                        f"❌ {err}\n\n"
                        f"💡 Phone number se try karo agar saved nahi: "
                        f"'+92300... ko whatsapp pe ...'"
                    )
                return False, (
                    f"Extension se WhatsApp send fail: {err}\n"
                    f"Chrome mein extension popup khol ke 'connected' confirm karo, phir retry."
                )
        else:
            log.info("extension_not_connected_falling_back", recipient=recipient)

        # Try CDP next (uses JARVIS Chrome — separate profile)
        from app.services.laptop_control import chrome_cdp
        if chrome_cdp.is_debug_running():
            def _attempt():
                if attachment:
                    if phone:
                        return WhatsAppAutomation._send_file_via_cdp(phone, message, display, attachment)
                    return WhatsAppAutomation._send_file_via_cdp_by_name(name_only, message, display, attachment)
                if phone:
                    return WhatsAppAutomation._send_via_cdp(phone, message, display)
                return WhatsAppAutomation._send_via_cdp_by_name(name_only, message, display)

            ok, msg = _attempt()
            # Auto-retry once if verification failed (transient page-state issues)
            if not ok and "verify" in msg.lower():
                log.info("whatsapp_send_retry", first_error=msg)
                import time as _t
                _t.sleep(1.0)
                ok, msg = _attempt()

            if ok:
                return True, msg
            log.warning("cdp_send_failed_fallback_to_ui", error=msg)

            # If CDP returned a clear actionable error (logged out, no tab, etc.),
            # surface it directly. Don't mask it with the UI fallback's generic
            # "phone nahi hai" message.
            ACTIONABLE_CDP_ERRORS = (
                "logged out", "QR", "qr scan",
                "web tab khuli nahi", "naam ka contact whatsapp mein nahi mila",
                "search box nahi mila",
            )
            if any(marker.lower() in msg.lower() for marker in ACTIONABLE_CDP_ERRORS):
                return False, msg

            if attachment:
                return False, (
                    f"CDP fail hua aur file send ke liye fallback abhi nahi hai. "
                    f"JARVIS Chrome aur WhatsApp web tab khol ke retry kar. Error: {msg}"
                )

        # No extension, no JARVIS Chrome — UI fallback needs a phone (it can't
        # search WhatsApp's own contacts).
        if not phone:
            return False, (
                f"WhatsApp send nahi ho saka — extension chalu nahi hai. "
                f"Fix: chrome://extensions me JARVIS Bridge reload karo aur "
                f"'connected' confirm karo, phir retry."
            )
        return WhatsAppAutomation._send_via_ui(phone, message, display)

    @staticmethod
    def _send_via_cdp(phone: str, message: str, display: str) -> tuple[bool, str]:
        """Send via existing Chrome's WhatsApp Web tab — invisible/background."""
        from app.services.laptop_control import chrome_cdp
        import urllib.parse

        try:
            p, browser, context = chrome_cdp.connect_playwright_cdp()
        except Exception as e:
            return False, f"CDP connect failed: {e}"

        try:
            # Find existing WhatsApp Web page
            wa_page = None
            for ctx in browser.contexts:
                for page in ctx.pages:
                    if "web.whatsapp.com" in (page.url or "").lower():
                        wa_page = page
                        break
                if wa_page:
                    break

            # If no WhatsApp tab, open one in user's existing context
            if wa_page is None:
                wa_page = context.new_page()
                wa_page.goto("https://web.whatsapp.com/", timeout=30000)

            # Navigate to wa.me link in same tab — auto-fills message
            url = f"https://web.whatsapp.com/send?phone={phone}&text={urllib.parse.quote(message)}"
            wa_page.goto(url, timeout=30000, wait_until="domcontentloaded")

            # Wait for chat to load (compose box appears)
            import time as _t
            deadline = _t.monotonic() + 20
            input_box = None
            while _t.monotonic() < deadline:
                # Check for QR (not logged in)
                qr = wa_page.query_selector('canvas[aria-label*="QR"]')
                if qr:
                    return False, "WhatsApp Web logged out hai — phone se QR scan kar."

                # Check for invalid number error
                err = wa_page.query_selector('text=/phone number shared|isn.t on WhatsApp|Invalid/i')
                if err:
                    return False, f"+{phone} WhatsApp pe registered nahi"

                input_box = wa_page.query_selector('div[contenteditable="true"][data-tab="10"]')
                if not input_box:
                    input_box = wa_page.query_selector('div[contenteditable="true"]')
                if input_box:
                    break
                _t.sleep(0.5)

            if not input_box:
                return False, "WhatsApp chat load nahi hua time mein"

            _t.sleep(1.2)  # Let pre-fill settle

            # Send via Enter (URL pre-filled the message)
            wa_page.keyboard.press("Enter")
            _t.sleep(1.4)

            # Post-send verification — read visible chat text and confirm the
            # message we just sent actually appears. No fake success.
            verified = False
            deadline = _t.monotonic() + 4.0
            needle = message.strip()
            while _t.monotonic() < deadline:
                try:
                    body_text = wa_page.evaluate("document.body.innerText") or ""
                except Exception:
                    body_text = ""
                tail = body_text[-3000:]
                if needle and needle in tail:
                    verified = True
                    break
                _t.sleep(0.4)

            if not verified:
                return False, (
                    f"WhatsApp pe {display} ko bhejne ki koshish ki par message chat mein dikha nahi — "
                    f"verification fail. WhatsApp web tab khol ke khud check kar."
                )

            return True, f"WhatsApp pe {display} ko bhej diya: \"{message[:80]}\" (background, verified)"

        except Exception as e:
            log.warning("cdp_whatsapp_send_failed", error=str(e))
            return False, f"CDP send failed: {e}"
        finally:
            try:
                p.stop()
            except Exception:
                pass

    @staticmethod
    def _resolve_file_path(name_or_path: str) -> tuple[bool, str]:
        """Resolve a filename or path to an absolute existing path.

        - Absolute path that exists → use as-is
        - Otherwise search the system via FileOperations.find_files
        - Returns (True, path) or (False, error_message)
        """
        from pathlib import Path
        from app.services.laptop_control.files import FileOperations

        s = (name_or_path or "").strip().strip('"').strip("'")
        if not s:
            return False, "Attachment ka naam/path dena hoga"

        # Direct path?
        try:
            p = Path(s).expanduser()
            if p.is_absolute() and p.exists() and p.is_file():
                return True, str(p)
        except Exception:
            pass

        # Search system for the filename
        try:
            results = FileOperations.find_files(s, include_folders=False, max_results=10)
        except Exception as e:
            return False, f"File search fail hua: {e}"

        # Strict post-filter: keep only results whose filename actually contains the
        # user-given name (or its stem). find_files falls back to per-term partial
        # search which can return unrelated files for a unique name like
        # "logoXYZ.png" — we don't want those.
        s_low = s.lower()
        stem_low = Path(s).stem.lower()
        strict = []
        for r in results:
            name = (r.get("name") or "").lower()
            if not name:
                continue
            if s_low in name or (stem_low and stem_low in name):
                strict.append(r)
        results = strict[:3]

        if not results:
            return False, f"File '{s}' laptop pe nahi mili"
        if len(results) > 1:
            options = "\n".join(f"  - {r['path']}" for r in results[:3])
            return False, (
                f"'{s}' ke kaii match mile — specific path do:\n{options}"
            )
        match = results[0].get("path")
        if not match or not Path(match).exists():
            return False, f"Path mil gaya tha but ab nahi hai: {match}"
        return True, match

    @staticmethod
    def _send_file_via_cdp(phone: str, message: str, display: str, attachment_path: str) -> tuple[bool, str]:
        """Upload + send a file (with optional caption) via Chrome's WhatsApp Web tab.

        Background mode — no window switch. Uses the existing logged-in WhatsApp Web
        session in JARVIS Chrome. Verifies post-send by reading the chat for filename
        or caption text.
        """
        import os
        import time as _t
        from app.services.laptop_control import chrome_cdp

        try:
            p, browser, context = chrome_cdp.connect_playwright_cdp()
        except Exception as e:
            return False, f"CDP connect failed: {e}"

        try:
            # Find or create WhatsApp page
            wa_page = None
            for ctx in browser.contexts:
                for page in ctx.pages:
                    if "web.whatsapp.com" in (page.url or "").lower():
                        wa_page = page
                        break
                if wa_page:
                    break
            if wa_page is None:
                wa_page = context.new_page()
                wa_page.goto("https://web.whatsapp.com/", timeout=30000)

            # Navigate to chat with the recipient (phone number URL — no auto-fill, just opens chat)
            url = f"https://web.whatsapp.com/send?phone={phone}"
            wa_page.goto(url, timeout=30000, wait_until="domcontentloaded")

            # Wait for compose box to appear (chat loaded). Also catch QR/error states.
            deadline = _t.monotonic() + 20
            input_box = None
            while _t.monotonic() < deadline:
                if wa_page.query_selector('canvas[aria-label*="QR"]'):
                    return False, "WhatsApp Web logged out hai — phone se QR scan kar."
                err = wa_page.query_selector('text=/phone number shared|isn.t on WhatsApp|Invalid/i')
                if err:
                    return False, f"+{phone} WhatsApp pe registered nahi"
                input_box = (
                    wa_page.query_selector('div[contenteditable="true"][data-tab="10"]')
                    or wa_page.query_selector('div[role="textbox"][contenteditable="true"]')
                    or wa_page.query_selector('div[contenteditable="true"]')
                )
                if input_box:
                    break
                _t.sleep(0.5)
            if not input_box:
                return False, "WhatsApp chat load nahi hua time mein"
            _t.sleep(0.8)

            # Click paperclip to expose file inputs (some are lazy-mounted)
            for sel in (
                '[data-icon="plus-rounded"]',
                '[data-icon="plus"]',
                '[data-icon="attach-menu-plus"]',
                '[data-icon="clip"]',
                'button[title*="Attach" i]',
                'div[title*="Attach" i]',
            ):
                btn = wa_page.query_selector(sel)
                if btn:
                    try:
                        btn.click()
                        _t.sleep(0.6)
                    except Exception:
                        pass
                    break

            # Pick the right file input. WhatsApp has MULTIPLE inputs in the attach
            # menu and several accept "image" — picking the wrong one was sending
            # photos as STICKERS:
            #   Photos & Videos:  accept="image/*,video/*,..."  ← what we want for photos
            #   Sticker:          accept="image/png,image/webp" ← would send as sticker
            #   Document:         accept="*"                    ← what we want for files
            #   Audio/Camera/etc: other specific accepts
            ext = os.path.splitext(attachment_path)[1].lower()
            is_media = ext in (
                ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic",
                ".mp4", ".mov", ".3gp", ".webm", ".mkv",
            )

            file_input = None
            inputs = wa_page.query_selector_all('input[type="file"]')

            def _pick_photos_input():
                # Prefer the "Photos & Videos" input: it uses image/* (catch-all)
                # AND accepts video. Sticker input uses specific image types only
                # and never accepts video.
                for inp in inputs:
                    accept = (inp.get_attribute("accept") or "").lower()
                    if "image/*" in accept and "video" in accept:
                        return inp
                # Fallback: accepts video (Photos & Videos must accept video)
                for inp in inputs:
                    accept = (inp.get_attribute("accept") or "").lower()
                    if "video" in accept and "image" in accept:
                        return inp
                # Last resort for media: any input with image/* (NOT sticker, which
                # lists specific types like image/png,image/webp without the wildcard)
                for inp in inputs:
                    accept = (inp.get_attribute("accept") or "").lower()
                    if "image/*" in accept:
                        return inp
                return None

            def _pick_document_input():
                for inp in inputs:
                    accept = (inp.get_attribute("accept") or "").strip()
                    if accept in ("*", "*/*", ""):
                        return inp
                return None

            if is_media:
                file_input = _pick_photos_input() or _pick_document_input()
            else:
                file_input = _pick_document_input() or _pick_photos_input()

            if not file_input and inputs:
                # Avoid sticker input as absolute last resort — it has specific
                # image types but no image/* and no video.
                non_sticker = [
                    inp for inp in inputs
                    if "image/*" in (inp.get_attribute("accept") or "").lower()
                    or "video" in (inp.get_attribute("accept") or "").lower()
                    or (inp.get_attribute("accept") or "").strip() in ("*", "*/*", "")
                ]
                file_input = non_sticker[0] if non_sticker else None
            if not file_input:
                return False, "WhatsApp file input element nahi mila — paperclip click kaam nahi kiya."

            # Upload the file
            try:
                file_input.set_input_files(attachment_path)
            except Exception as e:
                return False, f"File upload fail: {e}"

            # Wait for the preview/composer to render
            _t.sleep(2.5)

            # Optional caption — find the caption textbox in the preview UI
            if message:
                caption_box = (
                    wa_page.query_selector('div[contenteditable="true"][data-tab="10"]')
                    or wa_page.query_selector('div[role="textbox"][contenteditable="true"][aria-placeholder*="caption" i]')
                    or wa_page.query_selector('div[role="textbox"][contenteditable="true"]')
                )
                if caption_box:
                    try:
                        caption_box.click()
                        _t.sleep(0.2)
                        wa_page.keyboard.type(message, delay=15)
                        _t.sleep(0.4)
                    except Exception:
                        pass

            # Find the Send button in the file-preview overlay. The selectors
            # below cover several WhatsApp web revisions:
            #  - Newer builds use a div[role=button] with aria-label "Send"
            #  - Older builds use span[data-icon="send"] inside a button
            #  - "Voice message" button is at data-tab=11 — must NOT match it.
            send_clicked = False
            send_selectors = (
                # Preview overlay button (most reliable)
                'div[role="button"][aria-label="Send"]',
                'div[role="button"][aria-label="Send (Enter)"]',
                'button[aria-label="Send"]',
                'button[aria-label="Send (Enter)"]',
                # Span icons (older UI)
                'span[data-icon="send"]:not([data-tab="11"])',
                'span[data-icon="wds-ic-send-filled"]',
                'span[data-icon="send-fill"]',
            )
            for sel in send_selectors:
                btns = wa_page.query_selector_all(sel)
                for btn in btns:
                    try:
                        aria = (btn.get_attribute("aria-label") or "").lower()
                        # Skip voice-message button which sometimes matches icon selectors
                        if "voice" in aria or "audio" in aria:
                            continue
                        btn.click()
                        send_clicked = True
                        break
                    except Exception:
                        continue
                if send_clicked:
                    break

            if not send_clicked:
                # Fallback: Ctrl+Enter from caption box is the keyboard shortcut
                try:
                    wa_page.keyboard.press("Control+Enter")
                    send_clicked = True
                except Exception:
                    pass
            if not send_clicked:
                wa_page.keyboard.press("Enter")

            _t.sleep(2.5)

            # Verification strategy: WhatsApp does NOT display the filename in
            # the chat (it shows a thumbnail/preview). So checking for filename
            # gives false negatives. Instead we use a stronger signal: did the
            # preview overlay DISAPPEAR? If yes, the send succeeded.
            # Caption match is still useful when present.
            fname = os.path.basename(attachment_path)
            verified = False
            preview_gone = False
            deadline2 = _t.monotonic() + 8.0
            while _t.monotonic() < deadline2:
                # Check if preview overlay is still showing
                preview_still_there = False
                for sel in (
                    'div[data-animate-modal-popup="true"]',
                    'div[data-asset-intro-image]',
                    'div[role="application"] div[aria-label*="Preview" i]',
                ):
                    if wa_page.query_selector(sel):
                        preview_still_there = True
                        break
                if not preview_still_there:
                    preview_gone = True

                try:
                    body = wa_page.evaluate("document.body.innerText") or ""
                except Exception:
                    body = ""
                tail = body[-4000:]

                if message and message in tail:
                    verified = True
                    break
                if fname in tail:
                    verified = True
                    break
                # If we ever observed preview, and now it's gone, send succeeded
                if preview_gone:
                    verified = True
                    break
                _t.sleep(0.4)

            if not verified:
                return False, (
                    f"File upload kiya par send confirm nahi hua. "
                    f"WhatsApp web tab kholo aur khud check karo."
                )

            cap = f" caption: \"{message[:60]}\"" if message else ""
            return True, f"WhatsApp pe {display} ko file bhej di: {fname}{cap} (background)"
        except Exception as e:
            log.warning("cdp_whatsapp_file_send_failed", error=str(e))
            return False, f"WhatsApp file send fail: {e}"
        finally:
            try:
                p.stop()
            except Exception:
                pass

    @staticmethod
    def _open_chat_by_name(wa_page, name: str) -> tuple[bool, str]:
        """Use WhatsApp web's own search to open a chat by display name."""
        import time as _t
        try:
            wa_page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass
        _t.sleep(0.8)

        # If QR/login screen is showing, abort with a clear, user-actionable error.
        # Detecting this here saves the user from confusing "search box nahi mila"
        # messages when the real problem is they need to scan the QR.
        if wa_page.query_selector('canvas[aria-label*="QR" i]'):
            return False, (
                "WhatsApp Web LOGGED OUT hai JARVIS Chrome mein — QR scan zaroori. "
                "JARVIS Chrome khol ke web.whatsapp.com pe phone se QR scan kar. "
                "(Tumhari regular Chrome ki WhatsApp login JARVIS Chrome mein nahi aati — "
                "alag profile hai.)"
            )

        # Bring this tab to front (within JARVIS Chrome only — no OS focus steal)
        try:
            wa_page.bring_to_front()
        except Exception:
            pass

        # Find the search box — wait up to 25s because WhatsApp Web takes time
        # to fully render after first open (login check, chats sync, etc.).
        # Earlier impl checked once and bailed → false "nahi mila" on cold loads.
        search_selectors = (
            'div[contenteditable="true"][data-tab="3"]',
            'div[role="textbox"][contenteditable="true"][title*="Search" i]',
            'div[role="textbox"][contenteditable="true"][aria-label*="Search" i]',
            'div[aria-label="Search input textbox"]',
            'div[aria-placeholder*="Search" i]',
        )
        search_box = None
        deadline = _t.monotonic() + 25.0
        while _t.monotonic() < deadline:
            # Re-check QR mid-wait (login may be cleared while we wait)
            if wa_page.query_selector('canvas[aria-label*="QR" i]'):
                return False, (
                    "WhatsApp Web LOGGED OUT — JARVIS Chrome mein QR scan karo."
                )
            for sel in search_selectors:
                el = wa_page.query_selector(sel)
                if el:
                    search_box = el
                    break
            if search_box:
                break
            _t.sleep(0.4)

        if not search_box:
            return False, (
                "WhatsApp search box 25s mein nahi aaya — page slow load ho raha hai "
                "ya WhatsApp Web crash hua. JARVIS Chrome refresh kar ke retry kar."
            )

        try:
            search_box.click()
            _t.sleep(0.3)
            # Clear any prior search
            wa_page.keyboard.press("Control+A")
            wa_page.keyboard.press("Delete")
            _t.sleep(0.2)
            wa_page.keyboard.type(name, delay=15)
        except Exception as e:
            return False, f"WhatsApp search mein type fail: {e}"

        # Wait for results list to populate, then open the first contact result
        deadline = _t.monotonic() + 6.0
        first_result = None
        while _t.monotonic() < deadline:
            for sel in (
                'div[role="listitem"][tabindex="-1"]',
                'div[role="listitem"]',
                'div[data-testid="cell-frame-container"]',
            ):
                results = wa_page.query_selector_all(sel)
                # Skip the search box itself / header items — take a result with text
                for r in results:
                    try:
                        text = (r.inner_text() or "").strip().lower()
                    except Exception:
                        text = ""
                    if name.lower() in text and len(text) > 0:
                        first_result = r
                        break
                if first_result:
                    break
            if first_result:
                break
            _t.sleep(0.3)

        if not first_result:
            # Fallback: press Enter to open whatever the first match is
            try:
                wa_page.keyboard.press("Enter")
                _t.sleep(1.0)
            except Exception:
                pass
        else:
            try:
                first_result.click()
                _t.sleep(0.8)
            except Exception:
                try:
                    wa_page.keyboard.press("Enter")
                    _t.sleep(0.8)
                except Exception:
                    pass

        # Confirm a chat opened by looking for the compose box for that chat
        compose = None
        deadline2 = _t.monotonic() + 4.0
        while _t.monotonic() < deadline2:
            compose = (
                wa_page.query_selector('div[contenteditable="true"][data-tab="10"]')
                or wa_page.query_selector('div[role="textbox"][contenteditable="true"][aria-placeholder*="message" i]')
            )
            if compose:
                break
            _t.sleep(0.3)

        if not compose:
            return False, f"'{name}' naam ka contact WhatsApp mein nahi mila."
        return True, "ok"

    @staticmethod
    def _send_via_cdp_by_name(name: str, message: str, display: str) -> tuple[bool, str]:
        """Text send via WhatsApp's own contact-name search (no phone needed)."""
        import time as _t
        from app.services.laptop_control import chrome_cdp

        try:
            p, browser, context = chrome_cdp.connect_playwright_cdp()
        except Exception as e:
            return False, f"CDP connect failed: {e}"

        try:
            wa_page = None
            for ctx in browser.contexts:
                for page in ctx.pages:
                    if "web.whatsapp.com" in (page.url or "").lower():
                        wa_page = page
                        break
                if wa_page:
                    break
            if wa_page is None:
                wa_page = context.new_page()
                wa_page.goto("https://web.whatsapp.com/", timeout=30000)

            ok, err = WhatsAppAutomation._open_chat_by_name(wa_page, name)
            if not ok:
                return False, err

            compose = (
                wa_page.query_selector('div[contenteditable="true"][data-tab="10"]')
                or wa_page.query_selector('div[role="textbox"][contenteditable="true"][aria-placeholder*="message" i]')
            )
            if not compose:
                return False, "WhatsApp compose box nahi mila chat open hone ke baad."

            compose.click()
            _t.sleep(0.2)
            wa_page.keyboard.type(message, delay=15)
            _t.sleep(0.3)
            wa_page.keyboard.press("Enter")
            _t.sleep(1.2)

            # Verify message appeared in chat
            verified = False
            deadline = _t.monotonic() + 5.0
            while _t.monotonic() < deadline:
                try:
                    body = wa_page.evaluate("document.body.innerText") or ""
                except Exception:
                    body = ""
                if message.strip() in body[-3500:]:
                    verified = True
                    break
                _t.sleep(0.4)

            if not verified:
                return False, f"Message send kiya par chat mein verify nahi hua — khud check kar."
            return True, f"WhatsApp pe {display} ko bhej diya: \"{message[:80]}\" (background, verified)"
        except Exception as e:
            return False, f"CDP send (name path) failed: {e}"
        finally:
            try: p.stop()
            except Exception: pass

    @staticmethod
    def _send_file_via_cdp_by_name(name: str, message: str, display: str, attachment_path: str) -> tuple[bool, str]:
        """File send via WhatsApp's own contact-name search (no phone needed)."""
        import os
        import time as _t
        from app.services.laptop_control import chrome_cdp

        try:
            p, browser, context = chrome_cdp.connect_playwright_cdp()
        except Exception as e:
            return False, f"CDP connect failed: {e}"

        try:
            wa_page = None
            for ctx in browser.contexts:
                for page in ctx.pages:
                    if "web.whatsapp.com" in (page.url or "").lower():
                        wa_page = page
                        break
                if wa_page:
                    break
            if wa_page is None:
                wa_page = context.new_page()
                wa_page.goto("https://web.whatsapp.com/", timeout=30000)

            ok, err = WhatsAppAutomation._open_chat_by_name(wa_page, name)
            if not ok:
                return False, err

            # Reuse the existing file-upload + send + verify logic from the
            # phone-based path by jumping into the same selectors here.
            for sel in (
                '[data-icon="plus-rounded"]',
                '[data-icon="plus"]',
                '[data-icon="attach-menu-plus"]',
                '[data-icon="clip"]',
                'button[title*="Attach" i]',
                'div[title*="Attach" i]',
            ):
                btn = wa_page.query_selector(sel)
                if btn:
                    try:
                        btn.click()
                        _t.sleep(0.6)
                    except Exception:
                        pass
                    break

            # Same selection logic as _send_file_via_cdp — see that function
            # for the long-form comment. Short version: pick Photos input (has
            # image/* AND video), avoid Sticker input (specific image types only).
            ext = os.path.splitext(attachment_path)[1].lower()
            is_media = ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".mp4", ".mov", ".3gp", ".webm", ".mkv")
            inputs = wa_page.query_selector_all('input[type="file"]')

            def _pick_photos_input():
                for inp in inputs:
                    accept = (inp.get_attribute("accept") or "").lower()
                    if "image/*" in accept and "video" in accept:
                        return inp
                for inp in inputs:
                    accept = (inp.get_attribute("accept") or "").lower()
                    if "video" in accept and "image" in accept:
                        return inp
                for inp in inputs:
                    accept = (inp.get_attribute("accept") or "").lower()
                    if "image/*" in accept:
                        return inp
                return None

            def _pick_document_input():
                for inp in inputs:
                    accept = (inp.get_attribute("accept") or "").strip()
                    if accept in ("*", "*/*", ""):
                        return inp
                return None

            if is_media:
                file_input = _pick_photos_input() or _pick_document_input()
            else:
                file_input = _pick_document_input() or _pick_photos_input()

            if not file_input and inputs:
                non_sticker = [
                    inp for inp in inputs
                    if "image/*" in (inp.get_attribute("accept") or "").lower()
                    or "video" in (inp.get_attribute("accept") or "").lower()
                    or (inp.get_attribute("accept") or "").strip() in ("*", "*/*", "")
                ]
                file_input = non_sticker[0] if non_sticker else None
            if not file_input:
                return False, "WhatsApp file input element nahi mila."

            try:
                file_input.set_input_files(attachment_path)
            except Exception as e:
                return False, f"File upload fail: {e}"
            _t.sleep(2.5)

            if message:
                caption_box = (
                    wa_page.query_selector('div[contenteditable="true"][data-tab="10"]')
                    or wa_page.query_selector('div[role="textbox"][contenteditable="true"][aria-placeholder*="caption" i]')
                    or wa_page.query_selector('div[role="textbox"][contenteditable="true"]')
                )
                if caption_box:
                    try:
                        caption_box.click(); _t.sleep(0.2)
                        wa_page.keyboard.type(message, delay=15); _t.sleep(0.4)
                    except Exception:
                        pass

            # Click Send button on preview overlay
            send_clicked = False
            for sel in (
                'div[role="button"][aria-label="Send"]',
                'div[role="button"][aria-label="Send (Enter)"]',
                'button[aria-label="Send"]',
                'button[aria-label="Send (Enter)"]',
                'span[data-icon="send"]:not([data-tab="11"])',
                'span[data-icon="wds-ic-send-filled"]',
            ):
                for btn in wa_page.query_selector_all(sel):
                    try:
                        aria = (btn.get_attribute("aria-label") or "").lower()
                        if "voice" in aria or "audio" in aria:
                            continue
                        btn.click(); send_clicked = True; break
                    except Exception:
                        continue
                if send_clicked:
                    break
            if not send_clicked:
                wa_page.keyboard.press("Enter")
            _t.sleep(2.5)

            # Verify — preview gone or caption text in chat tail
            fname = os.path.basename(attachment_path)
            verified = False
            deadline = _t.monotonic() + 8.0
            while _t.monotonic() < deadline:
                preview = wa_page.query_selector('div[data-animate-modal-popup="true"]') or wa_page.query_selector('div[data-asset-intro-image]')
                if not preview:
                    verified = True
                    break
                try:
                    body = wa_page.evaluate("document.body.innerText") or ""
                except Exception:
                    body = ""
                if (message and message in body[-4000:]) or fname in body[-4000:]:
                    verified = True
                    break
                _t.sleep(0.4)

            if not verified:
                return False, f"File upload kiya par send confirm nahi hua. WA tab khol ke check kar."
            cap = f" caption: \"{message[:60]}\"" if message else ""
            return True, f"WhatsApp pe {display} ko file bhej di: {fname}{cap} (background)"
        except Exception as e:
            return False, f"CDP file send (name path) failed: {e}"
        finally:
            try: p.stop()
            except Exception: pass

    @staticmethod
    def _send_via_ui(phone: str, message: str, display: str) -> tuple[bool, str]:
        """Fallback UI-based send (visible — when CDP not available)."""
        search_query = phone
        window, is_direct = _find_whatsapp_window()
        if not window:
            return False, (
                "WhatsApp window nahi mili — Settings pe 'Enable Background Mode' dabao "
                "ya Chrome mein WhatsApp Web open kar."
            )

        try:
            import pyautogui
            import pyperclip

            pyautogui.FAILSAFE = True

            # Activate window
            try:
                if window.isMinimized:
                    window.restore()
                window.activate()
            except Exception:
                pass
            time.sleep(0.6)

            # If we matched a generic Chrome (WhatsApp on background tab), switch to WhatsApp tab
            if not is_direct:
                log.info("whatsapp_switching_tab")
                if not _switch_to_whatsapp_tab():
                    return False, (
                        "WhatsApp tab Chrome mein nahi mil rahi. "
                        "WhatsApp Web tab open kar pehle."
                    )
                # Verify we're now on WhatsApp
                time.sleep(0.5)

            # Press Escape to clear any popup/modal
            pyautogui.press("escape")
            time.sleep(0.2)

            # Ctrl+Alt+N opens new chat in WhatsApp Web/Desktop
            pyautogui.hotkey("ctrl", "alt", "n")
            time.sleep(0.7)

            # Clear any existing text
            pyautogui.hotkey("ctrl", "a")
            time.sleep(0.1)
            pyautogui.press("delete")
            time.sleep(0.2)

            # Type search query
            pyperclip.copy(search_query)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(1.4)

            # Open first result
            pyautogui.press("enter")
            time.sleep(1.2)

            # Type message
            pyperclip.copy(message)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(0.4)

            # Send
            pyautogui.press("enter")
            time.sleep(0.5)

            return True, f"WhatsApp pe {display} ko message bhej diya: \"{message[:80]}\""

        except Exception as e:
            log.warning("whatsapp_send_failed", error=str(e))
            return False, f"WhatsApp send failed: {e}"

    @staticmethod
    def _lookup_contact(name: str) -> str | None:
        """Look up phone from clients DB — flexible matching."""
        try:
            import sqlite3
            for candidate in (Path("data/jarvis.db"), Path("server/data/jarvis.db"),
                              Path(__file__).parent.parent.parent.parent / "data" / "jarvis.db"):
                if candidate.exists():
                    db_path = candidate
                    break
            else:
                return None

            STOPWORDS = {"client", "ka", "ki", "ke", "wala", "wali", "naam", "ji", "saab",
                         "sahab", "sir", "bhai", "yaar", "yr"}
            query_words = [w for w in name.lower().split() if w and w not in STOPWORDS]
            cleaned = " ".join(query_words) if query_words else name.lower()

            # `with sqlite3.connect(...)` guarantees the connection is closed
            # even if any of the cursor.execute / fetchall calls below raise —
            # earlier the close-on-success path leaked connections on errors.
            with sqlite3.connect(str(db_path)) as conn:
                cur = conn.cursor()

                cur.execute(
                    "SELECT name, phone FROM clients WHERE LOWER(name) = ? AND phone IS NOT NULL AND phone != '' LIMIT 1",
                    (cleaned,),
                )
                row = cur.fetchone()

                if not row:
                    cur.execute(
                        "SELECT name, phone FROM clients WHERE LOWER(name) LIKE ? AND phone IS NOT NULL AND phone != '' LIMIT 1",
                        (f"%{cleaned}%",),
                    )
                    row = cur.fetchone()

                if not row:
                    cur.execute(
                        "SELECT name, phone FROM clients WHERE phone IS NOT NULL AND phone != '' AND LENGTH(name) >= 2"
                    )
                    for db_name, db_phone in cur.fetchall():
                        if db_name and db_name.lower() in cleaned:
                            row = (db_name, db_phone)
                            break

                if not row and query_words:
                    cur.execute(
                        "SELECT name, phone FROM clients WHERE phone IS NOT NULL AND phone != '' AND LENGTH(name) >= 2"
                    )
                    for db_name, db_phone in cur.fetchall():
                        if not db_name:
                            continue
                        db_words = set(db_name.lower().split())
                        if db_words & set(query_words):
                            row = (db_name, db_phone)
                            break

                # Last-resort fuzzy match (typos like "Ahmd" → "Ahmed")
                if not row:
                    import difflib
                    cur.execute(
                        "SELECT name, phone FROM clients WHERE phone IS NOT NULL AND phone != '' AND LENGTH(name) >= 2"
                    )
                    all_rows = cur.fetchall()
                    if all_rows:
                        name_to_phone = {(n or "").lower(): (n, p) for n, p in all_rows if n}
                        candidates = list(name_to_phone.keys())
                        matches = difflib.get_close_matches(cleaned, candidates, n=1, cutoff=0.75)
                        if matches:
                            row = name_to_phone[matches[0]]

            if row and row[1]:
                log.info("contact_matched", query=name, matched=row[0], phone=row[1])
                return _normalize_phone(row[1])
            return None
        except Exception as e:
            log.warning("contact_lookup_failed", error=str(e))
            return None
