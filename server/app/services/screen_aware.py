"""Screen-Aware AI — auto-trigger screen context when user message references it.

Phase 1: On-demand.
  - Detect if a user message references the screen ("yeh / isko / yahaan / dikh raha / etc.")
  - If yes: capture screen + active-window info + ask vision LLM
  - Inject the result as extra context into the chat LLM call

The chat handler calls `enrich_with_screen_context(message)` BEFORE sending to LLM.
When triggered, the LLM gets the user's question + a brief description of what's
on screen + which app is foreground. The user never has to switch apps or
upload a screenshot — JARVIS sees it directly.

Privacy:
  - Blocklisted apps (banking, password managers) skip capture entirely
  - Capture is on-demand only — no continuous watching in Phase 1
  - No screenshot is saved to disk — bytes live in memory for the LLM call only
"""

from __future__ import annotations

import re
from typing import Any

from app.core.config import ServerConfig
from app.core.logging import get_logger
from app.services.laptop_control.vision import VisionDriver

log = get_logger(__name__)


# Words/phrases that mean "look at my screen" in Roman Urdu + English.
# Tuned for Pakistani/Indian usage — same dialect as the rest of JARVIS.
# IMPORTANT: cover BOTH "dikh" (passive — is visible) and "dekh" (active — to see/look)
# forms, since users mix them naturally: "kya dikh raha" vs "tu dekh raha".
_SCREEN_MARKERS = (
    # Roman Urdu pointers
    r"\byeh\b", r"\byaha\b", r"\byahaan\b", r"\bisko\b", r"\bisme\b", r"\bismein\b",
    r"\bidhar\b", r"\bydhar\b",
    # Direct references — both dikh/dekh and their Hindi/Urdu spellings
    r"\bscreen\b", r"\bvisible\b", r"\bshow\b", r"\bdikhao\b",
    r"\bdikh\b", r"\bdikha\b", r"\bdikhai\b", r"\bdikhta\b", r"\bdikh raha\b", r"\bdikh rha\b",
    r"\bdekh\b", r"\bdekha\b", r"\bdekhai\b", r"\bdekhta\b", r"\bdekh raha\b", r"\bdekh rha\b",
    r"\bnazar\b", r"\bnazr\b",  # "nazar a raha"
    # PDF / document / file pointers — when user references an open document
    r"\bpdf\b", r"\bdocument\b", r"\bfile khuli\b", r"\bfile khula\b", r"\bopen rakhaa\b",
    r"\bopen rakha\b", r"\bkhuli hai\b", r"\bkhula hai\b",
    # Common phrasings for "what's on screen"
    r"\bkya likha\b", r"\bkya hai\b.*\b(screen|page|window|app|pdf|file)\b",
    r"\bsamjha\b.*\b(yeh|is|pdf|file)\b",
    # Looking-at-code/error patterns
    r"\b(error|exception|warning)\b.*\b(yeh|is|isko)\b",
    r"\b(this|yeh)\b.*\b(error|code|file|line)\b",
    # "tu / tum / JARVIS dekh raha" style
    r"\b(tu|tum|tumhe|tumhain|jarvis)\b.*\b(dekh|dikh)\b",
)
_SCREEN_REGEX = re.compile("|".join(_SCREEN_MARKERS), re.IGNORECASE)


# When active window is the JARVIS dashboard itself, we want to look at what
# the user was DOING BEFORE — usually their other open app (PDF, Excel, etc.).
# These markers identify "this is JARVIS itself, look elsewhere".
# The dashboard's React app sets document.title to "AI Assistant Dashboard"
# (see App.jsx) — Chrome shows that as the window title, NOT the URL. Must
# match by the page title, not the localhost URL.
_DASHBOARD_TITLE_MARKERS = (
    "ai assistant dashboard",
    "jarvis dashboard",
    "jarvis -",
    "localhost:3000", "localhost:5173", "127.0.0.1:3000", "127.0.0.1:5173",
    "(new message)",  # tab title flashes this when bg notification fires
)


# Apps where capturing is disabled by default — sensitive content.
# Match is substring + case-insensitive against the active window title.
_BLOCKED_APP_PATTERNS = (
    "keepass", "1password", "bitwarden", "lastpass",
    "banking", "easypaisa", "jazzcash", "wallet",
    "password",
)


def is_screen_referential(message: str) -> bool:
    """True if the message likely refers to something on screen.

    Used to decide whether to spend a vision-LLM call. Cheap heuristic — no LLM.
    """
    if not message or len(message.strip()) < 3:
        return False
    # Drop obvious non-screen action keywords first to avoid false positives
    msg = message.lower()
    # If the message is clearly an action with explicit target, skip screen.
    # e.g. "ahmed ko whatsapp kar" — has "ko" but isn't screen-referential.
    action_with_target = re.search(
        r"\b\w{2,}\s+ko\s+(whatsapp|teams|email|message|mail|slack|telegram)\b",
        msg,
    )
    if action_with_target:
        return False
    return bool(_SCREEN_REGEX.search(msg))


def _looks_like_dashboard(title_low: str) -> bool:
    """True if the title appears to be the JARVIS dashboard itself.

    When the user is typing in the dashboard chat, that window is foreground
    but is NOT what they want JARVIS to "look at" — they're asking about
    a different open app (the PDF/Excel/code they were viewing before).
    """
    return any(m in title_low for m in _DASHBOARD_TITLE_MARKERS)


def get_active_window_info() -> dict:
    """Return the most-relevant window's title + a guessed app type. Fail-soft.

    Prefers a non-dashboard, non-Chrome-empty window when:
      - the current foreground is the JARVIS dashboard itself, OR
      - the current foreground is a generic browser/empty title
    This is the "I'm typing in dashboard but asking about my PDF" case — the
    PDF is in another window and is what the user really means by "yeh".
    """
    try:
        import pygetwindow as gw
        win = gw.getActiveWindow()
        title = (win.title or "").strip() if win else ""
        title_low = title.lower()

        # If foreground is JARVIS dashboard itself, look past it to the real
        # window the user was working in.
        if not title or _looks_like_dashboard(title_low):
            try:
                for other in gw.getAllWindows():
                    other_title = (other.title or "").strip()
                    if not other_title:
                        continue
                    other_low = other_title.lower()
                    if _looks_like_dashboard(other_low):
                        continue
                    # Skip JARVIS Chrome (the headless one running CDP)
                    if "jarvis" in other_low and "chrome" in other_low:
                        continue
                    # Skip minimized windows — user can't be referring to those
                    try:
                        if other.isMinimized:
                            continue
                    except Exception:
                        pass
                    # Take the first reasonable candidate
                    title = other_title
                    title_low = other_low
                    break
            except Exception as e:
                log.debug("alt_window_scan_failed", error=str(e))

        if not title:
            return {"title": "", "app": "unknown", "blocked": False}

        blocked = any(p in title_low for p in _BLOCKED_APP_PATTERNS)

        # Guess app type from title — cheap heuristic, helps the LLM understand context.
        app = "unknown"
        if ".pdf" in title_low or "adobe" in title_low or "acrobat" in title_low or "foxit" in title_low or "sumatra" in title_low:
            app = "pdf"
        elif "excel" in title_low or ".xlsx" in title_low:
            app = "excel"
        elif "word" in title_low or ".docx" in title_low:
            app = "word"
        elif "powerpoint" in title_low or ".pptx" in title_low:
            app = "powerpoint"
        elif "visual studio code" in title_low or "vscode" in title_low:
            app = "vscode"
        elif "chrome" in title_low or "edge" in title_low or "firefox" in title_low:
            app = "browser"
        elif "teams" in title_low:
            app = "teams"
        elif "whatsapp" in title_low:
            app = "whatsapp"
        elif "outlook" in title_low:
            app = "outlook"
        elif "notepad" in title_low:
            app = "notepad"
        elif "explorer" in title_low or "file explorer" in title_low:
            app = "explorer"

        return {"title": title, "app": app, "blocked": blocked}
    except Exception as e:
        log.debug("active_window_failed", error=str(e))
        return {"title": "", "app": "unknown", "blocked": False}


def _hint_keywords_from_message(message: str) -> list[str]:
    """Pull document/app hints out of the user's message.

    Used to pick the right window when several are open. E.g. user says
    "yeh PDF mein kya hai" → we should prefer a window with ".pdf" in its
    title over a random other window.
    """
    msg = message.lower()
    hints = []
    if "pdf" in msg:
        hints.extend([".pdf", "adobe", "acrobat", "foxit", "sumatra", "edge"])
    if "excel" in msg or "sheet" in msg:
        hints.extend([".xlsx", ".xls", "excel"])
    if "word" in msg or "document" in msg:
        hints.extend([".docx", ".doc", "word -"])
    if "ppt" in msg or "presentation" in msg or "slide" in msg:
        hints.extend([".pptx", ".ppt", "powerpoint"])
    if "code" in msg or "vscode" in msg or "error" in msg:
        hints.extend(["visual studio code", ".py", ".js", ".ts"])
    if "browser" in msg or "page" in msg or "article" in msg or "website" in msg:
        hints.extend(["chrome", "edge", "firefox"])
    return hints


def _find_target_window(hints: list[str]):
    """Find the window the user most likely means.

    Strategy:
      1. If any hint matches a window's title → pick that (strongest hint first)
      2. Else fall back to the most-recent non-dashboard, non-minimized window
    Returns the pygetwindow Window object or None.

    The hints are passed in PRIORITY order — most specific first (e.g. ".pdf"
    before generic "edge"). We walk hints in order and return the FIRST hint
    that produces a match. This avoids picking a random Edge window when the
    user clearly means the Edge window that has ".pdf" in its title.
    """
    try:
        import pygetwindow as gw
        all_wins = gw.getAllWindows()
    except Exception:
        return None

    candidates = []
    for w in all_wins:
        title = (w.title or "").strip()
        if not title:
            continue
        title_low = title.lower()
        if _looks_like_dashboard(title_low):
            continue
        if "jarvis" in title_low and "chrome" in title_low:
            continue
        try:
            if w.isMinimized:
                continue
        except Exception:
            pass
        candidates.append((w, title_low))

    if not candidates:
        log.info("screen_aware_no_candidates")
        return None

    log.info(
        "screen_aware_candidates",
        count=len(candidates),
        titles=[t[:60] for _, t in candidates[:8]],
    )

    # Try to match by hint first — hints are in priority order
    if hints:
        for hint in hints:
            for w, title_low in candidates:
                if hint in title_low:
                    log.info("screen_aware_picked", hint=hint, title=w.title)
                    return w
        log.info("screen_aware_no_hint_match", hints=hints)

    # Fallback: first non-dashboard candidate
    log.info("screen_aware_fallback_first", title=candidates[0][0].title)
    return candidates[0][0]


class ScreenAwareService:
    """Service for on-demand screen context enrichment in chat."""

    def __init__(self, config: ServerConfig):
        self._config = config
        self._vision = VisionDriver(config)

    def _capture_targeted(self, message: str) -> tuple[dict, bytes]:
        """Pick the target window from the message + capture it.

        Returns (window_info, image_bytes). Image may be empty if capture fails.
        For occluded windows (e.g. PDF behind dashboard) we briefly activate
        the target window, capture, then restore the previous focus. The
        flicker is ~300ms and only happens for explicit screen-referential
        queries — not on every chat message.

        If the user references a specific document type (PDF/Excel/Word) but
        NO matching window exists at the OS level (e.g. they opened it as a
        Chrome tab inside the dashboard's own Chrome window — tabs aren't
        visible to pygetwindow), return a clear "not found" signal instead
        of silently falling back to the dashboard. Capturing the dashboard
        and pretending it's the PDF was the bug that made JARVIS say
        "PDF nahi dikh rahi" while looking at the dashboard.
        """
        import time as _t
        hints = _hint_keywords_from_message(message)
        target = _find_target_window(hints)

        if not target:
            # If the user explicitly referenced a specific document type and we
            # couldn't find a matching window, fail loud — don't capture random
            # other windows pretending to be that document.
            if hints:
                hint_label = (
                    "PDF" if any("pdf" in h or "adobe" in h or "acrobat" in h for h in hints)
                    else "Excel" if any("xls" in h or "excel" in h for h in hints)
                    else "Word" if any("doc" in h or "word" in h for h in hints)
                    else "document"
                )
                return (
                    {
                        "title": "[not-found]",
                        "app": "missing",
                        "blocked": False,
                        "hint": hint_label,
                    },
                    b"",
                )
            # No specific hint, just generic "yeh / screen" — capture whatever's foreground
            return get_active_window_info(), self._vision.capture_screen()

        title = (target.title or "").strip()
        title_low = title.lower()
        if any(p in title_low for p in _BLOCKED_APP_PATTERNS):
            return (
                {"title": "[private]", "app": "blocked", "blocked": True},
                b"",
            )

        # Remember the current foreground so we can restore it after capture.
        prev_active = None
        try:
            import pygetwindow as gw
            prev_active = gw.getActiveWindow()
        except Exception:
            pass

        try:
            if not target.isActive:
                try:
                    target.activate()
                except Exception:
                    # Windows can refuse foreground activation when no recent
                    # user input — try restore() which is more permissive.
                    try:
                        target.restore()
                        target.activate()
                    except Exception:
                        pass
                _t.sleep(0.35)
        except Exception:
            pass

        img = self._vision.capture_screen()

        # Restore previous focus (the dashboard) so the user isn't yanked away
        try:
            if prev_active and prev_active.title and prev_active.title != target.title:
                prev_active.activate()
        except Exception:
            pass

        # Build the window-info dict using the targeted window, not whatever
        # ended up active after restore.
        win_info = {
            "title": title,
            "app": self._classify_app(title_low),
            "blocked": False,
        }
        return win_info, img

    @staticmethod
    def _classify_app(title_low: str) -> str:
        if ".pdf" in title_low or "adobe" in title_low or "acrobat" in title_low or "foxit" in title_low or "sumatra" in title_low:
            return "pdf"
        if "excel" in title_low or ".xlsx" in title_low:
            return "excel"
        if "word" in title_low or ".docx" in title_low:
            return "word"
        if "powerpoint" in title_low or ".pptx" in title_low:
            return "powerpoint"
        if "visual studio code" in title_low or "vscode" in title_low:
            return "vscode"
        if "teams" in title_low:
            return "teams"
        if "whatsapp" in title_low:
            return "whatsapp"
        if "outlook" in title_low:
            return "outlook"
        if "chrome" in title_low or "edge" in title_low or "firefox" in title_low:
            return "browser"
        if "notepad" in title_low:
            return "notepad"
        return "unknown"

    def enrich_with_screen_context(self, message: str) -> dict | None:
        """If the message references the screen, capture + describe. Else return None.

        Uses _capture_targeted() to find the window the user most likely means
        (PDF, Excel, etc.) and bring it forward briefly before capturing — so
        we see THAT window's content, not whatever happens to be on top.
        """
        if not is_screen_referential(message):
            return None

        win, img_bytes = self._capture_targeted(message)

        if win.get("blocked"):
            return {
                "window_title": "[private]",
                "app": "blocked",
                "screen_description": (
                    f"User ne screen reference kiya par target app "
                    f"'{win.get('title', '')}' privacy blocklist mein hai — "
                    "screenshot skip kiya. User ko boldo agar yahi app analyze "
                    "karna hai to JARVIS settings mein blocklist se nikalo."
                ),
                "trigger": "blocked",
            }

        if not img_bytes:
            # Special handling for "user said PDF/Excel but no such window exists"
            if win.get("app") == "missing":
                hint = win.get("hint", "document")
                return {
                    "window_title": "[not-found]",
                    "app": "missing",
                    "screen_description": (
                        f"User ne {hint} ka reference kiya par koi {hint} window "
                        f"OS pe khuli nahi hai. Possible reasons:\n"
                        f"  1. {hint} Chrome tab mein khuli hai DASHBOARD ke saath SAME WINDOW mein — "
                        f"tabs OS-level window nahi hote, pygetwindow nahi dekh sakti.\n"
                        f"  2. {hint} minimize hai — usko restore karo.\n"
                        f"  3. {hint} actually khuli nahi hai.\n"
                        f"User ko boldo: '{hint} ko separate window mein kholo "
                        f"(Adobe Reader / Edge alag se / Chrome mein right-click tab → "
                        f"Move tab to new window). Phir retry kar.'"
                    ),
                    "trigger": "not_found",
                }
            return {
                "window_title": win.get("title", ""),
                "app": win.get("app", "unknown"),
                "screen_description": "(screenshot fail — capture nahi hua)",
                "trigger": "fail",
            }

        # Send the targeted screenshot through Gemini Vision with the user's query.
        # Switched from Groq Llama-4 vision to Gemini 2.0 Flash to free up Groq's
        # text quota (which is shared with chat). Gemini's vision tier is more
        # generous (1500/day) and independent.
        try:
            import io
            from PIL import Image
            model = self._vision._get_gemini()
            target_app = win.get("app", "unknown")
            target_title = win.get("title", "(unknown)")
            extraction_prompt = (
                "Tu ek screen content extractor hai. Yeh ek laptop screenshot hai. "
                f"Target window: '{target_title}' (type: {target_app}).\n\n"
                "TERA KAAM:\n"
                "1. Image mein jo bhi readable text hai usko **bilkul actually padh**. "
                "Guess mat kar — sirf jo screen pe likha dikh raha woh extract kar.\n"
                "2. Roman Urdu + English mix mein bata.\n"
                "3. Agar image ek PDF/document hai — uska content summarize kar.\n"
                "4. Agar image ek code editor hai — error/code padh ke explain kar.\n"
                "5. Agar image ek Excel hai — data summarize kar.\n\n"
                "USER NE PUCHA: " + message + "\n\n"
                "IMPORTANT RULES:\n"
                "- KABHI mat bol 'main read nahi kar sakta' — tum vision-capable ho, tum padh sakte ho\n"
                "- Agar image blank/empty hai to honestly bol 'screen empty hai ya target window nahi mili'\n"
                "- Agar PDF ka content padh sakte ho to actual text extract karke summarize karo\n"
                "- Short answer do (3-6 lines), key points pe focus"
            )
            img = Image.open(io.BytesIO(img_bytes))
            response = model.generate_content(
                [extraction_prompt, img],
                generation_config={"temperature": 0.2, "max_output_tokens": 800},
            )
            description = (response.text or "").strip()
        except Exception as e:
            log.info("screen_aware_vision_failed", reason=str(e))
            return {
                "window_title": win.get("title", ""),
                "app": win.get("app", "unknown"),
                "screen_description": f"(vision LLM call fail — {e})",
                "trigger": "fail",
            }

        return {
            "window_title": win.get("title", ""),
            "app": win.get("app", "unknown"),
            "screen_description": description,
            "trigger": "auto",
        }

    @staticmethod
    def format_for_llm(ctx: dict) -> str:
        """Format the enrichment dict as a system-style context block for the LLM."""
        if not ctx:
            return ""
        return (
            "[SCREEN CONTEXT — JARVIS dekh raha hai user ki screen]\n"
            f"Active window: {ctx.get('window_title', '(unknown)')}\n"
            f"App type: {ctx.get('app', 'unknown')}\n"
            f"Screen description:\n{ctx.get('screen_description', '')}\n"
            "[END SCREEN CONTEXT]"
        )
