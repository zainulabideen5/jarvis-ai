"""WhatsApp Web control via user's REGULAR Chrome — NO Playwright, NO background.

User has WhatsApp Web open in their regular Chrome. JARVIS finds that window,
activates it (foreground), uses UIA + pyautogui to drive the chat.

FOREGROUND mode — Chrome window briefly visible during action.
"""

from __future__ import annotations

import os
import subprocess
import time
from typing import Any

from app.core.logging import get_logger

log = get_logger(__name__)

_WA_TITLE_MARKERS = ("whatsapp",)
_BROWSER_TITLE_MARKERS = (
    "google chrome", "chromium", "brave", "microsoft edge", "edge",
)
# Title strings that look WA-related but are NOT the Web app — skip them.
# (Marketing pages, settings, help center, etc.)
_WA_NEGATIVE_MARKERS = (
    "whatsapp help", "whatsapp settings", "whatsapp business api",
    "whatsapp.com -",  # plain landing page
)

# Cross-module lock — prevents WA and Gmail native sends from racing on
# window activation when called concurrently.
import threading as _threading
_WINDOW_ACTIVATION_LOCK = _threading.Lock()


def _get_pyautogui():
    import pyautogui as pag
    pag.FAILSAFE = True
    pag.PAUSE = 0.1
    return pag


def _get_pwa():
    import pywinauto
    return pywinauto


def _launch_url(url: str) -> None:
    try:
        subprocess.Popen(["cmd", "/c", "start", "", url], shell=False)
    except Exception:
        import webbrowser
        webbrowser.open(url, new=2)


class WhatsAppChromeNative:
    _instance: "WhatsAppChromeNative | None" = None

    @classmethod
    def get(cls) -> "WhatsAppChromeNative":
        if cls._instance is None:
            cls._instance = WhatsAppChromeNative()
        return cls._instance

    def find_whatsapp_window(self) -> dict:
        try:
            import pygetwindow as gw
            matches = []
            for w in gw.getAllWindows():
                if not w.title:
                    continue
                t = w.title.lower()
                if any(m in t for m in _WA_TITLE_MARKERS) and any(b in t for b in _BROWSER_TITLE_MARKERS) and not any(neg in t for neg in _WA_NEGATIVE_MARKERS):
                    matches.append({"title": w.title, "minimized": w.isMinimized})
            return {"ok": bool(matches), "matches": matches, "count": len(matches)}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    def focus_whatsapp(self, auto_open: bool = True) -> dict:
        """Find + activate WhatsApp Chrome tab.

        Step 1: Direct window-title match (WA tab is ACTIVE in Chrome).
        Step 2: UIA scan Chrome tab-strip for "WhatsApp" tab (covers background tabs).
        Step 3: Auto-open if allowed (last resort).
        """
        # ----- Step 1: direct title -----
        try:
            import pygetwindow as gw
            for w in gw.getAllWindows():
                if not w.title:
                    continue
                t = w.title.lower()
                if (any(m in t for m in _WA_TITLE_MARKERS)
                        and any(b in t for b in _BROWSER_TITLE_MARKERS)
                        and not any(neg in t for neg in _WA_NEGATIVE_MARKERS)):
                    try:
                        if w.isMinimized:
                            w.restore()
                        w.activate()
                        time.sleep(0.4)
                        return {"ok": True, "title": w.title, "tier": "direct-title"}
                    except Exception:
                        return {"ok": True, "title": w.title, "note": "found but activation failed"}
        except Exception as e:
            log.debug("focus_step1_fail", error=str(e)[:120])

        # ----- Step 2: UIA tab-strip scan (WA tab is BACKGROUND in Chrome) -----
        try:
            import pygetwindow as gw
            pwa = _get_pwa()
            for cw in gw.getAllWindows():
                if not cw.title:
                    continue
                ctitle = cw.title.lower()
                if not any(b in ctitle for b in _BROWSER_TITLE_MARKERS):
                    continue
                try:
                    app = pwa.Application(backend="uia").connect(title=cw.title, timeout=4)
                    window = app.top_window()
                    for tab in window.descendants(control_type="TabItem"):
                        try:
                            tab_name = (tab.element_info.name or "").lower()
                            if ("whatsapp" in tab_name
                                    and "help" not in tab_name
                                    and "business api" not in tab_name
                                    and "settings" not in tab_name):
                                try:
                                    if cw.isMinimized:
                                        cw.restore()
                                    cw.activate()
                                except Exception:
                                    pass
                                time.sleep(0.2)
                                # Click the tab to bring it to foreground
                                try:
                                    tab.select()
                                except Exception:
                                    try:
                                        tab.click_input()
                                    except Exception:
                                        continue
                                time.sleep(0.5)
                                return {
                                    "ok": True,
                                    "title": cw.title,
                                    "tab": tab_name[:80],
                                    "tier": "uia-tab-strip",
                                }
                        except Exception:
                            continue
                except Exception:
                    continue
        except Exception as e:
            log.debug("focus_step2_fail", error=str(e)[:120])

        # ----- Step 3: auto-open as last resort -----
        if auto_open:
            try:
                import pygetwindow as gw
                _launch_url("https://web.whatsapp.com/")
                deadline = time.monotonic() + 12.0
                while time.monotonic() < deadline:
                    for w in gw.getAllWindows():
                        if not w.title:
                            continue
                        t = (w.title or "").lower()
                        if (any(m in t for m in _WA_TITLE_MARKERS)
                                and any(b in t for b in _BROWSER_TITLE_MARKERS)
                                and not any(neg in t for neg in _WA_NEGATIVE_MARKERS)):
                            try:
                                if w.isMinimized:
                                    w.restore()
                                w.activate()
                            except Exception:
                                pass
                            return {"ok": True, "title": w.title, "auto_opened": True, "tier": "auto-open"}
                    time.sleep(0.5)
            except Exception as e:
                log.debug("focus_step3_fail", error=str(e)[:120])

        return {
            "ok": False,
            "error": (
                "WhatsApp tab Chrome mein nahi mila. Chrome mein WhatsApp Web "
                "tab khol ke ek baar uss pe click kar (active rakhna zaruri nahi, "
                "but tab exist karni chahiye)."
            ),
        }

    def send_message_sync(
        self,
        recipient: str,
        message: str = "",
        attachment_path: str = "",
        timeout_sec: float = 60.0,
    ) -> dict:
        if not recipient or not recipient.strip():
            return {"ok": False, "error": "Recipient empty"}
        if not message:
            return {"ok": False, "error": "Empty message"}
        if attachment_path:
            return {"ok": False, "error": "Native mode mein attachment abhi support nahi."}

        # Cross-module window lock — prevents race when WA + Gmail sends
        # are dispatched concurrently and both call w.activate().
        with _WINDOW_ACTIVATION_LOCK:
            # DO NOT auto-open new tab — user has WA already open in Chrome.
            focused = self.focus_whatsapp(auto_open=False)
            if not focused.get("ok"):
                return focused
        log.info("wa_chrome_native_focused", title=focused.get("title", ""))
        time.sleep(1.0)

        pag = _get_pyautogui()
        pwa = _get_pwa()

        # Find + click the search input via UIA
        search_clicked = False
        try:
            app = pwa.Application(backend="uia").connect(title_re=r".*[Ww]hat[sS]?[Aa]pp.*", timeout=6)
            window = app.top_window()
            for el in window.descendants():
                try:
                    nm = (el.element_info.name or "").lower()
                    if "search or start" in nm or "search input" in nm:
                        try:
                            el.set_focus()
                            time.sleep(0.2)
                            el.click_input()
                            search_clicked = True
                            break
                        except Exception:
                            r = el.rectangle()
                            pag.click((r.left + r.right) // 2, (r.top + r.bottom) // 2)
                            search_clicked = True
                            break
                except Exception:
                    continue
        except Exception as e:
            log.debug("wa_chrome_native_uia_search_fail", error=str(e)[:120])
        if not search_clicked:
            return {"ok": False, "error": "Search box nahi mila — Chrome mein WA tab active hai?"}
        time.sleep(0.4)

        # Clear + type recipient
        pag.hotkey("ctrl", "a")
        time.sleep(0.15)
        pag.press("backspace")
        time.sleep(0.15)
        try:
            pag.typewrite(recipient, interval=0.02)
        except Exception:
            try:
                import pyperclip
                pyperclip.copy(recipient)
                pag.hotkey("ctrl", "v")
            except Exception as e:
                return {"ok": False, "error": f"Type recipient fail: {e}"}
        time.sleep(1.6)

        # ArrowDown + Enter to open top result
        pag.press("down")
        time.sleep(0.25)
        pag.press("enter")
        # Wait longer — WA chat needs time to load after navigation
        time.sleep(2.5)

        # CRITICAL — focus the compose box explicitly.
        # After opening a chat, the search box may still hold focus. We need
        # to CLICK the message area before typing. Use UIA to find the
        # "Type a message" / compose textbox.
        compose_focused = False
        try:
            app = pwa.Application(backend="uia").connect(title_re=r".*[Ww]hat[sS]?[Aa]pp.*", timeout=3)
            window = app.top_window()
            for el in window.descendants():
                try:
                    nm = (el.element_info.name or "").lower()
                    if ("type a message" in nm or "message" == nm
                            or "type your message" in nm):
                        try:
                            el.set_focus()
                            time.sleep(0.3)
                            el.click_input()
                            compose_focused = True
                            break
                        except Exception:
                            try:
                                r = el.rectangle()
                                pag.click((r.left + r.right) // 2, (r.top + r.bottom) // 2)
                                compose_focused = True
                                break
                            except Exception:
                                continue
                except Exception:
                    continue
        except Exception as e:
            log.debug("wa_compose_uia_fail", error=str(e)[:120])

        # If UIA didn't find it, try keyboard shortcut Tab to focus compose
        if not compose_focused:
            # WhatsApp Web: pressing Tab a few times eventually lands on compose.
            # OR — click near the bottom of the window where compose box lives.
            try:
                import pygetwindow as gw
                for w in gw.getAllWindows():
                    if not w.title:
                        continue
                    t = w.title.lower()
                    if "whatsapp" in t and any(b in t for b in ("chrome", "edge", "brave")):
                        # Click in the bottom-center area of the WA window where
                        # compose box typically lives (about 90% down, 50% across)
                        cx = w.left + w.width // 2
                        cy = w.top + int(w.height * 0.92)
                        pag.click(cx, cy)
                        time.sleep(0.3)
                        compose_focused = True
                        break
            except Exception:
                pass

        if not compose_focused:
            return {"ok": False, "error": "Compose box pe focus nahi ho saka. Chat khuli but message type nahi ho paya."}

        time.sleep(0.4)

        # Type message + Enter to send.
        # Unicode-safe: try clipboard FIRST (handles Urdu/Arabic), fallback
        # to typewrite for ASCII.
        typed_via_clip = False
        try:
            import pyperclip
            pyperclip.copy(message)
            pag.hotkey("ctrl", "v")
            typed_via_clip = True
        except Exception:
            pass
        if not typed_via_clip:
            try:
                pag.typewrite(message, interval=0.01)
            except Exception as e:
                return {"ok": False, "error": f"Type message fail: {e}"}
        time.sleep(0.3)
        pag.press("enter")
        time.sleep(1.5)

        # Post-send verification — poll the WA window's UIA text for the
        # message in the chat tail. WA Web renders sent messages into the
        # active chat region; if we don't see our text within ~8s, treat
        # as unverified (still ok, but flag for user).
        verified = False
        msg_strip = message.strip() if message else ""
        if msg_strip:
            try:
                for _ in range(8):
                    time.sleep(1.0)
                    try:
                        app = pwa.Application(backend="uia").connect(
                            title_re=r".*[Ww]hat[sS]?[Aa]pp.*", timeout=2,
                        )
                        window = app.top_window()
                        body = window.window_text() or ""
                        # Check last 4000 chars of the window text for the msg
                        if msg_strip in (body or "")[-4000:]:
                            verified = True
                            break
                    except Exception:
                        continue
            except Exception:
                pass

        log.info("wa_chrome_native_sent", recipient=recipient, verified=verified)
        return {
            "ok": True,
            "tier": "chrome-native",
            "recipient": recipient,
            "message_len": len(message),
            "verified": verified,
        }
