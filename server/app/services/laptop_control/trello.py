"""Trello driver — manages cards via Chrome CDP background mode (NO API).

Per user preference: tum Trello.com JARVIS Chrome mein khol ke login karo, JARVIS
us tab ko Playwright se control karega — same pattern as Teams/WhatsApp. No API
keys needed.

Setup:
    1. JARVIS Chrome mein https://trello.com/b/<your-board>/ kholo
    2. Login (one-time)
    3. Tab khuli rakho

Operations:
    - list_cards: read current visible cards
    - create_card: click "+ Add a card" on a target list, type title, Enter
    - move_card: open card → Move button → pick target list
    - add_comment: open card → comment box → type → submit
"""

from __future__ import annotations

import time as _t

from app.core.logging import get_logger

log = get_logger(__name__)


def _find_trello_page(browser) -> object | None:
    """Find an open Trello tab across all contexts."""
    for ctx in browser.contexts:
        for page in ctx.pages:
            url = (page.url or "").lower()
            if "trello.com" in url and "login" not in url:
                return page
    return None


def _connect_trello() -> tuple[object | None, object | None, object | None, str]:
    """Connect via CDP and return (playwright, browser, page, error). Page may be None if not found."""
    from app.services.laptop_control import chrome_cdp
    if not chrome_cdp.is_debug_running():
        return None, None, None, (
            "JARVIS Chrome (background mode) chal nahi raha. Pehle 'Google Chrome (Background)' "
            "icon se Chrome kholo aur trello.com mein login karo."
        )
    try:
        p, browser, _ = chrome_cdp.connect_playwright_cdp()
    except Exception as e:
        return None, None, None, f"CDP connect fail: {e}"

    page = _find_trello_page(browser)
    if not page:
        try:
            p.stop()
        except Exception:
            pass
        return None, None, None, (
            "JARVIS Chrome mein Trello tab khuli nahi. trello.com khol ke login kar pehle, phir retry."
        )
    return p, browser, page, ""


class TrelloDriver:
    """High-level Trello DOM operations via CDP."""

    @staticmethod
    def list_cards(list_filter: str | None = None) -> tuple[bool, str]:
        """Scrape visible cards from the open Trello board. Optional list filter."""
        p, browser, page, err = _connect_trello()
        if err:
            return False, err
        try:
            page.wait_for_load_state("domcontentloaded", timeout=10000)
            _t.sleep(0.6)

            # Trello board lists each have a list-name + card list. Selectors:
            #   list wrapper: [data-testid="list"] or div.list-wrapper
            #   list name:    [data-testid="list-name"] or h2
            #   cards in it:  [data-testid="card-name"] or a.list-card
            try:
                lists_data = page.evaluate("""
                    () => {
                      const out = [];
                      // Modern Trello uses [data-testid="list"]
                      const listEls = document.querySelectorAll('[data-testid="list"], div.list-wrapper, .js-list');
                      listEls.forEach(le => {
                        const nameEl = le.querySelector('[data-testid="list-name"], h2, .list-header-name');
                        const name = nameEl ? nameEl.innerText.trim() : '(unnamed)';
                        const cards = [];
                        const cardEls = le.querySelectorAll('[data-testid="card-name"], a.list-card-title, [data-testid="trello-card"]');
                        cardEls.forEach(c => {
                          const t = c.innerText.trim();
                          if (t) cards.push(t.split('\\n')[0]);
                        });
                        out.push({name, cards});
                      });
                      return out;
                    }
                """)
            except Exception as e:
                return False, f"Trello DOM scrape fail: {e}"

            if not lists_data:
                return False, "Iss page pe koi Trello list detect nahi hui — board page khuli hai?"

            if list_filter:
                fl = list_filter.lower()
                lists_data = [l for l in lists_data if fl in l.get("name", "").lower()]
                if not lists_data:
                    return False, f"'{list_filter}' naam ki list nahi mili"

            lines = [f"📋 **Trello board** — {len(lists_data)} lists:"]
            for l in lists_data:
                cards = l.get("cards", [])
                lines.append(f"\n**{l.get('name','?')}** ({len(cards)} cards)")
                for c in cards[:15]:
                    lines.append(f"  • {c}")
                if len(cards) > 15:
                    lines.append(f"  …and {len(cards)-15} more")
            return True, "\n".join(lines)
        finally:
            try:
                p.stop()
            except Exception:
                pass

    @staticmethod
    def create_card(title: str, list_name: str | None = None, board_name: str | None = None) -> tuple[bool, str]:
        """Add a card to a list on a Trello board.

        Tries paths in priority order:
          1. Chrome UIA — brief flash via 'n' shortcut, no Playwright/CDP/API
          2. Playwright shared browser (workspace) — silent background
          3. CDP fallback — requires Chrome debug mode + open board tab
        """
        if not title or not title.strip():
            return False, "Card title dena hoga"
        title = title.strip()
        list_name = (list_name or "").strip() or None
        board_name = (board_name or "").strip() or None

        # ===== Chrome UIA path (PRIMARY — NO API, NO Playwright, NO CDP) =====
        # User's regular Chrome with logged-in trello.com tab.
        # 'n' keystroke opens "Add a card" overlay on the currently-active list.
        try:
            from app.services.laptop_control.trello_chrome_uia import TrelloChromeUIA
            ok_u, msg_u = TrelloChromeUIA.get().add_card_sync(
                card_name=title,
                board_hint=board_name or "",
                timeout_sec=30.0,
            )
            if ok_u:
                if list_name:
                    return True, f"{msg_u}. Note: list_name='{list_name}' is currently best-effort — JARVIS adds to the list with last focus."
                return True, msg_u
            # Chrome-UIA module returned an error. If "no Trello tab" → fall through.
            # For other errors, surface them directly (no Playwright fallback).
            if "Chrome mein koi Trello tab" in msg_u:
                log.info("trello_chrome_uia_no_tab_falling_through")
            else:
                return False, msg_u
        except Exception as e:
            log.info("trello_chrome_uia_exception_falling_through", error=str(e)[:200])

        # ===== Playwright Trello (legacy fallback) =====
        try:
            from app.services.laptop_control.wa_playwright import WhatsAppPlaywright
            inst = WhatsAppPlaywright.get()
            # Both signals required: browser alive AND Trello actually logged in
            if inst.is_background_alive() and WhatsAppPlaywright.trello_session_exists():
                if board_name and list_name:
                    ok_pw, msg_pw = inst.trello_add_card_sync(
                        board_name=board_name,
                        list_name=list_name,
                        card_title=title,
                        timeout_sec=90,
                    )
                    if ok_pw:
                        return True, f"Trello card '{title}' add kar diya '{board_name}' → '{list_name}' mein."
                    log.info("trello_playwright_failed_fallback_cdp", error=msg_pw)
        except Exception as e:
            log.info("trello_playwright_init_fallback", error=str(e)[:200])

        # ===== CDP fallback (legacy — requires open board tab) =====
        p, browser, page, err = _connect_trello()
        if err:
            return False, err
        try:
            page.wait_for_load_state("domcontentloaded", timeout=10000)
            _t.sleep(0.6)

            # Find the target list element. If list_name given, match by name; else use first list.
            list_handle = None
            try:
                lists = page.query_selector_all('[data-testid="list"], div.list-wrapper, .js-list')
            except Exception:
                lists = []
            if not lists:
                return False, "Iss tab pe Trello board nahi dikha — board page kholo pehle"

            if list_name:
                ln = list_name.lower().strip()
                for li in lists:
                    name_el = li.query_selector('[data-testid="list-name"], h2, .list-header-name')
                    name_text = (name_el.inner_text() if name_el else "").strip().lower()
                    if name_text == ln or ln in name_text:
                        list_handle = li
                        break
                if not list_handle:
                    available = []
                    for li in lists[:8]:
                        name_el = li.query_selector('[data-testid="list-name"], h2, .list-header-name')
                        if name_el:
                            available.append(name_el.inner_text().strip())
                    return False, f"'{list_name}' list nahi mili. Available: {', '.join(available)}"
            else:
                list_handle = lists[0]

            # Click "Add a card" footer button inside this list
            add_btn = (
                list_handle.query_selector('[data-testid="list-add-card-button"]')
                or list_handle.query_selector('button:has-text("Add a card")')
                or list_handle.query_selector('a.open-card-composer')
                or list_handle.query_selector('button:has-text("Add another card")')
            )
            if not add_btn:
                return False, "List mein 'Add a card' button nahi mila"
            try:
                add_btn.click()
            except Exception:
                pass
            _t.sleep(0.6)

            # The composer text input appears
            composer = (
                list_handle.query_selector('textarea[data-testid="list-card-composer-textarea"]')
                or list_handle.query_selector('textarea[placeholder*="title" i]')
                or list_handle.query_selector('textarea')
            )
            if not composer:
                return False, "Card composer textarea nahi mila"
            try:
                composer.fill(title)
                _t.sleep(0.3)
            except Exception as e:
                return False, f"Title type fail: {e}"

            # Submit (Enter or click "Add card" button)
            submit = (
                list_handle.query_selector('[data-testid="list-card-composer-add-card-button"]')
                or list_handle.query_selector('button:has-text("Add card")')
            )
            if submit:
                try:
                    submit.click()
                except Exception:
                    page.keyboard.press("Enter")
            else:
                page.keyboard.press("Enter")
            _t.sleep(1.2)

            # Verify — find the new card text in the list
            try:
                body = page.evaluate("document.body.innerText") or ""
            except Exception:
                body = ""
            if title in body:
                return True, f"✅ Card bana di: '{title}' (list: {list_name or 'first list'}, background)"
            return False, f"Card add ki par chosen list mein verify nahi hua. Trello tab khol ke check karo."
        finally:
            try:
                p.stop()
            except Exception:
                pass

    @staticmethod
    def add_comment(card_query: str, comment: str) -> tuple[bool, str]:
        """Open a card by name match and add a comment to it."""
        if not card_query or not comment:
            return False, "Card aur comment dono dene honge"

        p, browser, page, err = _connect_trello()
        if err:
            return False, err
        try:
            page.wait_for_load_state("domcontentloaded", timeout=10000)
            _t.sleep(0.5)

            # Find a card link by name (substring match)
            ql = card_query.lower().strip()
            try:
                card_link = page.evaluate_handle(f"""
                    (q) => {{
                      const candidates = document.querySelectorAll('a[data-testid="card-name"], a.list-card');
                      for (const c of candidates) {{
                        if ((c.innerText || '').toLowerCase().includes(q)) return c;
                      }}
                      return null;
                    }}
                """, ql)
                el = card_link.as_element() if card_link else None
            except Exception:
                el = None

            if not el:
                return False, f"'{card_query}' naam ka card visible nahi"

            try:
                el.click()
                _t.sleep(1.5)
            except Exception as e:
                return False, f"Card open fail: {e}"

            # Find comment textarea — Trello card detail has a comment composer
            comment_box = (
                page.query_selector('textarea[data-testid="card-back-comment-input"]')
                or page.query_selector('textarea[placeholder*="comment" i]')
                or page.query_selector('div[data-testid="comment-box"] textarea')
            )
            if not comment_box:
                return False, "Comment textarea nahi mila card detail mein"

            try:
                comment_box.click()
                _t.sleep(0.2)
                comment_box.fill(comment)
                _t.sleep(0.3)
            except Exception as e:
                return False, f"Comment type fail: {e}"

            # Save (Save button or Ctrl+Enter)
            save = (
                page.query_selector('[data-testid="card-back-comment-save-button"]')
                or page.query_selector('button:has-text("Save")')
            )
            if save:
                try:
                    save.click()
                except Exception:
                    page.keyboard.press("Control+Enter")
            else:
                page.keyboard.press("Control+Enter")
            _t.sleep(1.0)

            return True, f"✅ Comment add hua: \"{comment[:80]}\" (card: {card_query})"
        finally:
            try:
                p.stop()
            except Exception:
                pass

    @staticmethod
    def move_card(card_query: str, to_list: str) -> tuple[bool, str]:
        """Open a card and move it to a target list via the card's Move button."""
        if not card_query or not to_list:
            return False, "Card aur target list dono dene honge"

        p, browser, page, err = _connect_trello()
        if err:
            return False, err
        try:
            page.wait_for_load_state("domcontentloaded", timeout=10000)
            _t.sleep(0.5)

            # Find card by partial title match
            ql = card_query.lower().strip()
            try:
                handle = page.evaluate_handle(f"""
                    (q) => {{
                      const candidates = document.querySelectorAll('a[data-testid="card-name"], a.list-card');
                      for (const c of candidates) {{
                        if ((c.innerText || '').toLowerCase().includes(q)) return c;
                      }}
                      return null;
                    }}
                """, ql)
                el = handle.as_element() if handle else None
            except Exception:
                el = None
            if not el:
                return False, f"'{card_query}' naam ka card nahi mila"

            try:
                el.click()
                _t.sleep(1.5)
            except Exception as e:
                return False, f"Card open fail: {e}"

            # Click "Move" button in the card sidebar
            move_btn = (
                page.query_selector('button:has-text("Move")')
                or page.query_selector('[data-testid="card-back-move-card-button"]')
            )
            if not move_btn:
                return False, "Move button nahi mila card sidebar mein"
            try:
                move_btn.click()
                _t.sleep(0.6)
            except Exception:
                pass

            # In the move popover, set target list (a select or input)
            list_select = (
                page.query_selector('select[name="list"]')
                or page.query_selector('[data-testid="move-card-list-select"]')
                or page.query_selector('button[aria-label*="List" i]')
            )
            if list_select:
                try:
                    if list_select.evaluate("e => e.tagName.toLowerCase()") == "select":
                        list_select.select_option(label=to_list)
                    else:
                        list_select.click()
                        _t.sleep(0.4)
                        # Find the option for to_list
                        opt = page.query_selector(f'div[role="option"]:has-text("{to_list}")')
                        if opt:
                            opt.click()
                except Exception as e:
                    return False, f"Target list select fail: {e}"

            # Confirm move
            confirm = (
                page.query_selector('button:has-text("Move"):not(:has-text("Move card"))')
                or page.query_selector('[data-testid="move-card-confirm"]')
                or page.query_selector('button[type="submit"]')
            )
            if confirm:
                try:
                    confirm.click()
                except Exception:
                    pass
            _t.sleep(1.5)

            return True, f"✅ Card '{card_query}' move ki: → {to_list} (background)"
        finally:
            try:
                p.stop()
            except Exception:
                pass
