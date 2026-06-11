// JARVIS Bridge — Gmail content script.
// Drives Gmail's web UI in the user's logged-in Chrome.
//
// We use Gmail's deep-link compose URL to pre-fill to/subject/body, then
// programmatically click Send. Multi-account: each account is /mail/u/<idx>/
// — caller can pass an index or this script defaults to the current URL's
// account.

(function () {
  if (window.__jarvisGmailInstalled) return;
  window.__jarvisGmailInstalled = true;

  function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }
  function $(s, r) { return (r || document).querySelector(s); }
  function $$(s, r) { return Array.from((r || document).querySelectorAll(s)); }

  function isLoggedOut() {
    return /accounts\.google\.com\/(signin|ServiceLogin)/.test(location.href);
  }

  function currentAccountIndex() {
    const m = location.pathname.match(/\/mail\/u\/(\d+)\//);
    return m ? parseInt(m[1], 10) : 0;
  }

  async function clickSend() {
    // Gmail's Send button has either data-tooltip or aria-label containing "Send"
    const sels = [
      'div[role="button"][data-tooltip*="Send" i]',
      'div[role="button"][aria-label*="Send" i]',
      'div[role="button"][data-tooltip*="Ctrl" i]',
    ];
    for (const s of sels) {
      const btn = $(s);
      if (btn) { btn.click(); return true; }
    }
    // Fallback: Ctrl+Enter
    const compose = $('div[role="dialog"]');
    if (compose) {
      compose.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", ctrlKey: true, bubbles: true }));
      return true;
    }
    return false;
  }

  async function waitForComposeDialogToClose(timeout = 10000) {
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {
      const dlg = $('div[role="dialog"]') || $('div.aDh');
      if (!dlg) return true;
      await sleep(400);
    }
    return false;
  }

  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    (async () => {
      try {
        if (isLoggedOut()) {
          sendResponse({ ok: false, error: "Gmail logged out — login karo pehle." });
          return;
        }
        if (msg.action === "gmail_send") {
          const { to, subject = "", body = "" } = msg.params || {};
          if (!to) throw new Error("To: email dena hoga.");

          // If we're not already on a compose URL, navigate there.
          // The page reload re-injects this content script, so we use a
          // sessionStorage flag to "continue from here" after reload.
          const idx = currentAccountIndex();
          const composeParams = new URLSearchParams({
            view: "cm", fs: "1", to: to, su: subject, body: body,
          });
          const composeUrl = `https://mail.google.com/mail/u/${idx}/?${composeParams.toString()}`;

          if (location.search.includes("view=cm")) {
            // already in compose — just wait + send
            await sleep(2000);
          } else {
            // store intent and navigate
            sessionStorage.setItem("_jarvis_gmail_pending", JSON.stringify({ to, subject, body, ts: Date.now() }));
            location.href = composeUrl;
            // After navigation, the content script re-runs and the new
            // page-load handler below sends. Reply to background that we
            // initiated.
            sendResponse({ ok: true, pending: true });
            return;
          }

          await clickSend();
          const verified = await waitForComposeDialogToClose(8000);
          sendResponse({ ok: verified, verified });
          return;
        }
        if (msg.action === "gmail_ping") {
          sendResponse({ ok: true, accountIndex: currentAccountIndex(), url: location.href });
          return;
        }
        sendResponse({ ok: false, error: `unknown gmail action: ${msg.action}` });
      } catch (e) {
        sendResponse({ ok: false, error: String(e?.message || e) });
      }
    })();
    return true;
  });

  // Auto-send if we arrived from a previous compose-pending state
  (async () => {
    try {
      const raw = sessionStorage.getItem("_jarvis_gmail_pending");
      if (!raw) return;
      const pending = JSON.parse(raw);
      // Only auto-send if the pending intent is recent (< 60s) and we're on compose
      if (Date.now() - pending.ts > 60000) {
        sessionStorage.removeItem("_jarvis_gmail_pending");
        return;
      }
      if (!location.search.includes("view=cm")) return;
      sessionStorage.removeItem("_jarvis_gmail_pending");
      await sleep(2500);
      await clickSend();
    } catch (_) {}
  })();

  console.info("[JARVIS] Gmail content script loaded");
})();
