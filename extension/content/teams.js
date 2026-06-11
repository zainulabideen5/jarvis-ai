// JARVIS Bridge — Microsoft Teams content script.
// Runs on teams.microsoft.com + teams.live.com.

(function () {
  if (window.__jarvisTeamsInstalled) return;
  window.__jarvisTeamsInstalled = true;

  function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }
  function $(s, r) { return (r || document).querySelector(s); }

  async function waitFor(sel, timeout = 15000) {
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {
      const el = $(sel);
      if (el) return el;
      await sleep(250);
    }
    return null;
  }

  function isLoggedOut() {
    return /login\.(microsoftonline|live)\.com/.test(location.href);
  }

  async function findComposeBox(timeout = 12000) {
    const selectors = [
      'div[role="textbox"][contenteditable="true"][data-tid="ckeditor"]',
      'div[contenteditable="true"][role="textbox"]',
      'div.ck-editor__editable[contenteditable="true"]',
      'div[contenteditable="true"][aria-label*="message" i]',
    ];
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {
      for (const s of selectors) {
        const el = $(s);
        if (el) return el;
      }
      await sleep(300);
    }
    return null;
  }

  async function openNewChatWith(recipient) {
    // "New chat" button — top of left rail
    const sels = [
      'button[data-tid="chat-create-new-chat-button"]',
      'button[aria-label*="New chat" i]',
      'button[title*="New chat" i]',
    ];
    let btn = null;
    for (const s of sels) { btn = $(s); if (btn) break; }
    if (btn) btn.click();
    await sleep(800);

    // To: picker
    const pickerSelectors = [
      'input[aria-label*="To" i]',
      'div[contenteditable="true"][role="combobox"]',
      'input[placeholder*="Type a name" i]',
    ];
    let picker = null;
    const deadline = Date.now() + 5000;
    while (Date.now() < deadline) {
      for (const s of pickerSelectors) { picker = $(s); if (picker) break; }
      if (picker) break;
      await sleep(200);
    }
    if (!picker) throw new Error("Teams 'To' picker nahi mila.");

    // Focus-free text injection — mirrors WhatsApp's React-aware setter.
    // Avoids picker.focus() which triggers OS-level window foreground shift.
    setReactInputValue(picker, recipient);
    await sleep(1500);

    // First search result
    const resultDeadline = Date.now() + 5000;
    while (Date.now() < resultDeadline) {
      const result = $('li[role="option"]') || $('div[role="option"]');
      if (result) { result.click(); await sleep(600); return true; }
      await sleep(250);
    }
    // Fallback: press Enter
    picker.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    await sleep(800);
    return true;
  }

  // Focus-free React-aware text setter for contenteditable divs.
  // Avoids el.focus() + execCommand which trigger OS window foreground shift.
  // Mirrors WhatsApp content script's setContentEditableText approach.
  function setText(el, text) {
    while (el.firstChild) el.removeChild(el.firstChild);
    if (text) el.appendChild(document.createTextNode(text));
    const inputEvent = new InputEvent("input", {
      bubbles: true,
      cancelable: false,
      inputType: "insertText",
      data: text || "",
    });
    el.dispatchEvent(inputEvent);
    try { el.dispatchEvent(new Event("textInput", { bubbles: true })); } catch (_) {}
  }

  // For Teams "To" picker which is a real <input> element (not contenteditable),
  // use the native React-aware value setter via Object.getOwnPropertyDescriptor.
  function setReactInputValue(el, value) {
    if (el.tagName === "INPUT" || el.tagName === "TEXTAREA") {
      const proto = el.tagName === "INPUT" ? HTMLInputElement.prototype : HTMLTextAreaElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
      setter.call(el, value);
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
    } else {
      setText(el, value);
    }
  }

  async function sendMessage(message) {
    const compose = await findComposeBox(15000);
    if (!compose) throw new Error("Teams compose box nahi mila.");
    setText(compose, message);
    await sleep(400);
    // Dispatch Enter without focus — Teams's React keyboard handler picks it up
    compose.dispatchEvent(new KeyboardEvent("keydown", {
      key: "Enter",
      code: "Enter",
      keyCode: 13,
      which: 13,
      bubbles: true,
      cancelable: true,
    }));
    await sleep(1500);
    return true;
  }

  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    (async () => {
      try {
        if (isLoggedOut()) {
          sendResponse({ ok: false, error: "Teams logged out — login karo pehle." });
          return;
        }
        if (msg.action === "teams_send") {
          const { recipient, message } = msg.params || {};
          if (!recipient || !message) throw new Error("Recipient + message dena hoga.");

          // If already on the chat with this recipient, just type+send
          const title = (document.title || "").toLowerCase();
          if (!title.includes(recipient.toLowerCase())) {
            await openNewChatWith(recipient);
          }
          await sendMessage(message);
          sendResponse({ ok: true });
          return;
        }
        if (msg.action === "teams_ping") {
          sendResponse({ ok: true, url: location.href });
          return;
        }
        sendResponse({ ok: false, error: `unknown teams action: ${msg.action}` });
      } catch (e) {
        sendResponse({ ok: false, error: String(e?.message || e) });
      }
    })();
    return true;
  });

  console.info("[JARVIS] Teams content script loaded");
})();
