// JARVIS Bridge — background service worker.
//
// v0.5: Switched WhatsApp send to use chrome.debugger (CDP Runtime.evaluate)
// instead of chrome.tabs.sendMessage. CDP commands don't trigger the OS-level
// focus-shift that was bringing Chrome to the foreground in earlier versions.

import { CDP } from "./lib/cdp.js";
//
// Connects to the locally-running JARVIS server over WebSocket and acts as
// the bridge between server-issued commands and Chrome tab automation.
//
// Why a WebSocket and not HTTP polling: commands fire from chat in real time
// (user types "ahmed ko WhatsApp", server needs the extension to act NOW).
// Polling adds latency + battery cost.
//
// Connection lifecycle:
//   - Auto-connect on startup
//   - Reconnect with exponential backoff if dropped (max 30s)
//   - Keepalive ping every 25s so MV3 service worker doesn't get killed
//     while idle (Chrome unloads idle workers after ~30s)
//
// Tabs strategy: open service tabs in BACKGROUND (active=false), do the work
// via content scripts, then close them. User's existing tabs are never
// touched — they stay in whatever window/state the user left them.

const JARVIS_WS_URL = "ws://127.0.0.1:8000/api/ext/ws";
const JARVIS_HTTP_BASE = "http://127.0.0.1:8000";
const RECONNECT_MIN_MS = 1000;
const RECONNECT_MAX_MS = 30000;
const KEEPALIVE_MS = 25000;

// Selector config polling — every 5 min, check server for newer version.
// When updated, fetch full config + push to all active service tabs.
const SELECTORS_POLL_MS = 5 * 60 * 1000;  // 5 minutes
const SELECTORS_CACHE_KEY = "jarvis_selectors_v1";
let selectorsPollTimer = null;

let ws = null;
let reconnectMs = RECONNECT_MIN_MS;
let keepaliveTimer = null;
let lastConnectedAt = 0;

function setBadge(state) {
  // green = connected, red = disconnected, yellow = connecting
  const map = {
    connected: { color: "#10b981", text: "ON" },
    disconnected: { color: "#ef4444", text: "OFF" },
    connecting: { color: "#f59e0b", text: "..." },
  };
  const c = map[state] || map.disconnected;
  try {
    chrome.action.setBadgeBackgroundColor({ color: c.color });
    chrome.action.setBadgeText({ text: c.text });
  } catch (_) {}
}

async function setStatus(status) {
  try {
    await chrome.storage.local.set({ jarvis_status: status, jarvis_status_ts: Date.now() });
  } catch (_) {}
}

function startKeepalive() {
  stopKeepalive();
  keepaliveTimer = setInterval(() => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      try { ws.send(JSON.stringify({ type: "ping", ts: Date.now() })); } catch (_) {}
    }
  }, KEEPALIVE_MS);
}

function stopKeepalive() {
  if (keepaliveTimer) {
    clearInterval(keepaliveTimer);
    keepaliveTimer = null;
  }
}

// ── Selector config sync ─────────────────────────────────────────────
// Background script owns the selector cache lifecycle:
//   1. On startup: fetch latest from server (if reachable)
//   2. Every 5 min: poll /api/ext/selectors/version, fetch full only if newer
//   3. After fetch: persist to chrome.storage.local + notify open service tabs
//
// Content scripts read from chrome.storage.local via lib/selectors.js.
// When we push an update, content scripts auto-refresh their in-memory cache
// without a page reload.

async function getCachedSelectorsVersion() {
  return new Promise((resolve) => {
    chrome.storage.local.get([SELECTORS_CACHE_KEY], (items) => {
      const meta = items && items[SELECTORS_CACHE_KEY] && items[SELECTORS_CACHE_KEY]._meta;
      resolve((meta && meta.version) || 0);
    });
  });
}

async function fetchAndApplySelectors(opts) {
  const forceFull = opts && opts.forceFull;
  try {
    // Lightweight version check first
    let serverVersion = 0;
    if (!forceFull) {
      try {
        const verResp = await fetch(`${JARVIS_HTTP_BASE}/api/ext/selectors/version`, { cache: "no-store" });
        if (verResp.ok) {
          const verData = await verResp.json();
          serverVersion = parseInt(verData.version || 0, 10);
        }
      } catch (_) {
        // version endpoint unreachable — try full fetch anyway
      }
      const cachedVersion = await getCachedSelectorsVersion();
      if (serverVersion && serverVersion <= cachedVersion) {
        // Already up to date — no fetch needed
        return { updated: false, version: cachedVersion, reason: "up-to-date" };
      }
    }
    // Fetch full config
    const resp = await fetch(`${JARVIS_HTTP_BASE}/api/ext/selectors`, { cache: "no-store" });
    if (!resp.ok) {
      return { updated: false, version: 0, reason: `server returned ${resp.status}` };
    }
    const data = await resp.json();
    if (!data || !data._meta || !Number.isInteger(data._meta.version)) {
      return { updated: false, version: 0, reason: "malformed config" };
    }
    // Persist
    await new Promise((resolve) => {
      chrome.storage.local.set({ [SELECTORS_CACHE_KEY]: data }, () => resolve());
    });
    // Notify all open service tabs so they refresh their in-memory cache
    await notifyTabsOfSelectorUpdate(data);
    return { updated: true, version: data._meta.version, reason: "fetched" };
  } catch (e) {
    return { updated: false, version: 0, reason: String(e && e.message || e) };
  }
}

async function notifyTabsOfSelectorUpdate(newConfig) {
  const serviceHosts = ["web.whatsapp.com", "mail.google.com", "teams.microsoft.com", "teams.live.com"];
  try {
    const tabs = await new Promise((resolve) => {
      chrome.tabs.query({}, (t) => resolve(t || []));
    });
    for (const tab of tabs) {
      try {
        const url = tab.url || "";
        if (!serviceHosts.some((h) => url.includes(h))) continue;
        chrome.tabs.sendMessage(tab.id, { type: "selectors_updated", config: newConfig }, () => {
          // ignore "no receiver" errors — tab may not have selectors lib loaded
          if (chrome.runtime.lastError) {
            // silent — content script not ready
          }
        });
      } catch (_) {
        // continue
      }
    }
  } catch (_) {
    // tabs query failed — non-fatal
  }
}

function startSelectorsPoller() {
  stopSelectorsPoller();
  // Fire once immediately on startup, then on interval
  fetchAndApplySelectors({}).catch(() => {});
  selectorsPollTimer = setInterval(() => {
    fetchAndApplySelectors({}).catch(() => {});
  }, SELECTORS_POLL_MS);
}

function stopSelectorsPoller() {
  if (selectorsPollTimer) {
    clearInterval(selectorsPollTimer);
    selectorsPollTimer = null;
  }
}

function connect() {
  setBadge("connecting");
  setStatus("connecting");
  try {
    ws = new WebSocket(JARVIS_WS_URL);
  } catch (e) {
    console.warn("[JARVIS] WS construct failed", e);
    scheduleReconnect();
    return;
  }

  ws.onopen = () => {
    console.info("[JARVIS] WS connected");
    reconnectMs = RECONNECT_MIN_MS;
    lastConnectedAt = Date.now();
    setBadge("connected");
    setStatus("connected");
    startKeepalive();
    startSelectorsPoller();
    try {
      ws.send(JSON.stringify({
        type: "hello",
        client: "jarvis-bridge-extension",
        version: chrome.runtime.getManifest().version,
        ts: Date.now(),
      }));
    } catch (_) {}
  };

  ws.onmessage = async (evt) => {
    let msg;
    try { msg = JSON.parse(evt.data); }
    catch (e) { console.warn("[JARVIS] bad message", evt.data); return; }
    await handleServerMessage(msg);
  };

  ws.onclose = () => {
    console.info("[JARVIS] WS closed");
    stopKeepalive();
    stopSelectorsPoller();
    setBadge("disconnected");
    setStatus("disconnected");
    scheduleReconnect();
  };

  ws.onerror = (e) => {
    // Errors usually fire alongside close — log but rely on onclose to schedule reconnect.
    console.warn("[JARVIS] WS error", e);
  };
}

function scheduleReconnect() {
  setTimeout(connect, reconnectMs);
  reconnectMs = Math.min(reconnectMs * 2, RECONNECT_MAX_MS);
}

// ===== Command handling =====

async function handleServerMessage(msg) {
  const type = msg.type || "";
  switch (type) {
    case "pong":
    case "ack":
      return;
    case "ping":
      sendToServer({ type: "pong", ts: Date.now(), req_id: msg.req_id });
      return;
    case "command":
      return handleCommand(msg);
    default:
      console.warn("[JARVIS] unknown server msg type:", type);
  }
}

function sendToServer(payload) {
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    console.warn("[JARVIS] cannot send, WS not open", payload?.type);
    return false;
  }
  try { ws.send(JSON.stringify(payload)); return true; }
  catch (e) { console.warn("[JARVIS] send failed", e); return false; }
}

// ===== Tab + content-script helpers =====

// JARVIS work happens in a dedicated, MINIMIZED Chrome window. This is what
// makes operations truly background-invisible: the user's main window keeps
// focus, the minimized window does the WhatsApp/Teams/Gmail work without
// disturbing anything. Reusing an existing service tab in the user's main
// window is bad because:
//   - the content script's input.focus() may scroll the tab into view
//   - WA navigation (wa.me deeplinks) re-loads the visible page
//   - any tab title change can flash the tab bar
// A separate minimized window avoids all of that.

const JARVIS_WINDOW_KEY = "jarvis_work_window_id";

async function getJarvisWindow() {
  try {
    const { [JARVIS_WINDOW_KEY]: stored } = await chrome.storage.local.get(JARVIS_WINDOW_KEY);
    if (stored) {
      try {
        const w = await chrome.windows.get(stored);
        if (w) return w;
      } catch (_) {
        // Window was closed — fall through to create a new one
      }
    }
  } catch (_) {}
  return null;
}

// JARVIS window approach REMOVED — was causing a separate Chrome window to
// flash open before minimizing. We now use the user's EXISTING WhatsApp/Teams/
// Gmail tabs in their normal Chrome window. chrome.debugger lets us drive
// those tabs at the CDP layer without activating the tab or pulling the
// window forward.

// Find an existing service tab anywhere in the user's Chrome.
async function findTab(urlPrefixes) {
  const tabs = await chrome.tabs.query({});
  for (const t of tabs) {
    const u = (t.url || "").toLowerCase();
    for (const p of urlPrefixes) {
      if (u.startsWith(p)) return t;
    }
  }
  return null;
}

// Ensure a service tab exists. If the user already has one open (which they
// usually do — WhatsApp Web stays open), reuse it. Otherwise create a new
// background tab in the most-recent window. Key: active:false so the new tab
// doesn't steal focus from whatever the user is doing.
async function ensureTab(urlPrefixes, fallbackUrl) {
  let tab = await findTab(urlPrefixes);
  if (!tab) {
    tab = await chrome.tabs.create({
      url: fallbackUrl,
      active: false,
    });
    await waitForTabLoaded(tab.id, 30000);
  }
  return tab;
}

function waitForTabLoaded(tabId, timeout = 20000) {
  return new Promise((resolve) => {
    const deadline = Date.now() + timeout;
    const check = async () => {
      try {
        const t = await chrome.tabs.get(tabId);
        if (t.status === "complete") return resolve(true);
      } catch (_) { return resolve(false); }
      if (Date.now() > deadline) return resolve(false);
      setTimeout(check, 400);
    };
    check();
  });
}

// Send a message to a tab's content script with timeout
function sendToTab(tabId, payload, timeout = 60000) {
  return new Promise((resolve) => {
    let done = false;
    const timer = setTimeout(() => {
      if (done) return;
      done = true;
      resolve({ ok: false, error: "content_script_timeout" });
    }, timeout);

    try {
      chrome.tabs.sendMessage(tabId, payload, (response) => {
        if (done) return;
        done = true;
        clearTimeout(timer);
        if (chrome.runtime.lastError) {
          resolve({ ok: false, error: chrome.runtime.lastError.message });
          return;
        }
        resolve(response || { ok: false, error: "empty_response" });
      });
    } catch (e) {
      if (done) return;
      done = true;
      clearTimeout(timer);
      resolve({ ok: false, error: String(e?.message || e) });
    }
  });
}

// Show a small notification to the user (top-right corner of OS)
async function notify(title, message) {
  try {
    await chrome.notifications.create({
      type: "basic",
      iconUrl: chrome.runtime.getURL("icons/icon48.png"),
      title: title || "JARVIS",
      message: String(message || ""),
      priority: 1,
    });
  } catch (_) {}
}

// ===== Service dispatchers =====

// Keyboard-driven helper: open a WhatsApp chat by typing in the search bar.
// Uses Ctrl+Alt+/ (WhatsApp's universal accessibility shortcut) to focus
// search, then types the query, then Arrow Down + Enter to open the top match.
// No DOM selector dependency for the search box — works on any WA Web version
// or language because keyboard shortcuts are part of WhatsApp's stable a11y API.
async function waOpenChatByKeyboard(tabId, searchTerm) {
  if (!searchTerm) throw new Error("Recipient ya phone dena hoga");

  // Ensure WhatsApp has page focus before sending keyboard events
  await CDP.evaluate(tabId, `(() => { try { document.body.click(); } catch (_) {} return true; })()`);
  await sleep(200);

  // Focus search via Ctrl+Alt+/
  await CDP.key(tabId, "/", { modifiers: CDP.MOD_CTRL | CDP.MOD_ALT });
  await sleep(500);

  // Clear any existing text in the search input
  await CDP.key(tabId, "a", { modifiers: CDP.MOD_CTRL });
  await sleep(120);
  await CDP.key(tabId, "Backspace");
  await sleep(120);

  // Type the search query (Input.insertText is more reliable than per-char)
  await CDP.typeText(tabId, searchTerm);
  await sleep(1800);

  // ArrowDown to highlight the first result, Enter to open it
  await CDP.key(tabId, "ArrowDown");
  await sleep(300);
  await CDP.key(tabId, "Enter");
  await sleep(1000);

  // Verify chat opened — compose box becomes available
  return await CDP.evaluate(tabId, `(() => {
    const eds = document.querySelectorAll('[contenteditable="true"]');
    for (const el of eds) {
      const ph = (el.getAttribute('aria-placeholder') || '').toLowerCase();
      const lbl = (el.getAttribute('aria-label') || '').toLowerCase();
      const tab = el.getAttribute('data-tab') || '';
      if (tab === '10' || ph.includes('message') || lbl.includes('type a message')) return true;
    }
    return false;
  })()`);
}

// WhatsApp send via CDP keyboard input — no DOM selector dependency
async function actWhatsapp(action, params) {
  const tab = await ensureTab(
    ["https://web.whatsapp.com/"],
    "https://web.whatsapp.com/",
  );
  // On COLD start, WhatsApp takes 5-20s to render its UI even after the
  // page reports "complete". Give it some breathing room; findSearch will
  // poll for readiness too.
  try {
    const t = await chrome.tabs.get(tab.id);
    if (t.status !== "complete") {
      await sleep(2500);
    } else {
      await sleep(500);
    }
  } catch (_) { await sleep(500); }

  if (action === "whatsapp_ping") {
    return await CDP.withSession(tab.id, async () => {
      return await CDP.evaluate(tab.id, `(() => ({
        loggedOut: !!document.querySelector('canvas[aria-label*="QR" i]'),
        url: location.href,
      }))()`);
    });
  }

  if (action === "whatsapp_send") {
    const { recipient, phone, message, attachment_b64, attachment_name, attachment_mime } = params || {};
    if (!message && !attachment_b64) return { ok: false, error: "Message text ya attachment dena hoga" };

    // KEYBOARD-DRIVEN PATH (preferred — portable across WA versions/laptops).
    // Uses Ctrl+Alt+/ to focus search → types → ArrowDown+Enter to open match.
    // No DOM-selector dependency for opening the chat.
    const sessionResult = await CDP.withSession(tab.id, async () => {
      try {
        // Check login state
        const state = await CDP.evaluate(tab.id, `(() => ({
          loggedOut: !!document.querySelector('canvas[aria-label*="QR" i]'),
          ready: !!(document.querySelector('#pane-side') || document.querySelector('[role="grid"]') || document.querySelector('div[id="side"]')),
        }))()`);
        if (state.loggedOut) {
          return { ok: false, error: "WhatsApp Web logged out — QR scan zaroori." };
        }
        if (!state.ready) {
          // Wait for WA to finish loading (up to 30s)
          let ready = false;
          for (let i = 0; i < 30; i++) {
            await sleep(1000);
            ready = await CDP.evaluate(tab.id, `!!(document.querySelector('#pane-side') || document.querySelector('[role="grid"]') || document.querySelector('div[id="side"]'))`);
            if (ready) break;
          }
          if (!ready) return { ok: false, error: "WhatsApp Web 30 sec mein load nahi hua. Refresh karo aur retry." };
        }

        // First attempt — full search term (name or phone)
        const primary = (recipient && recipient.trim()) || phone || "";
        let opened = await waOpenChatByKeyboard(tab.id, primary);

        // Second attempt — first word only (handles "Zaid Zenesa" → "Zaid")
        if (!opened) {
          const firstWord = primary.split(/\s+/)[0];
          if (firstWord && firstWord !== primary) {
            opened = await waOpenChatByKeyboard(tab.id, firstWord);
          }
        }

        // Third attempt — phone deeplink fallback. Detaching+reattaching
        // the debugger INSIDE withSession causes "Debugger is not attached"
        // errors because Chrome considers the navigation as terminating the
        // session. Instead, signal that deeplink is needed and handle it
        // OUTSIDE this withSession block. We do that by returning a sentinel.
        if (!opened && phone && !attachment_b64) {
          const cleanPhone = String(phone).replace(/[^\d]/g, "");
          if (cleanPhone) {
            return { ok: false, needsDeeplink: true, cleanPhone };
          }
        }

        if (!opened) {
          // Collect diagnostic — what contacts/results WA actually shows
          const seen = await CDP.evaluate(tab.id, `(() => {
            const out = [];
            const titles = document.querySelectorAll('span[title]');
            for (const t of titles) {
              const v = (t.getAttribute('title') || '').trim();
              if (v && !out.includes(v)) out.push(v);
              if (out.length >= 8) break;
            }
            return out;
          })()`);
          const list = seen && seen.length ? " | WhatsApp pe yeh dikh raha: " + seen.join(", ") : "";
          return { ok: false, error: "'" + primary + "' WA mein nahi mila." + list };
        }

        // Chat is open. Now either upload attachment or send text.
        if (attachment_b64) {
          // Attachment path — page-side script handles file picker.
          // We pass values via JSON.stringify to avoid any quoting issues.
          const uploadScript = `
(async () => {
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const $ = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));

  // Decide which menu item to target based on file type. Returns the
  // user-facing menu label WhatsApp uses (case-insensitive match).
  function targetMenuLabel(mime, name) {
    const m = (mime || "").toLowerCase();
    const ext = (name || "").toLowerCase().split('.').pop();
    if (m.startsWith('image/') || m.startsWith('video/') ||
        ['jpg','jpeg','png','gif','webp','heic','mp4','mov','3gp','webm','mkv'].includes(ext)) {
      return ['photos & videos', 'photos and videos', 'photos', 'image', 'video', 'media'];
    }
    if (m.startsWith('audio/') || ['mp3','wav','ogg','m4a','aac','flac'].includes(ext)) {
      return ['audio'];
    }
    return ['document'];
  }

  // STICKER label — explicit blacklist. We never want this even by accident.
  const STICKER_LABELS = ['sticker', 'stickers'];

  function elText(el) {
    return ((el.innerText || el.textContent || '') + ' ' +
      (el.getAttribute('aria-label') || '') + ' ' +
      (el.getAttribute('title') || '')).toLowerCase();
  }

  // Find a menu item whose visible text/label matches one of the targets.
  // The item must NOT match any sticker label (defence-in-depth).
  function findMenuItem(targets) {
    const candidates = $$('li[role="button"], div[role="button"], button, [role="menuitem"]');
    for (const el of candidates) {
      const t = elText(el);
      if (!t) continue;
      if (STICKER_LABELS.some(s => t.includes(s))) continue;
      for (const target of targets) {
        if (t.includes(target)) return el;
      }
    }
    return null;
  }

  // After we click a specific menu item, that item's hidden <input type="file">
  // is the one we want to use. Search for it inside (or near) the menu item.
  function inputForMenuItem(item) {
    if (!item) return null;
    // Common case — input is descendant of the menu item
    const inside = item.querySelector('input[type="file"]');
    if (inside) return inside;
    // Some WA versions put inputs as siblings in the same menu container
    const container = item.closest('ul, [role="menu"], div[role="menu"]') || item.parentElement;
    if (container) {
      const ins = container.querySelectorAll('input[type="file"]');
      // Pick the input nearest to the clicked item in DOM order
      for (const inp of ins) {
        // If the item or its descendants contain this input, it's the match
        if (item.contains(inp)) return inp;
      }
      // Otherwise pick the FIRST non-sticker input — never the sticker one
      for (const inp of ins) {
        const acc = (inp.getAttribute('accept') || '').toLowerCase();
        // Sticker inputs: only specific image types (webp,png,gif) and NO wildcard/video/audio
        const isSticker = (acc.includes('image/webp') || acc.includes('image/png') || acc.includes('image/gif'))
          && !acc.includes('image/*') && !acc.includes('video') && !acc.includes('audio') && acc.trim() !== '*' && !acc.includes('*/*');
        if (!isSticker) return inp;
      }
    }
    return null;
  }

  async function clickAttach() {
    const sels = [
      '[data-icon="plus-rounded"]', '[data-icon="plus"]',
      '[data-icon="attach-menu-plus"]', '[data-icon="clip"]',
      'button[title*="Attach" i]', 'button[aria-label*="Attach" i]',
    ];
    for (const s of sels) {
      const el = $(s);
      if (el) {
        let t = el;
        if (t.tagName !== "BUTTON") {
          const b = t.closest('button, [role="button"]');
          if (b) t = b;
        }
        try { t.click(); return true; } catch (_) {}
      }
    }
    return false;
  }

  try {
    if (!(await clickAttach())) return { ok: false, error: "Attach button nahi mila." };
    await sleep(700);

    const targets = targetMenuLabel(${JSON.stringify(attachment_mime || "")}, ${JSON.stringify(attachment_name || "")});

    // Wait up to 5s for the menu items to appear and find the right one
    let menuItem = null, input = null;
    const end = Date.now() + 5000;
    while (Date.now() < end) {
      menuItem = findMenuItem(targets);
      if (menuItem) {
        input = inputForMenuItem(menuItem);
        if (input) break;
      }
      await sleep(200);
    }

    // Fallback — if we didn't find a specific menu item, use any non-sticker input
    if (!input) {
      const allInputs = $$('input[type="file"]').filter(i => {
        const a = (i.getAttribute('accept') || '').toLowerCase();
        const isSticker = (a.includes('image/webp') || a.includes('image/png') || a.includes('image/gif'))
          && !a.includes('image/*') && !a.includes('video') && !a.includes('audio') && a.trim() !== '*' && !a.includes('*/*');
        return !isSticker;
      });
      // Pick the LARGEST accept attribute (Photos & Videos is the biggest)
      // — sticker has shortest, document has '*', photos has many MIMEs
      allInputs.sort((x, y) => (y.getAttribute('accept') || '').length - (x.getAttribute('accept') || '').length);
      input = allInputs[0] || null;
    }
    if (!input) return { ok: false, error: "Photos & Videos file input nahi mila." };

    const b64 = ${JSON.stringify(attachment_b64 || "")};
    const bytes = atob(b64);
    const buf = new Uint8Array(bytes.length);
    for (let i = 0; i < bytes.length; i++) buf[i] = bytes.charCodeAt(i);
    const mime = ${JSON.stringify(attachment_mime || "application/octet-stream")};
    const name = ${JSON.stringify(attachment_name || "file")};
    const blob = new Blob([buf], { type: mime });
    const file = new File([blob], name, { type: mime });

    const dt = new DataTransfer();
    dt.items.add(file);
    input.files = dt.files;
    input.dispatchEvent(new Event('change', { bubbles: true }));
    await sleep(2500);

    // Return which input was used for diagnostic
    return {
      ok: true,
      usedAccept: (input.getAttribute('accept') || '').slice(0, 100),
      menuItemText: menuItem ? elText(menuItem).slice(0, 80) : '(no menu item — used fallback)',
    };
  } catch (e) {
    return { ok: false, error: String(e && e.message || e) };
  }
})()
`;
          const upRes = await CDP.evaluate(tab.id, uploadScript, { awaitPromise: true });
          if (!upRes || !upRes.ok) {
            return { ok: false, error: "Attachment upload fail: " + (upRes && upRes.error || "unknown") };
          }

          // Type optional caption — preview's caption box is already focused
          if (message && message.trim()) {
            await CDP.typeText(tab.id, message);
            await sleep(400);
          }

          // CRITICAL: In WA's preview dialog, Enter does NOT always send —
          // for PDFs/documents/videos, only the green send button works.
          // Click it via DOM. Fallback to Enter only if button not found.
          const sendResult = await CDP.evaluate(tab.id, `(async () => {
            const sleep = (ms) => new Promise(r => setTimeout(r, ms));
            const $ = (s) => document.querySelector(s);
            const $$ = (s) => Array.from(document.querySelectorAll(s));

            // Wait up to 4s for the upload to finish and Send button to appear
            const end = Date.now() + 4000;
            while (Date.now() < end) {
              const sels = [
                'div[role="button"][aria-label="Send"]',
                'button[aria-label="Send"]',
                'span[data-icon="send"]',
                'span[data-icon="wds-ic-send-filled"]',
                'div[role="button"] span[data-icon="send"]',
                'button[data-tab="11"]',
              ];
              for (const sel of sels) {
                const el = $(sel);
                if (el) {
                  let btn = el;
                  if (btn.tagName !== "BUTTON" && btn.getAttribute('role') !== 'button') {
                    const b = btn.closest('button, [role="button"]');
                    if (b) btn = b;
                  }
                  try { btn.click(); return { clicked: true, sel }; } catch (_) {}
                }
              }
              await sleep(200);
            }
            return { clicked: false };
          })()`, { awaitPromise: true });

          if (!sendResult || !sendResult.clicked) {
            // Fallback — keyboard Enter (works for some media types)
            await CDP.key(tab.id, "Enter");
          }

          // CRITICAL — verify the send actually landed in the chat. Just
          // checking the preview state is unreliable: WhatsApp can close the
          // preview without sending (silent reject) and our code would
          // wrongly report success.
          //
          // Real verification: poll the chat body for the filename or caption.
          // If neither appears in the chat tail within 8 seconds → not sent.
          const verifyResult = await CDP.evaluate(tab.id, `(async () => {
            const sleep = (ms) => new Promise(r => setTimeout(r, ms));
            const fname = ${JSON.stringify(attachment_name || "")};
            const caption = ${JSON.stringify((message || "").trim())};
            // Drop the extension off the filename — WA usually shows the
            // bare name in chat without the extension (or with a different
            // rendering).
            const fnameNoExt = (fname || "").replace(/\\.[^/.]+$/, "").toLowerCase();
            const fnameLow = (fname || "").toLowerCase();
            const capLow = (caption || "").toLowerCase();

            // Common WhatsApp error strings to detect rejection
            const errPatterns = [
              /not supported/i,
              /too large/i,
              /failed to send/i,
              /file type.*not.*supported/i,
              /exceeds.*limit/i,
              /can.t send.*file/i,
            ];

            const end = Date.now() + 8000;
            let foundInChat = false;
            let lastBody = "";
            while (Date.now() < end) {
              const body = (document.body.innerText || "");
              lastBody = body;

              // Check for explicit WA error in the page
              for (const p of errPatterns) {
                const m = body.match(p);
                if (m) {
                  const idx = body.indexOf(m[0]);
                  return { sent: false, error: body.slice(Math.max(0, idx-30), idx+100).trim() };
                }
              }

              // Search the LAST ~4000 chars for filename or caption (most
              // recent chat messages are at the tail)
              const tail = body.slice(-4000).toLowerCase();
              if (fnameLow && tail.includes(fnameLow)) { foundInChat = true; break; }
              if (fnameNoExt && fnameNoExt.length > 4 && tail.includes(fnameNoExt)) { foundInChat = true; break; }
              if (capLow && tail.includes(capLow)) { foundInChat = true; break; }

              await sleep(400);
            }

            if (foundInChat) return { sent: true };

            // Last-resort signal: check whether preview is still open. If
            // still open after 8 seconds, we know send didn't fire.
            const previewSels = [
              'div[role="dialog"]',
              'div[aria-label*="preview" i]',
            ];
            for (const s of previewSels) {
              if (document.querySelector(s)) {
                return { sent: false, error: "Preview dialog abhi bhi khuli — file send nahi hua." };
              }
            }
            return { sent: false, error: "Chat mein file/caption nahi mila 8 sec mein — send confirm nahi hua." };
          })()`, { awaitPromise: true });

          if (verifyResult && verifyResult.sent === false) {
            return {
              ok: false,
              error: "Attachment send FAIL: " + (verifyResult.error || "unknown"),
            };
          }
          return { ok: true, withAttachment: true, verified: true };
        }

        // No attachment — just type and send via keyboard
        if (message) {
          await CDP.typeText(tab.id, message);
          await sleep(400);
        }
        await CDP.key(tab.id, "Enter");
        await sleep(1500);

        // Verify message landed (best-effort scan of chat tail)
        const verified = await CDP.evaluate(tab.id, `(() => {
          const body = (document.body.innerText || "").slice(-3000);
          return body.includes(${JSON.stringify((message || "").trim())});
        })()`);

        return { ok: true, verified };
      } catch (e) {
        return { ok: false, error: String(e && e.message || e) };
      }
    });

    // If keyboard path signalled "needsDeeplink", do the deeplink navigation
    // OUTSIDE the withSession block — that way we don't fight the debugger's
    // own attach/detach lifecycle. Navigation kills the debug session by
    // design; we open a fresh session after the new URL loads.
    if (sessionResult && sessionResult.needsDeeplink) {
      const deeplink = "https://web.whatsapp.com/send?phone=" + sessionResult.cleanPhone;
      try {
        await chrome.tabs.update(tab.id, { url: deeplink, active: false });
        await waitForTabLoaded(tab.id, 25000);
        await sleep(3000);  // give WA's chat UI time to render
      } catch (e) {
        return { ok: false, error: "Deeplink navigation fail: " + String(e?.message || e) };
      }

      return await CDP.withSession(tab.id, async () => {
        try {
          // Check that the chat actually opened (or that the number is invalid)
          const status = await CDP.evaluate(tab.id, `(() => {
            const body = document.body.innerText || "";
            if (/isn.t on WhatsApp|phone number shared.*invalid/i.test(body)) {
              return { ok: false, reason: "invalid" };
            }
            const eds = document.querySelectorAll('[contenteditable="true"]');
            for (const el of eds) {
              const ph = (el.getAttribute('aria-placeholder') || '').toLowerCase();
              const t = el.getAttribute('data-tab') || '';
              if (t === '10' || ph.includes('message')) return { ok: true };
            }
            return { ok: false, reason: "no_compose" };
          })()`);

          if (!status.ok) {
            if (status.reason === "invalid") {
              return { ok: false, error: "Phone " + sessionResult.cleanPhone + " WhatsApp pe registered nahi." };
            }
            return { ok: false, error: "Chat load nahi hua deeplink ke baad." };
          }

          // Compose box is the focus — type message and Enter
          if (message) {
            await CDP.typeText(tab.id, message);
            await sleep(400);
          }
          await CDP.key(tab.id, "Enter");
          await sleep(1500);
          return { ok: true, viaDeeplink: true };
        } catch (e) {
          return { ok: false, error: "Deeplink send fail: " + String(e?.message || e) };
        }
      });
    }

    return sessionResult;
  }

  // Legacy DOM-based path (kept dead-code-eliminated below — wrapping in
  // an unreachable block since we always return above for whatsapp_send).
  if (false) {
    // Try CDP send first (search-based, fully background).
    // If that fails AND we have a phone number, fall back to wa.me deeplink
    // navigation — which works even for numbers NOT in the user's contacts.
    // The deeplink path requires us to detach the debugger, navigate via
    // chrome.tabs.update (background tab, no focus steal), wait for the new
    // page to load, then reattach the debugger and finish the send. This
    // avoids the "Inspected target navigated" error that killed earlier runs.

    // The page-side function — runs in WhatsApp Web's page context via
    // Runtime.evaluate. Returns { ok, error, verified }.
    const pageScript = `
(async () => {
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));

  function setText(el, text) {
    // Robust contenteditable text input for WhatsApp Web's React UI.
    // Tries execCommand (most reliable for contenteditable) but also fires
    // input events so React picks up the change. Without focus, the search
    // results list won't populate — accept the brief focus, the JARVIS
    // window stays minimized anyway.
    el.focus();
    el.click();
    document.execCommand("selectAll", false, null);
    document.execCommand("delete", false, null);
    document.execCommand("insertText", false, text);
    // Also dispatch a synthetic input event so any React listeners that
    // bypass execCommand still see the change.
    el.dispatchEvent(new InputEvent("input", {
      bubbles: true, cancelable: false, inputType: "insertText", data: text,
    }));
  }

  function isLoggedOut() {
    return !!$('canvas[aria-label*="QR" i]');
  }

  // Wait for WhatsApp Web to fully boot before looking for the search box.
  // Cold loads can take 10-25s before the chat-list pane is in the DOM.
  async function waitForWaReady(timeout) {
    const end = Date.now() + (timeout || 30000);
    while (Date.now() < end) {
      if (isLoggedOut()) throw new Error("WhatsApp Web logged out — QR scan zaroori.");
      // Either the chat-list pane OR a search-like input means WA is interactive
      if ($('#pane-side') || $('div[aria-label*="Chat list" i]') || $('[role="grid"]')) {
        return true;
      }
      await sleep(400);
    }
    return false;
  }

  async function findSearch(timeout) {
    // Wait for WA to be interactive first
    const ready = await waitForWaReady(timeout || 30000);
    if (!ready) {
      throw new Error("WhatsApp Web 30 sec mein load nahi hua. Refresh karo aur retry.");
    }

    // Specific selectors first (fast path when WA UI matches expected DOM)
    const sels = [
      'div[contenteditable="true"][data-tab="3"]',
      'div[role="textbox"][contenteditable="true"][title*="Search" i]',
      'div[role="textbox"][contenteditable="true"][aria-label*="Search" i]',
      'div[aria-label="Search input textbox"]',
      'div[aria-placeholder*="Search" i]',
      'p.selectable-text[contenteditable="true"]',
      '#pane-side div[contenteditable="true"]',
      'div[data-testid="chat-list-search"] div[contenteditable="true"]',
      'div[id="side"] div[contenteditable="true"]',
      'header div[contenteditable="true"]',
    ];
    const end = Date.now() + 15000;
    while (Date.now() < end) {
      if (isLoggedOut()) throw new Error("WhatsApp Web logged out — QR scan zaroori.");
      for (const s of sels) {
        const el = $(s);
        if (el) return el;
      }

      // BROAD FALLBACK: any contenteditable on the page that's NOT the chat
      // compose box. The chat compose has data-tab="10" or aria-placeholder
      // containing "Type a message". Anything else that's editable is most
      // likely the search box.
      const allEditable = $$('[contenteditable="true"]');
      for (const el of allEditable) {
        const tab = el.getAttribute('data-tab') || '';
        const ph = (el.getAttribute('aria-placeholder') || '').toLowerCase();
        const lbl = (el.getAttribute('aria-label') || '').toLowerCase();
        // Skip chat compose
        if (tab === '10' || ph.includes('type a message') || ph.includes('message')) continue;
        if (lbl.includes('type a message')) continue;
        // Pick the first remaining contenteditable — that's the search
        return el;
      }

      await sleep(300);
    }

    // Diagnostic: dump ALL contenteditable on the page so we can see what's there
    const all = $$('[contenteditable="true"]').slice(0, 5).map(e => {
      const attrs = [];
      for (const a of e.attributes || []) attrs.push(a.name + '=' + JSON.stringify(a.value));
      return '<' + e.tagName + ' ' + attrs.join(' ').slice(0, 200) + '>';
    });
    throw new Error("WA search box nahi mila. Page mein contenteditable elements: " + JSON.stringify(all));
  }

  // Fallback path: scan the chat list (left rail) directly for a matching
  // chat title. WhatsApp shows recent chats as a side list — if the contact
  // has been messaged before, their row is in here even without searching.
  async function findChatInList(name, timeout) {
    const end = Date.now() + (timeout || 5000);
    const low = name.toLowerCase();
    while (Date.now() < end) {
      // Multiple selectors for the chat rows (WA changes these occasionally)
      const candidateSelectors = [
        '#pane-side div[role="listitem"]',
        '#pane-side [data-testid="cell-frame-container"]',
        'div[aria-label*="Chat list" i] div[role="listitem"]',
        'div[role="grid"] div[role="row"]',
      ];
      for (const sel of candidateSelectors) {
        const rows = $$(sel);
        for (const r of rows) {
          // Look for the chat title specifically (span with dir="auto" is
          // typical for the contact name in WA's row layout).
          const titleEl =
            r.querySelector('span[dir="auto"][title]') ||
            r.querySelector('span[title]') ||
            r.querySelector('span[dir="auto"]');
          const title = titleEl ? (titleEl.getAttribute('title') || titleEl.innerText || "").trim() : "";
          if (title && title.toLowerCase().includes(low)) {
            return r;
          }
        }
      }
      await sleep(250);
    }
    return null;
  }

  async function findCompose(timeout) {
    const sels = [
      'div[contenteditable="true"][data-tab="10"]',
      'div[role="textbox"][contenteditable="true"][aria-placeholder*="Type" i]',
      'div[role="textbox"][contenteditable="true"][data-lexical-editor="true"]',
    ];
    const end = Date.now() + (timeout || 15000);
    while (Date.now() < end) {
      for (const s of sels) { const el = $(s); if (el) return el; }
      await sleep(300);
    }
    return null;
  }

  async function findSendBtn(timeout) {
    const sels = [
      'button[aria-label="Send"]',
      'button[data-tab="11"]',
      'span[data-icon="send"]',
      'span[data-testid="send"]',
      '[data-icon="send-2"]',
    ];
    const end = Date.now() + (timeout || 4000);
    while (Date.now() < end) {
      for (const s of sels) {
        const el = $(s);
        if (el) {
          let t = el;
          if (t.tagName !== "BUTTON") {
            const b = t.closest('button, [role="button"]');
            if (b) t = b;
          }
          return t;
        }
      }
      await sleep(200);
    }
    return null;
  }

  // BULLETPROOF name search — broad scan + multiple match tiers.
  //
  // Strategy:
  //   1. Use WhatsApp's search box (Ctrl+Alt+/ shortcut activates focus)
  //   2. Type the name — React picks it up via execCommand + input event
  //   3. Wait for WA to filter
  //   4. Scan the ENTIRE LEFT PANE for any element whose title/innerText
  //      contains the name (not just specific listitem selectors which change
  //      between WA Web versions)
  //   5. Click the best match
  //   6. If nothing → return diagnostic with what WA actually shows
  async function openByName(name) {
    const wanted = name.trim();
    const wantedLow = wanted.toLowerCase();
    const firstWord = wanted.split(/\s+/)[0];
    const firstLow = firstWord.toLowerCase();

    // Focus the search box. WhatsApp also responds to Ctrl+Alt+/ to focus
    // search, but a direct click is more reliable across versions.
    let search;
    try {
      search = await findSearch(15000);
      search.click();
      await sleep(200);
      search.focus();
    } catch (e) {
      throw new Error("WA search box nahi mila — page abhi ready nahi.");
    }

    // Type the name. We type the FULL name; if no match found later, we'll
    // re-type with just the first word.
    setText(search, wanted);
    await sleep(1800);  // give WA time to filter

    // Broad scan: collect every element on the page that has a visible name
    // text. WA renders contact rows in many shells across versions, so we
    // cast a wide net and filter by what matters — does the text contain
    // the contact name?
    function collectCandidates() {
      // All elements with a title attribute (WA uses these for contact names)
      const withTitle = $$('span[title]');
      // All listitem/row elements in the left pane
      const leftPane = $('#pane-side') || $('div[aria-label*="Chat list" i]') || document;
      const rows = $$('[role="listitem"], [role="row"], [role="option"], [role="button"]', leftPane);
      // Merge and de-dup
      const set = new Set();
      const all = [];
      for (const el of [...withTitle, ...rows]) {
        // Walk up to the clickable parent (usually a row container)
        let target = el;
        for (let i = 0; i < 8 && target; i++) {
          if (
            target.getAttribute('role') === 'listitem' ||
            target.getAttribute('role') === 'row' ||
            target.getAttribute('role') === 'option' ||
            target.getAttribute('role') === 'button' ||
            (target.tagName === 'DIV' && target.querySelector('span[title]'))
          ) {
            break;
          }
          target = target.parentElement;
        }
        if (!target || set.has(target)) continue;
        set.add(target);
        all.push(target);
      }
      return all;
    }

    function nameOf(el) {
      // Prefer span[title] which is WA's stable contact-name marker
      const titleEl = el.querySelector('span[title]');
      if (titleEl) {
        const t = (titleEl.getAttribute('title') || titleEl.innerText || '').trim();
        if (t) return t;
      }
      // Fallback: first line of innerText. Split on the newline escape
      // (written as \\\\n so the outer template literal renders \\n for
      // the page-side JS to parse as a newline string).
      const t = (el.innerText || '').trim().split('\\n')[0];
      return t;
    }

    function pick(elements, predicate) {
      for (const el of elements) {
        const t = nameOf(el).toLowerCase();
        if (t && predicate(t)) return el;
      }
      return null;
    }

    async function attemptMatch() {
      const cands = collectCandidates();
      // Track for diagnostic
      const seen = cands.map(nameOf).filter(Boolean);

      // Tier 1: exact match (case-insensitive)
      let row = pick(cands, t => t === wantedLow);
      if (row) return { row, seen };
      // Tier 2: starts with full wanted ("Saif" → "Saif Khan")
      row = pick(cands, t => t.startsWith(wantedLow));
      if (row) return { row, seen };
      // Tier 3: contains full wanted anywhere
      row = pick(cands, t => t.includes(wantedLow));
      if (row) return { row, seen };
      // Tier 4: starts with first word ("Zaid Zenesa" → "Zaid Khan")
      if (firstLow && firstLow !== wantedLow) {
        row = pick(cands, t => t.startsWith(firstLow));
        if (row) return { row, seen };
        // Tier 5: contains first word
        row = pick(cands, t => t.includes(firstLow));
        if (row) return { row, seen };
      }
      return { row: null, seen };
    }

    // First attempt — with full name typed
    let result = await attemptMatch();

    // If nothing — retry with just first word (sometimes WA contacts are
    // saved as just "Zaid" even when the user types "Zaid Zenesa")
    if (!result.row && firstWord && firstWord !== wanted) {
      setText(search, firstWord);
      await sleep(1500);
      const second = await attemptMatch();
      if (second.row) {
        result = second;
      } else {
        // Combine seen lists for the diagnostic
        const combined = [...result.seen];
        for (const t of second.seen) if (!combined.includes(t)) combined.push(t);
        result = { row: null, seen: combined };
      }
    }

    if (result.row) {
      result.row.click();
      await sleep(900);
      return;
    }

    const top = result.seen.slice(0, 8);
    const list = top.length
      ? " | WhatsApp pe yeh dikh raha: " + top.join(", ")
      : " | WhatsApp se kuch contacts read nahi hue.";
    throw new Error("'" + name + "' WA mein nahi mila." + list);
  }

  async function openByPhone(phone) {
    // No more wa.me deeplink navigation — that was reloading the tab and
    // killing the debugger session ("Inspected target navigated"). Use the
    // search box just like openByName — WA search accepts phone numbers too.
    const search = await findSearch();
    search.click();
    await sleep(150);
    setText(search, phone);
    await sleep(900);
    const end = Date.now() + 6000;
    while (Date.now() < end) {
      const results = $$('div[role="listitem"]');
      if (results.length > 0) {
        // First result with non-empty text — open it
        for (const r of results) {
          const t = (r.innerText || "").trim();
          if (t) { r.click(); await sleep(800); return; }
        }
      }
      await sleep(250);
    }
    throw new Error("Phone " + phone + " WhatsApp pe nahi mila — number save hai contacts mein?");
  }

  async function sendMsg(text) {
    const compose = await findCompose(10000);
    if (!compose) throw new Error("Compose box nahi mila.");
    setText(compose, text);
    await sleep(500);
    const btn = await findSendBtn(3000);
    if (btn) btn.click();
    else {
      compose.dispatchEvent(new KeyboardEvent("keydown", {
        key: "Enter", code: "Enter", which: 13, keyCode: 13, bubbles: true,
      }));
    }
    await sleep(1200);
  }

  // Find the right "attach" button (paperclip / plus icon) and click it
  // to reveal WhatsApp's file inputs.
  async function clickAttach(timeout) {
    const sels = [
      '[data-icon="plus-rounded"]',
      '[data-icon="plus"]',
      '[data-icon="attach-menu-plus"]',
      '[data-icon="clip"]',
      'button[title*="Attach" i]',
      'div[title*="Attach" i]',
      'span[data-icon="attach-menu-plus"]',
    ];
    const end = Date.now() + (timeout || 5000);
    while (Date.now() < end) {
      for (const s of sels) {
        const el = $(s);
        if (el) {
          let target = el;
          if (target.tagName !== "BUTTON" && target.getAttribute('role') !== 'button') {
            const b = target.closest('button, [role="button"]');
            if (b) target = b;
          }
          try { target.click(); return true; } catch (_) {}
        }
      }
      await sleep(200);
    }
    return false;
  }

  // Detect whether an input is the STICKER input — we never want this.
  // Sticker inputs typically accept specific image types only (image/webp,
  // image/png, image/gif) without the image/* wildcard and without video.
  // Photos input has image/* + video. Document input has * or *\/*.
  function isStickerInput(accept) {
    const a = (accept || "").toLowerCase();
    if (!a) return false;
    // Has wildcard or video → definitely NOT sticker
    if (a.includes('image/*')) return false;
    if (a.includes('video')) return false;
    if (a.includes('audio')) return false;
    if (a.includes('*/*') || a.trim() === '*') return false;
    // Pure specific image types only → sticker
    return a.includes('image/webp') || a.includes('image/png') || a.includes('image/gif');
  }

  // Pick the right hidden <input type="file"> for this attachment type.
  //   - Media (images/videos) → "Photos & Videos" input
  //   - Documents (pdf/docx/zip/etc) → "Document" input
  //   - Audio → "Audio" input
  //   - NEVER the "Sticker" input — logos and images must go as media
  function pickFileInput(mime, fileName) {
    // STEP 1: Filter out sticker inputs upfront — bulletproof guarantee
    // logos/photos never go as stickers
    const allInputs = $$('input[type="file"]');
    const inputs = allInputs.filter(inp => !isStickerInput(inp.getAttribute('accept')));
    if (!inputs.length) return null;

    const lowerMime = (mime || "").toLowerCase();
    const ext = (fileName || "").toLowerCase().split('.').pop();
    const isMedia =
      lowerMime.startsWith('image/') ||
      lowerMime.startsWith('video/') ||
      ['jpg','jpeg','png','gif','webp','heic','mp4','mov','3gp','webm','mkv'].includes(ext);
    const isAudio =
      lowerMime.startsWith('audio/') ||
      ['mp3','wav','ogg','m4a','aac','flac'].includes(ext);

    if (isMedia) {
      // Tier 1: Photos & Videos input (image/* + video) — most specific
      for (const inp of inputs) {
        const a = (inp.getAttribute('accept') || '').toLowerCase();
        if (a.includes('image/*') && a.includes('video')) return inp;
      }
      // Tier 2: any input accepting image/* (catch-all wildcard, never sticker)
      for (const inp of inputs) {
        const a = (inp.getAttribute('accept') || '').toLowerCase();
        if (a.includes('image/*')) return inp;
      }
    }
    if (isAudio) {
      for (const inp of inputs) {
        const a = (inp.getAttribute('accept') || '').toLowerCase();
        if (a.includes('audio')) return inp;
      }
    }
    // Document path — most permissive accept
    for (const inp of inputs) {
      const a = (inp.getAttribute('accept') || '').trim();
      if (a === '*' || a === '*/*' || a === '') return inp;
    }
    // Last-resort: any non-sticker input
    return inputs[inputs.length - 1];
  }

  // Decode a base64 string to a Blob with the given MIME type.
  function b64ToBlob(b64, mime) {
    const bytes = atob(b64);
    const buf = new Uint8Array(bytes.length);
    for (let i = 0; i < bytes.length; i++) buf[i] = bytes.charCodeAt(i);
    return new Blob([buf], { type: mime || 'application/octet-stream' });
  }

  // Upload an attachment to the currently-open chat. Optional caption is
  // typed in the preview compose box before send. Handles images, videos,
  // PDFs, documents, audio, and any other binary the user passes in.
  async function uploadAttachment(b64, fileName, mime, caption) {
    const ok = await clickAttach(5000);
    if (!ok) throw new Error("WhatsApp attach button (paperclip) nahi mila.");
    await sleep(700);

    // Some WA versions reveal a menu before the file input becomes interactive.
    // Wait for any file input to be in the DOM.
    let input = null;
    const deadline = Date.now() + 5000;
    while (Date.now() < deadline) {
      input = pickFileInput(mime, fileName);
      if (input) break;
      await sleep(200);
    }
    if (!input) throw new Error("WhatsApp file input nahi mila.");

    // Build a real File and set it on the input — this triggers WA's upload flow.
    const blob = b64ToBlob(b64, mime || 'application/octet-stream');
    const file = new File([blob], fileName || 'file', { type: mime || 'application/octet-stream' });
    const dt = new DataTransfer();
    dt.items.add(file);
    input.files = dt.files;
    input.dispatchEvent(new Event('change', { bubbles: true }));

    // Let WA render the preview / progress UI
    await sleep(2500);

    // Type the caption in the preview's compose box if provided
    if (caption && caption.trim()) {
      // The preview's caption box may differ from the chat compose box.
      const capSelectors = [
        'div[role="textbox"][contenteditable="true"][aria-placeholder*="caption" i]',
        'div[contenteditable="true"][data-tab="10"]',
        'div[role="textbox"][contenteditable="true"]',
      ];
      let cap = null;
      const capEnd = Date.now() + 3000;
      while (!cap && Date.now() < capEnd) {
        for (const s of capSelectors) { cap = $(s); if (cap) break; }
        if (!cap) await sleep(200);
      }
      if (cap) {
        setText(cap, caption);
        await sleep(400);
      }
    }

    // Find and click the send button in the preview dialog
    const sendBtn = await findSendBtn(6000);
    if (!sendBtn) throw new Error("Send button nahi mila preview mein.");
    sendBtn.click();
    await sleep(3000);  // upload + send time
    return true;
  }

  async function verify(text, timeout) {
    const end = Date.now() + (timeout || 4000);
    const needle = (text || "").trim();
    if (!needle) return true;
    while (Date.now() < end) {
      const body = document.body.innerText || "";
      if (body.slice(-3000).includes(needle)) return true;
      await sleep(300);
    }
    return false;
  }

  try {
    if (isLoggedOut()) return { ok: false, error: "WhatsApp Web logged out — QR scan zaroori." };
    const recipient = ${JSON.stringify(recipient || "")};
    const phone = ${JSON.stringify(phone || "")};
    const message = ${JSON.stringify(message || "")};
    const attachment_b64 = ${JSON.stringify(attachment_b64 || "")};
    const attachment_name = ${JSON.stringify(attachment_name || "")};
    const attachment_mime = ${JSON.stringify(attachment_mime || "")};

    let opened = false;
    if (recipient && recipient.trim()) {
      try { await openByName(recipient.trim()); opened = true; }
      catch (e) { if (!phone) throw e; }
    }
    if (!opened) {
      if (phone) await openByPhone(phone);
      else throw new Error("Recipient ya phone dena hoga.");
    }

    if (attachment_b64) {
      // Send file (with optional caption = message)
      await uploadAttachment(attachment_b64, attachment_name, attachment_mime, message);
      return { ok: true, verified: true, withAttachment: true };
    }

    await sendMsg(message);
    const v = await verify(message);
    return { ok: true, verified: v };
  } catch (e) {
    return { ok: false, error: String(e && e.message || e) };
  }
})()
`;

    // Strategy:
    //   - If attachment present → ALWAYS use main pageScript (it has upload logic).
    //     The deeplink fallback's send-only path doesn't support attachments.
    //   - Else if phone → deeplink directly (most reliable, works for unsaved numbers)
    //   - Else (name only) → search in user's contacts
    //   - If search fails AND we have a phone fallback → deeplink (text-only)
    const looksLikePhone = recipient && /^[+\d\s\-()]+$/.test(String(recipient).trim()) && /\d{7,}/.test(String(recipient));
    const hasAttachment = !!(params && params.attachment_b64);
    const preferDeeplink = (!!phone || looksLikePhone) && !hasAttachment;
    let result;

    if (preferDeeplink) {
      // Skip search — go straight to deeplink
      result = { ok: false, error: "use_deeplink" };
    } else {
      result = await CDP.withSession(tab.id, async () => {
        const r = await CDP.evaluate(tab.id, pageScript, { awaitPromise: true });
        return r || { ok: false, error: "no result" };
      });
      if (result.ok) return result;
    }

    // Deeplink path (initial OR fallback): navigate to wa.me/send?phone=...
    // Only if NO attachment — deeplink's send-only script can't upload files.
    const errStr = (result.error || "").toLowerCase();
    const shouldDeeplink = (preferDeeplink || errStr.includes("nahi mila") || errStr.includes("not found")) && !hasAttachment;
    const phoneForLink = phone || (looksLikePhone ? String(recipient).replace(/[^\d]/g, "") : "");
    if (shouldDeeplink && phoneForLink) {
      const cleanPhone = phoneForLink.replace(/[^\d]/g, "");
      if (cleanPhone) {
        const deeplink = `https://web.whatsapp.com/send?phone=${cleanPhone}`;
        try {
          // Navigate WITHOUT activating the tab. chrome.tabs.update from the
          // background page doesn't fire the focus events that location.href
          // from the page itself would.
          await chrome.tabs.update(tab.id, { url: deeplink, active: false });
          await waitForTabLoaded(tab.id, 25000);
          await sleep(2500);  // Let WA render the chat after URL load

          // Now send the message via CDP on the now-loaded chat
          const sendOnlyScript = `
(async () => {
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const $ = (s) => document.querySelector(s);
  async function findCompose(timeout) {
    const sels = [
      'div[contenteditable="true"][data-tab="10"]',
      'div[role="textbox"][contenteditable="true"][aria-placeholder*="Type" i]',
      'div[role="textbox"][contenteditable="true"][data-lexical-editor="true"]',
    ];
    const end = Date.now() + (timeout || 15000);
    while (Date.now() < end) {
      for (const s of sels) { const el = $(s); if (el) return el; }
      await sleep(300);
    }
    return null;
  }
  function setText(el, text) {
    el.focus();
    document.execCommand("selectAll", false, null);
    document.execCommand("delete", false, null);
    document.execCommand("insertText", false, text);
  }
  async function findSendBtn(timeout) {
    const sels = [
      'button[aria-label="Send"]',
      'button[data-tab="11"]',
      'span[data-icon="send"]',
      'span[data-testid="send"]',
      '[data-icon="send-2"]',
    ];
    const end = Date.now() + (timeout || 4000);
    while (Date.now() < end) {
      for (const s of sels) {
        const el = $(s);
        if (el) {
          let t = el;
          if (t.tagName !== "BUTTON") { const b = t.closest('button, [role="button"]'); if (b) t = b; }
          return t;
        }
      }
      await sleep(200);
    }
    return null;
  }
  try {
    if (document.querySelector('canvas[aria-label*="QR" i]')) {
      return { ok: false, error: "WhatsApp Web logged out — QR scan zaroori." };
    }
    // Check for "Phone number shared via URL is invalid" error
    const errNode = document.body.innerText || "";
    if (/isn.t on WhatsApp|phone number shared.*invalid/i.test(errNode)) {
      return { ok: false, error: "Yeh number WhatsApp pe registered nahi hai." };
    }
    const compose = await findCompose(20000);
    if (!compose) return { ok: false, error: "Chat load nahi hua deeplink ke baad." };
    setText(compose, ${JSON.stringify(message)});
    await sleep(500);
    const btn = await findSendBtn(3000);
    if (btn) btn.click();
    else compose.dispatchEvent(new KeyboardEvent("keydown", {
      key: "Enter", code: "Enter", which: 13, keyCode: 13, bubbles: true,
    }));
    await sleep(1500);
    // Verify
    const needle = ${JSON.stringify(message)}.trim();
    const end = Date.now() + 4000;
    while (Date.now() < end) {
      if ((document.body.innerText || "").slice(-3000).includes(needle)) {
        return { ok: true, verified: true, via: "deeplink" };
      }
      await sleep(300);
    }
    return { ok: true, verified: false, via: "deeplink" };
  } catch (e) {
    return { ok: false, error: String(e && e.message || e) };
  }
})()
`;
          result = await CDP.withSession(tab.id, async () => {
            return await CDP.evaluate(tab.id, sendOnlyScript, { awaitPromise: true });
          });
        } catch (e) {
          return { ok: false, error: "Deeplink fallback fail: " + String(e?.message || e) };
        }
      }
    }
    return result;
  }

  return { ok: false, error: `unknown wa action: ${action}` };
}

async function actTeams(action, params) {
  const tab = await ensureTab(
    ["https://teams.microsoft.com/", "https://teams.live.com/"],
    "https://teams.microsoft.com/",
  );
  try {
    const t = await chrome.tabs.get(tab.id);
    if (t.status !== "complete") await sleep(2500); else await sleep(500);
  } catch (_) { await sleep(500); }

  if (action === "teams_ping") {
    return await CDP.withSession(tab.id, async () => {
      return await CDP.evaluate(tab.id, `(() => ({
        loggedOut: /login\\.(microsoftonline|live)\\.com/.test(location.href),
        url: location.href,
      }))()`);
    });
  }

  if (action === "teams_send") {
    const { recipient, message, attachment_b64, attachment_name, attachment_mime } = params || {};
    if (!recipient) return { ok: false, error: "Recipient name dena hoga" };
    if (!message && !attachment_b64) return { ok: false, error: "Message text ya attachment dena hoga" };

    const pageScript = `
(async () => {
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));

  function isLoggedOut() {
    return /login\\.(microsoftonline|live)\\.com/.test(location.href);
  }

  function setText(el, text) {
    el.focus();
    document.execCommand("selectAll", false, null);
    document.execCommand("delete", false, null);
    document.execCommand("insertText", false, text);
    el.dispatchEvent(new InputEvent("input", {
      bubbles: true, cancelable: false, inputType: "insertText", data: text,
    }));
  }

  async function findCompose(timeout) {
    const sels = [
      'div[role="textbox"][contenteditable="true"][data-tid="ckeditor"]',
      'div[contenteditable="true"][role="textbox"]',
      'div.ck-editor__editable[contenteditable="true"]',
      'div[contenteditable="true"][aria-label*="message" i]',
      'div[contenteditable="true"][data-lexical-editor="true"]',
    ];
    const end = Date.now() + (timeout || 15000);
    while (Date.now() < end) {
      for (const s of sels) { const el = $(s); if (el) return el; }
      await sleep(300);
    }
    return null;
  }

  async function findSendButton(timeout) {
    const sels = [
      'button[data-tid="newMessageCommands-send"]',
      'button[aria-label*="Send" i]',
      'button[title*="Send" i]',
      '[data-icon-name="Send"]',
      'span[data-icon-name="Send"]',
    ];
    const end = Date.now() + (timeout || 5000);
    while (Date.now() < end) {
      for (const s of sels) {
        const el = $(s);
        if (el) {
          let t = el;
          if (t.tagName !== "BUTTON") {
            const b = t.closest('button, [role="button"]');
            if (b) t = b;
          }
          return t;
        }
      }
      await sleep(200);
    }
    return null;
  }

  async function openNewChatWith(name) {
    // Click "New chat" button
    const newChatSels = [
      'button[data-tid="chat-create-new-chat-button"]',
      'button[aria-label*="New chat" i]',
      'button[title*="New chat" i]',
    ];
    let btn = null;
    for (const s of newChatSels) { btn = $(s); if (btn) break; }
    if (btn) btn.click();
    await sleep(900);

    // Find people picker input
    const pickerSels = [
      'input[aria-label*="To" i]',
      'input[placeholder*="Type a name" i]',
      'div[contenteditable="true"][role="combobox"]',
    ];
    let picker = null;
    const pickerEnd = Date.now() + 5000;
    while (Date.now() < pickerEnd) {
      for (const s of pickerSels) { picker = $(s); if (picker) break; }
      if (picker) break;
      await sleep(200);
    }
    if (!picker) throw new Error("Teams 'To' picker nahi mila.");

    picker.focus();
    if (picker.tagName === 'INPUT') {
      picker.value = name;
      picker.dispatchEvent(new Event('input', { bubbles: true }));
    } else {
      setText(picker, name);
    }
    await sleep(1500);

    // Click first search result
    const resultEnd = Date.now() + 5000;
    while (Date.now() < resultEnd) {
      const result = $('li[role="option"]') || $('div[role="option"]');
      if (result) { result.click(); await sleep(700); return; }
      await sleep(250);
    }
    // Fallback — press Enter on picker
    picker.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    await sleep(800);
  }

  function isStickerInput(accept) {
    const a = (accept || "").toLowerCase();
    if (!a) return false;
    if (a.includes('image/*') || a.includes('video') || a.includes('audio')) return false;
    if (a.includes('*/*') || a.trim() === '*') return false;
    return a.includes('image/webp') || a.includes('image/png') || a.includes('image/gif');
  }

  function pickFileInput(mime, fileName) {
    const inputs = $$('input[type="file"]').filter(i => !isStickerInput(i.getAttribute('accept')));
    if (!inputs.length) return null;
    // Teams' file input is usually accept="*" — most permissive wins
    for (const inp of inputs) {
      const a = (inp.getAttribute('accept') || '').trim();
      if (a === '*' || a === '*/*' || a === '') return inp;
    }
    return inputs[inputs.length - 1];
  }

  function b64ToBlob(b64, mime) {
    const bytes = atob(b64);
    const buf = new Uint8Array(bytes.length);
    for (let i = 0; i < bytes.length; i++) buf[i] = bytes.charCodeAt(i);
    return new Blob([buf], { type: mime || 'application/octet-stream' });
  }

  async function uploadAttachment(b64, fileName, mime, caption) {
    // Find Teams attach button (paperclip)
    const attachSels = [
      'button[data-tid*="attach" i]',
      'button[aria-label*="Attach" i]',
      'button[title*="Attach" i]',
      '[data-tid="newMessageAttachButton"]',
    ];
    let btn = null;
    for (const s of attachSels) { btn = $(s); if (btn) break; }
    if (!btn) throw new Error("Teams attach button nahi mila.");
    btn.click();
    await sleep(700);

    // Click "Upload from this device" menu item if a menu opened
    const menuSels = [
      'div[role="menuitem"]:has-text("Upload from this device")',
      'button:has-text("Upload from this device")',
      '[data-tid="upload-from-device"]',
      '[data-tid*="upload" i]',
    ];
    for (const sel of menuSels) {
      try {
        const item = $(sel);
        if (item) { item.click(); await sleep(500); break; }
      } catch (_) {}
    }

    // Wait for file input
    let input = null;
    const inEnd = Date.now() + 5000;
    while (Date.now() < inEnd) {
      input = pickFileInput(mime, fileName);
      if (input) break;
      await sleep(200);
    }
    if (!input) throw new Error("Teams file input nahi mila.");

    const blob = b64ToBlob(b64, mime || 'application/octet-stream');
    const file = new File([blob], fileName || 'file', { type: mime || 'application/octet-stream' });
    const dt = new DataTransfer();
    dt.items.add(file);
    input.files = dt.files;
    input.dispatchEvent(new Event('change', { bubbles: true }));
    await sleep(3500);  // upload time

    // Type caption in compose
    if (caption && caption.trim()) {
      const compose = await findCompose(8000);
      if (compose) { setText(compose, caption); await sleep(400); }
    }

    // Send
    const send = await findSendButton(6000);
    if (!send) throw new Error("Teams send button nahi mila.");
    send.click();
    await sleep(2500);
    return true;
  }

  try {
    if (isLoggedOut()) return { ok: false, error: "Teams logged out — login karo pehle." };

    const recipient = ${JSON.stringify(recipient)};
    const message = ${JSON.stringify(message || "")};
    const attachment_b64 = ${JSON.stringify(attachment_b64 || "")};
    const attachment_name = ${JSON.stringify(attachment_name || "")};
    const attachment_mime = ${JSON.stringify(attachment_mime || "")};

    // If already on this chat (title contains recipient) skip the new-chat dance
    const title = (document.title || "").toLowerCase();
    if (!title.includes(recipient.toLowerCase())) {
      await openNewChatWith(recipient);
    }

    if (attachment_b64) {
      await uploadAttachment(attachment_b64, attachment_name, attachment_mime, message);
      return { ok: true, withAttachment: true };
    }

    const compose = await findCompose(15000);
    if (!compose) return { ok: false, error: "Teams compose box nahi mila." };
    setText(compose, message);
    await sleep(500);
    compose.focus();
    const sendBtn = await findSendButton(3000);
    if (sendBtn) sendBtn.click();
    else compose.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    await sleep(1500);
    return { ok: true };
  } catch (e) {
    return { ok: false, error: String(e && e.message || e) };
  }
})()
`;
    return await CDP.withSession(tab.id, async () => {
      const r = await CDP.evaluate(tab.id, pageScript, { awaitPromise: true });
      return r || { ok: false, error: "no result" };
    });
  }

  return { ok: false, error: "unknown teams action: " + action };
}

async function actGmail(action, params) {
  // Pick account from params.from_account if provided (e.g. "u/0", "u/1", or 0/1)
  let idx = 0;
  if (params && params.from_account != null) {
    const fa = String(params.from_account);
    const m = fa.match(/\d+/);
    if (m) idx = parseInt(m[0], 10);
  }
  const tab = await ensureTab(
    [`https://mail.google.com/mail/u/${idx}/`, "https://mail.google.com/"],
    `https://mail.google.com/mail/u/${idx}/`,
  );
  try {
    const t = await chrome.tabs.get(tab.id);
    if (t.status !== "complete") await sleep(2500); else await sleep(500);
  } catch (_) { await sleep(500); }

  if (action === "gmail_ping") {
    return await CDP.withSession(tab.id, async () => {
      return await CDP.evaluate(tab.id, `(() => ({
        loggedOut: /accounts\\.google\\.com\\/(signin|ServiceLogin)/.test(location.href),
        url: location.href,
      }))()`);
    });
  }

  if (action === "gmail_send") {
    const { to, subject = "", body = "", attachment_b64, attachment_name, attachment_mime } = params || {};
    if (!to) return { ok: false, error: "To: email dena hoga" };

    // Build the compose deeplink — Gmail accepts these query params and
    // pops the compose dialog pre-filled. We navigate to this URL via the
    // tab so the debugger session stays alive.
    const composeParams = new URLSearchParams({
      view: "cm", fs: "1", to: String(to), su: subject, body: body,
    });
    const composeUrl = `https://mail.google.com/mail/u/${idx}/?${composeParams.toString()}`;

    // Navigate to compose URL (background tab, no focus steal)
    await chrome.tabs.update(tab.id, { url: composeUrl, active: false });
    await waitForTabLoaded(tab.id, 25000);
    await sleep(2500);

    const pageScript = `
(async () => {
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));

  function isLoggedOut() {
    return /accounts\\.google\\.com\\/(signin|ServiceLogin)/.test(location.href);
  }

  function b64ToBlob(b64, mime) {
    const bytes = atob(b64);
    const buf = new Uint8Array(bytes.length);
    for (let i = 0; i < bytes.length; i++) buf[i] = bytes.charCodeAt(i);
    return new Blob([buf], { type: mime || 'application/octet-stream' });
  }

  async function clickSend() {
    const sels = [
      'div[role="button"][data-tooltip*="Send" i]',
      'div[role="button"][aria-label*="Send" i]',
      'div[role="button"][data-tooltip*="Ctrl" i]',
    ];
    for (const s of sels) {
      const btn = $(s);
      if (btn) { btn.click(); return true; }
    }
    // Fallback: Ctrl+Enter on the compose dialog
    const dlg = $('div[role="dialog"]');
    if (dlg) {
      dlg.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", ctrlKey: true, bubbles: true }));
      return true;
    }
    return false;
  }

  async function waitForComposeDialogToClose(timeout) {
    const end = Date.now() + (timeout || 10000);
    while (Date.now() < end) {
      const dlg = $('div[role="dialog"]') || $('div.aDh');
      if (!dlg) return true;
      await sleep(400);
    }
    return false;
  }

  async function uploadAttachment(b64, fileName, mime) {
    // Gmail compose has a hidden file input — find ANY input that can take
    // arbitrary files (Filedata is Gmail's stable name)
    let input = null;
    const end = Date.now() + 5000;
    while (Date.now() < end) {
      input = $('input[type="file"][name="Filedata"]') || $('input[type="file"]');
      if (input) break;
      await sleep(200);
    }
    if (!input) throw new Error("Gmail attach input nahi mila.");

    const blob = b64ToBlob(b64, mime || 'application/octet-stream');
    const file = new File([blob], fileName || 'file', { type: mime || 'application/octet-stream' });
    const dt = new DataTransfer();
    dt.items.add(file);
    input.files = dt.files;
    input.dispatchEvent(new Event('change', { bubbles: true }));
    // Wait for Gmail to finish uploading (attachment chip appears)
    await sleep(3500);
  }

  try {
    if (isLoggedOut()) return { ok: false, error: "Gmail logged out — login karo pehle." };

    const attachment_b64 = ${JSON.stringify(attachment_b64 || "")};
    const attachment_name = ${JSON.stringify(attachment_name || "")};
    const attachment_mime = ${JSON.stringify(attachment_mime || "")};

    // Wait for compose dialog to be present (URL params pre-fill it)
    let dlg = null;
    const dlgEnd = Date.now() + 12000;
    while (Date.now() < dlgEnd) {
      dlg = $('div[role="dialog"]');
      if (dlg) break;
      await sleep(300);
    }
    if (!dlg) return { ok: false, error: "Compose dialog nahi mila (deeplink load timeout)" };

    if (attachment_b64) {
      await uploadAttachment(attachment_b64, attachment_name, attachment_mime);
    }

    const sent = await clickSend();
    if (!sent) return { ok: false, error: "Send button nahi mila." };
    const verified = await waitForComposeDialogToClose(8000);
    return { ok: verified, verified, withAttachment: !!attachment_b64 };
  } catch (e) {
    return { ok: false, error: String(e && e.message || e) };
  }
})()
`;
    return await CDP.withSession(tab.id, async () => {
      const r = await CDP.evaluate(tab.id, pageScript, { awaitPromise: true });
      return r || { ok: false, error: "no result" };
    });
  }

  return { ok: false, error: "unknown gmail action: " + action };
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

async function handleCommand(msg) {
  const reqId = msg.req_id || null;
  const action = msg.action || "";
  const params = msg.params || {};

  try {
    let result;
    switch (action) {
      // ----- Generic / diagnostic -----
      case "echo":
        result = { echoed: params, at: Date.now() };
        break;
      case "open_tab": {
        const tab = await chrome.tabs.create({ url: params.url || "about:blank", active: false });
        result = { tab_id: tab.id, url: tab.url };
        break;
      }
      case "list_tabs": {
        const tabs = await chrome.tabs.query({});
        result = tabs.map(t => ({ id: t.id, url: t.url, title: t.title, active: t.active }));
        break;
      }
      case "notify":
        await notify(params.title, params.message);
        result = { shown: true };
        break;

      // ----- WhatsApp -----
      case "whatsapp_send": {
        const r = await actWhatsapp("whatsapp_send", params);
        if (!r.ok) throw new Error(r.error || "wa_send_failed");
        result = r;
        if (params.notify !== false) {
          notify("WhatsApp ✅", `Sent to ${params.recipient || params.phone || ""}`);
        }
        break;
      }
      case "whatsapp_ping": {
        const r = await actWhatsapp("whatsapp_ping", {});
        result = r;
        break;
      }

      // ----- Teams -----
      case "teams_send": {
        const r = await actTeams("teams_send", params);
        if (!r.ok) throw new Error(r.error || "teams_send_failed");
        result = r;
        if (params.notify !== false) {
          notify("Teams ✅", `Sent to ${params.recipient || ""}`);
        }
        break;
      }
      case "teams_ping": {
        const r = await actTeams("teams_ping", {});
        result = r;
        break;
      }

      // ----- Gmail -----
      case "gmail_send": {
        const r = await actGmail("gmail_send", params);
        if (!r.ok) throw new Error(r.error || "gmail_send_failed");
        result = r;
        if (params.notify !== false) {
          notify("Email ✅", `Sent to ${params.to || ""}`);
        }
        break;
      }
      case "gmail_ping": {
        const r = await actGmail("gmail_ping", {});
        result = r;
        break;
      }

      default:
        sendToServer({ type: "result", req_id: reqId, ok: false, error: `unknown action: ${action}` });
        return;
    }
    sendToServer({ type: "result", req_id: reqId, ok: true, result });
  } catch (e) {
    sendToServer({ type: "result", req_id: reqId, ok: false, error: String(e?.message || e) });
    if (params.notify !== false) {
      notify("JARVIS ⚠️", `${action} failed: ${String(e?.message || e).slice(0, 80)}`);
    }
  }
}

// Boot
connect();
setBadge("connecting");
