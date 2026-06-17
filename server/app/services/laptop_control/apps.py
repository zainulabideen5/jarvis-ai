"""App launcher — open, close, focus apps."""

from __future__ import annotations

import os
import subprocess
import time

from app.core.logging import get_logger

log = get_logger(__name__)

# Common app aliases → actual launch command
APP_ALIASES = {
    "chrome": ["chrome", "Google Chrome", "chrome.exe"],
    "firefox": ["firefox", "Mozilla Firefox", "firefox.exe"],
    "edge": ["msedge", "Microsoft Edge", "msedge.exe"],
    "teams": ["ms-teams", "Microsoft Teams", "ms-teams.exe", "Teams.exe"],
    "whatsapp": ["WhatsApp", "whatsapp.exe"],
    "slack": ["slack", "Slack"],
    "zoom": ["Zoom", "Zoom.exe"],
    "excel": ["excel", "EXCEL.EXE"],
    "word": ["winword", "WINWORD.EXE"],
    "powerpoint": ["powerpnt", "POWERPNT.EXE"],
    "outlook": ["outlook", "OUTLOOK.EXE"],
    "notepad": ["notepad", "notepad.exe"],
    "calculator": ["calc", "calc.exe"],
    "explorer": ["explorer", "explorer.exe"],
    "cmd": ["cmd", "cmd.exe"],
    "powershell": ["powershell", "powershell.exe"],
    "vscode": ["code", "Code.exe"],
    "spotify": ["spotify", "Spotify.exe"],
    "gmail": "https://mail.google.com",
    "youtube": "https://youtube.com",
    "linkedin": "https://linkedin.com",
    "whatsapp web": "https://web.whatsapp.com",
}

# Modern Store/UWP apps don't launch by exe name — use their protocol URI.
# os.startfile on the protocol is the most reliable way to launch them.
APP_PROTOCOLS = {
    "teams": ["msteams:", "ms-teams:"],
    "whatsapp": ["whatsapp:"],
    "spotify": ["spotify:"],
}


class AppController:
    """Control apps on the laptop."""

    # Generic brand words that must NOT decide a match — "Microsoft" appears
    # in Teams, Edge, Word, Excel, Outlook, etc., so matching on it is wrong.
    _BRAND_STOPWORDS = {"microsoft", "google", "mozilla", "ms", "the", "app"}

    @staticmethod
    def _canonical_key(name: str) -> str:
        """Map a free-form app name to a known alias key.

        Handles the LLM passing display names like "Microsoft Teams" or
        "Google Chrome" instead of the short key. Priority:
          1. exact key match
          2. an alias KEY appears as a word in the name (strongest signal)
          3. distinctive (non-brand) display-name word overlap
        """
        key = (name or "").lower().strip()
        if key in APP_ALIASES:
            return key

        words = [w for w in key.replace(".exe", "").split() if w]
        word_set = set(words)

        # 2. alias key present as a whole word / substring — "teams" in
        # "microsoft teams" wins over any brand-word overlap.
        for alias_key in APP_ALIASES:
            if alias_key in word_set or alias_key == key:
                return alias_key
        for alias_key in APP_ALIASES:
            if alias_key in key:
                return alias_key

        # 3. distinctive display-name word overlap (brand words excluded)
        distinctive = word_set - AppController._BRAND_STOPWORDS
        for alias_key, target in APP_ALIASES.items():
            candidates = target if isinstance(target, list) else [str(target)]
            for cand in candidates:
                cand_words = set(cand.lower().replace(".exe", "").split())
                cand_words -= AppController._BRAND_STOPWORDS
                if distinctive & cand_words:
                    return alias_key
        return key

    @staticmethod
    def resolve_app(name: str) -> tuple[str, bool]:
        """Resolve app name to launch target. Returns (target, is_url)."""
        if not name:
            return "", False
        key = AppController._canonical_key(name)
        target = APP_ALIASES.get(key)
        if target:
            if isinstance(target, str) and target.startswith("http"):
                return target, True
            if isinstance(target, list):
                return target[0], False
            return str(target), False
        return name, False

    @staticmethod
    def open_app(name: str) -> tuple[bool, str]:
        """Open an app by name."""
        target, is_url = AppController.resolve_app(name)
        if not target:
            return False, "App naam sahi nahi hai"

        try:
            if is_url:
                import webbrowser
                webbrowser.open(target)
                return True, f"Browser mein {name} khol di"

            key = AppController._canonical_key(name)

            # Modern Store/UWP apps (Teams, WhatsApp, Spotify) — launch via
            # protocol URI using os.startfile (most reliable on Windows).
            if key in APP_PROTOCOLS:
                for proto in APP_PROTOCOLS[key]:
                    try:
                        os.startfile(proto)  # type: ignore[attr-defined]
                        time.sleep(1.0)
                        return True, f"{key.title()} khol di"
                    except OSError:
                        continue
                # Protocol failed — fall through to exe/start attempts below

            # Try direct exe launch via os.startfile (resolves AppPaths registry)
            try:
                os.startfile(target)  # type: ignore[attr-defined]
                time.sleep(0.5)
                return True, f"{name} khol di"
            except OSError:
                pass

            # Last resort: shell `start` (PATH / AppPaths lookup)
            subprocess.Popen(f'start "" "{target}"', shell=True)
            time.sleep(0.5)
            return True, f"{name} khol di"
        except Exception as e:
            log.warning("app_open_failed", app=name, error=str(e))
            return False, f"App open nahi hui: {e}"

    @staticmethod
    def open_url(url: str) -> tuple[bool, str]:
        """Open a URL in the user's default browser. The page becomes visible —
        this is for "go look at this" actions where the user expects to SEE the page,
        not background automation.
        """
        u = (url or "").strip()
        if not u:
            return False, "URL nahi diya"
        # Auto-add scheme if missing
        if not (u.startswith("http://") or u.startswith("https://")):
            u = "https://" + u
        try:
            import webbrowser
            webbrowser.open(u, new=2)  # new=2 → new tab
            return True, f"Browser mein khol di: {u}"
        except Exception as e:
            return False, f"URL open nahi hui: {e}"

    @staticmethod
    def web_search(query: str) -> tuple[bool, str]:
        """Perplexity-style research: search → fetch top pages → LLM synthesizes a cited answer.

        Returns a coherent natural-language answer in chat, citing [1], [2], [3]
        with source URLs at the bottom — no browser tab. Uses real fetched content,
        never fabricated data.

        Pipeline:
          1. ddgs search → top 5 results (titles, URLs, snippets)
          2. trafilatura fetches & extracts main text from top 3 URLs
          3. LLM gets the sources and writes an answer constrained to that data
          4. Falls back to a plain results list if synthesis fails
        """
        q = (query or "").strip()
        if not q:
            return False, "Search query nahi di"

        # Step 1 — search (top 3 results — fewer = faster)
        try:
            from ddgs import DDGS
            results = list(DDGS().text(q, max_results=3))
        except Exception as e:
            log.warning("web_search_ddgs_failed", query=q, error=str(e))
            return False, f"Search backend fail hua: {e}"

        if not results:
            return False, f"'{q}' ke liye koi result nahi mila"

        # Step 2 — fetch top 2 page contents in PARALLEL (was sequential, slow).
        # Snippets-only fallback if a fetch fails. 2 fetches in parallel cuts
        # the wait from ~12s to ~6s for slow sites.
        try:
            import trafilatura  # type: ignore
        except ImportError:
            trafilatura = None  # type: ignore

        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _fetch_one(idx_url):
            i, r = idx_url
            url = (r.get("href") or "").strip()
            content = ""
            if trafilatura is not None and url:
                try:
                    html = trafilatura.fetch_url(url, no_ssl=True)
                    if html:
                        content = (trafilatura.extract(html) or "")[:1500]
                except Exception:
                    pass
            return i, r, content

        sources: list[dict] = []
        top = list(enumerate(results[:2]))  # only top 2 — saves time
        fetched: dict = {}
        with ThreadPoolExecutor(max_workers=2) as pool:
            futs = [pool.submit(_fetch_one, item) for item in top]
            # Wrap the iterator in try/except — as_completed raises TimeoutError
            # OUTSIDE the inner block when the 8s hard cap is hit. We treat
            # partial fetches as success and fall back to snippets for the rest.
            try:
                for fut in as_completed(futs, timeout=8):
                    try:
                        i, r, content = fut.result()
                        fetched[i] = (r, content)
                    except Exception:
                        pass
            except Exception as e:
                log.info("web_search_fetch_partial_timeout", error=str(e)[:120])
                # Cancel pending and continue with whatever completed
                for f in futs:
                    if not f.done():
                        try:
                            f.cancel()
                        except Exception:
                            pass
        # Build sources in original order
        for i, r in top:
            r2, content = fetched.get(i, (r, ""))
            title = (r2.get("title") or "").strip()
            url = (r2.get("href") or "").strip()
            snippet = (r2.get("body") or "").strip()
            sources.append({
                "idx": i + 1,
                "title": title,
                "url": url,
                "content": content or snippet,
            })

        # Step 3 — synthesis. Claude CLI (Opus) PEHLE — real, Claude/ChatGPT-jaisi
        # quality + koi quota/limit nahi. Groq sirf fallback (jab CLI available na ho).
        sources_block = ""
        for s in sources:
            sources_block += (
                f"\n[{s['idx']}] {s['title']}\nURL: {s['url']}\n"
                f"Content: {s['content']}\n"
            )
        prompt = (
            f"User ka sawal: \"{q}\"\n\n"
            f"Web sources (real-time fetched):\n{sources_block}\n\n"
            "In sources ko padh ke user ke sawal ka SAAF, COMPLETE, REAL answer do — "
            "bilkul jaise Claude/ChatGPT deta hai. Roman Urdu + English mix.\n"
            "RULES:\n"
            "- SIRF iss data se answer banao — kuch invent/guess mat karo\n"
            "- Jitna zaroori utna detail (chhota sawal = chhota jawab; gehra = gehra)\n"
            "- Citations [1], [2], [3] jahan info us source se aayi\n"
            "- Agar exact info sources mein na mili to honestly bolo\n"
            "Answer:"
        )

        answer = ""
        # Claude CLI first
        try:
            from app.services.universal_engine.brain import ClaudeCLIBrain
            brain = ClaudeCLIBrain(model="opus")
            if brain.is_available():
                txt = brain.think(
                    "Tu ek research assistant hai jo SIRF diye gaye real-time web "
                    "sources se sahi, cited, complete jawab deta hai — kuch invent nahi karta.",
                    [{"role": "user", "content": prompt}])
                if txt and txt.strip():
                    answer = txt.strip()
        except Exception as e:
            log.info("web_search_cli_fallback", error=str(e)[:140])

        # Groq fallback (sirf agar CLI na chala)
        if not answer:
            try:
                from app.core.config import ServerConfig
                from app.core.llm import LLMClient
                config = ServerConfig()
                llm = LLMClient(config)
                response = llm.chat.completions.create(
                    model=config.groq_model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.2, max_tokens=900, timeout=30.0,
                )
                answer = (response.choices[0].message.content or "").strip()
            except Exception as e:
                log.warning("web_search_synthesis_failed", query=q, error=str(e))

        if answer:
            footer = "\n\n📚 **Sources:**"
            for s in sources:
                footer += f"\n[{s['idx']}] {s['title'][:90]}\n   🔗 {s['url']}"
            return True, answer + footer

        # Dono fail — plain link list (kuch to mile)
        lines = [f"🔍 **{q}** — sources (synthesis fail hua, raw links):\n"]
        for s in sources:
            preview = (s["content"][:200].rstrip() + "…") if len(s["content"]) > 200 else s["content"]
            lines.append(f"{s['idx']}. **{s['title']}**")
            if s["url"]:
                lines.append(f"   🔗 {s['url']}")
            if preview:
                lines.append(f"   {preview}")
            lines.append("")
        return True, "\n".join(lines)

    @staticmethod
    def focus_window(title_substring: str) -> tuple[bool, str]:
        """Bring a window to foreground by title substring."""
        try:
            import pygetwindow as gw
            windows = [w for w in gw.getAllWindows() if title_substring.lower() in (w.title or "").lower() and w.title]
            if not windows:
                return False, f"Window '{title_substring}' nahi mili"

            w = windows[0]
            try:
                if w.isMinimized:
                    w.restore()
                w.activate()
                time.sleep(0.3)
                return True, f"Focus kiya: {w.title}"
            except Exception:
                # Activation sometimes fails on Windows — still consider found
                return True, f"Window mil gaya: {w.title}"
        except Exception as e:
            return False, f"Focus failed: {e}"

    @staticmethod
    def close_app(name: str) -> tuple[bool, str]:
        """Close an app by process name."""
        target, _ = AppController.resolve_app(name)
        if not target:
            return False, "App naam sahi nahi"

        proc_name = target if target.endswith(".exe") else f"{target}.exe"
        try:
            result = subprocess.run(
                ["taskkill", "/F", "/IM", proc_name],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                return True, f"{name} band ho gayi"
            return False, f"Close nahi hui: {result.stderr or 'running nahi thi'}"
        except Exception as e:
            return False, f"Close failed: {e}"

    @staticmethod
    def list_running_windows() -> list[str]:
        """List currently open window titles."""
        try:
            import pygetwindow as gw
            return [w.title for w in gw.getAllWindows() if w.title]
        except Exception:
            return []
