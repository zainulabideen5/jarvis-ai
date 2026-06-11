// JARVIS Bridge — WhatsApp Web content script (v0.4 — focus-free).
//
// THE BUG WE'RE FIXING:
//   Earlier versions called element.focus() and document.execCommand("insertText"),
//   which forces the browser window to come to the foreground on Windows even
//   when the tab is in a minimized window. That's why "background mode" was
//   leaking into the user's view.
//
// THE FIX:
//   - No element.focus() calls anywhere
//   - No document.execCommand (it requires focus)
//   - Text input uses React's native value setter + dispatched input events,
//     which works without focus
//   - Sending uses the Send button (click) instead of Enter key (which can
//     bubble focus changes)
//   - window.focus is stubbed so WhatsApp Web can't pull focus itself

(function () {
  if (window.__jarvisWAInstalled) return;
  window.__jarvisWAInstalled = true;

  try { window.focus = function () { /* JARVIS no-op */ }; } catch (_) {}
  try { window.alert = function () { /* JARVIS no-op */ }; } catch (_) {}

  function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }
  function $(s, r) { return (r || document).querySelector(s); }
  function $$(s, r) { return Array.from((r || document).querySelectorAll(s)); }

  function isLoggedOut() {
    return !!$('canvas[aria-label*="QR" i]');
  }

  // React-aware text setting for contenteditable divs (what WhatsApp Web uses).
  // Avoids focus(), avoids execCommand. Works on minimized/background windows.
  function setContentEditableText(el, text) {
    // Clear existing content
    while (el.firstChild) el.removeChild(el.firstChild);
    // Insert new text as a single text node
    el.appendChild(document.createTextNode(text));
    // Tell React the input changed
    const inputEvent = new InputEvent("input", {
      bubbles: true,
      cancelable: false,
      inputType: "insertText",
      data: text,
    });
    el.dispatchEvent(inputEvent);
    // Some WA components also listen for textInput
    try {
      el.dispatchEvent(new Event("textInput", { bubbles: true }));
    } catch (_) {}
  }

  // Programmatic click that doesn't pass through OS-level focus paths.
  // Calling el.click() is enough — it doesn't activate the window.
  function softClick(el) {
    try { el.click(); } catch (_) {}
  }

  async function findSearchBox(timeout = 25000) {
    const selectors = [
      'div[contenteditable="true"][data-tab="3"]',
      'div[role="textbox"][contenteditable="true"][title*="Search" i]',
      'div[role="textbox"][contenteditable="true"][aria-label*="Search" i]',
      'div[aria-label="Search input textbox"]',
      'div[aria-placeholder*="Search" i]',
    ];
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {
      if (isLoggedOut()) {
        throw new Error("WhatsApp Web logged out — phone se QR scan kar.");
      }
      for (const sel of selectors) {
        const el = $(sel);
        if (el) return el;
      }
      await sleep(300);
    }
    throw new Error("WhatsApp search box nahi mila — page slow ya WA UI changed.");
  }

  async function findComposeBox(timeout = 15000) {
    const selectors = [
      'div[contenteditable="true"][data-tab="10"]',
      'div[role="textbox"][contenteditable="true"][aria-placeholder*="Type" i]',
      'div[role="textbox"][contenteditable="true"][data-lexical-editor="true"]',
    ];
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {
      for (const sel of selectors) {
        const el = $(sel);
        if (el) return el;
      }
      await sleep(300);
    }
    return null;
  }

  // Find the send button — has aria-label "Send" or data-icon attributes.
  // Clicking the button instead of pressing Enter avoids any keyboard event
  // bubbling up to Chrome's window-management layer.
  async function findSendButton(timeout = 4000) {
    const selectors = [
      'button[aria-label="Send"]',
      'button[data-tab="11"]',
      'span[data-icon="send"]',
      'span[data-testid="send"]',
      '[data-icon="send-2"]',
    ];
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {
      for (const sel of selectors) {
        const el = $(sel);
        if (el) {
          // Walk up to the actual clickable button if we matched the icon span
          let target = el;
          if (target.tagName !== "BUTTON") {
            const btn = target.closest('button, [role="button"]');
            if (btn) target = btn;
          }
          return target;
        }
      }
      await sleep(200);
    }
    return null;
  }

  async function openChatByName(name) {
    const search = await findSearchBox();
    // Click the search box to ensure WA selects it as the input target — but
    // .click() doesn't activate the window like .focus() can.
    softClick(search);
    await sleep(150);
    setContentEditableText(search, name);
    await sleep(900);

    const deadline = Date.now() + 6000;
    const nameLow = name.toLowerCase();
    while (Date.now() < deadline) {
      // WA renders results as listitems; first matching one is the chat to open
      const results = $$('div[role="listitem"]');
      for (const r of results) {
        const t = (r.innerText || "").trim().toLowerCase();
        if (t && t.includes(nameLow)) {
          softClick(r);
          await sleep(800);
          return true;
        }
      }
      await sleep(250);
    }
    throw new Error(`"${name}" nahi mila WhatsApp ki contacts mein.`);
  }

  async function openChatByPhone(phone) {
    // We avoid location.href = ... navigation (visible UI change).
    // Instead use WhatsApp's URL fragment which auto-opens the chat without
    // reloading the page in most modern WA Web builds.
    const target = `https://web.whatsapp.com/send?phone=${encodeURIComponent(phone)}`;
    if (location.href !== target) {
      // history.replaceState avoids the page-load flash
      try {
        // For phone-only sends we still need to do a real navigation because WA's
        // URL handling only kicks in on full load — but this code path is the
        // fallback for numbers not in WA's contacts; the common case is name search.
        location.href = target;
        await sleep(2500);
      } catch (_) {}
    }
    const compose = await findComposeBox(20000);
    if (!compose) throw new Error("Chat load nahi hua — phone WA pe registered nahi shayad.");
    return true;
  }

  async function sendMessage(message) {
    const compose = await findComposeBox(10000);
    if (!compose) throw new Error("Compose box nahi mila.");
    setContentEditableText(compose, message);
    await sleep(500);

    // Prefer clicking the Send button — avoids dispatching keyboard events
    // which can interact with Chrome's window management on some Windows builds.
    const btn = await findSendButton(3000);
    if (btn) {
      softClick(btn);
    } else {
      // Fallback: synthetic Enter keydown on the compose box (no focus needed)
      const evt = new KeyboardEvent("keydown", {
        key: "Enter", code: "Enter", which: 13, keyCode: 13, bubbles: true,
      });
      compose.dispatchEvent(evt);
    }
    await sleep(1200);
    return true;
  }

  async function verifyMessageInChat(text, timeout = 4000) {
    const deadline = Date.now() + timeout;
    const needle = (text || "").trim();
    if (!needle) return true;
    while (Date.now() < deadline) {
      const body = document.body.innerText || "";
      const tail = body.slice(-3000);
      if (tail.includes(needle)) return true;
      await sleep(300);
    }
    return false;
  }

  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    (async () => {
      try {
        if (msg.action === "whatsapp_send") {
          const { recipient, phone, message } = msg.params || {};
          if (!message) throw new Error("Message text dena hoga.");
          if (isLoggedOut()) {
            throw new Error("WhatsApp Web logged out — QR scan zaroori.");
          }
          let opened = false;
          if (recipient && recipient.trim()) {
            try {
              await openChatByName(recipient.trim());
              opened = true;
            } catch (e) {
              if (!phone) throw e;
            }
          }
          if (!opened) {
            if (phone) {
              await openChatByPhone(phone);
            } else {
              throw new Error("Recipient ya phone dena hoga.");
            }
          }
          await sendMessage(message);
          const verified = await verifyMessageInChat(message);
          sendResponse({ ok: true, verified });
          return;
        }
        if (msg.action === "whatsapp_ping") {
          sendResponse({ ok: true, loggedOut: isLoggedOut(), url: location.href });
          return;
        }
        sendResponse({ ok: false, error: `unknown wa action: ${msg.action}` });
      } catch (e) {
        sendResponse({ ok: false, error: String(e?.message || e) });
      }
    })();
    return true;
  });

  console.info("[JARVIS] WhatsApp content script v0.4 loaded (focus-free mode)");
})();
