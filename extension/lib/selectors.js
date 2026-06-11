// JARVIS Bridge — selector resolution library.
//
// Centralized selector source = server's selectors.json
// Content scripts import this module to get robust element finding with
// automatic fallbacks. When Meta/Google/Microsoft pushes a UI update that
// breaks a selector, we edit server JSON + bump version — all 50 users
// auto-update within minutes via background script's poller.
//
// Public API:
//   getSelectorsFor(service)            → { logged_out_marker: [...], search_box: [...], ... }
//   findFirst(service, role, root=doc)  → Element or null (synchronous; tries each selector in order)
//   waitForFirst(service, role, opts)   → Promise<Element|null> (polls until found or timeout)
//   isLoggedOut(service)                 → boolean
//
// Internal:
//   cache stored in chrome.storage.local under key "jarvis_selectors_v1"
//   background script handles fetch + version polling (see background.js)
//   content scripts can request a refresh via chrome.runtime.sendMessage({type:"selectors_refresh"})

(function () {
  if (window.__jarvisSelectorsLoaded) return;
  window.__jarvisSelectorsLoaded = true;

  const CACHE_KEY = "jarvis_selectors_v1";
  let _cache = null;
  let _cacheLoadPromise = null;

  // Bootstrap default config — used if cache miss + server unreachable.
  // Keeps the extension functional offline; server config overrides on next fetch.
  const FALLBACK_CONFIG = {
    _meta: { version: 0 },
    whatsapp: {
      logged_out_marker: ["canvas[aria-label*='QR' i]"],
      search_box: [
        "div[contenteditable='true'][data-tab='3']",
        "div[role='textbox'][contenteditable='true'][title*='Search' i]",
      ],
      compose_box: [
        "div[contenteditable='true'][data-tab='10']",
        "div[contenteditable='true'][data-tab='6']",
        "div[role='textbox'][contenteditable='true'][title*='Type a message' i]",
        "footer div[contenteditable='true']",
      ],
      send_button: ["button[data-tab='11']", "button[aria-label='Send' i]"],
      attach_button: ["div[title='Attach' i]", "span[data-icon='clip']"],
      attach_photo_video: ["input[type='file'][accept*='image']", "input[type='file'][accept*='video']"],
      attach_document: ["input[type='file'][accept='*']"],
      send_attachment_button: ["div[role='button'][aria-label='Send' i]", "span[data-icon='send']"],
    },
    gmail: {
      logged_out_marker: ["form[action*='accounts.google.com/signin']"],
      compose_button: ["div[gh='cm']", "[role='button'][gh='cm']"],
      compose_to: ["input[name='to']", "textarea[name='to']"],
      compose_subject: ["input[name='subjectbox']"],
      compose_body: ["div[role='textbox'][aria-label*='Message' i]"],
      send_button: ["div[role='button'][data-tooltip*='Send' i]"],
      compose_dialog: ["div[role='dialog']"],
    },
    teams: {
      logged_out_marker: ["input[name='loginfmt']"],
      new_chat_button: ["button[data-tid='chat-list-new-chat-button']"],
      people_picker: ["input[aria-label*='To' i]", "input[role='combobox']"],
      suggestion_option: ["li[role='option']", "div[role='option']"],
      compose_box: ["div[role='textbox'][contenteditable='true'][aria-label*='message' i]"],
      send_button: ["button[data-tid='newMessageCommandBar-sendBtn']", "button[aria-label*='Send' i]"],
    },
    trello: {
      logged_out_marker: ["a[href*='/login']"],
      add_card_button: ["a[data-testid='list-add-card-button']"],
      card_input: ["textarea[data-testid='list-card-composer-textarea']"],
      card_save_button: ["button[data-testid='list-card-composer-add-card-button']"],
    },
  };

  // Read cached selectors from chrome.storage.local. Falls back to bundled
  // defaults on first run or if storage is empty.
  async function loadCache() {
    if (_cache) return _cache;
    if (_cacheLoadPromise) return _cacheLoadPromise;
    _cacheLoadPromise = (async () => {
      try {
        const stored = await new Promise((resolve) => {
          chrome.storage.local.get([CACHE_KEY], (items) => {
            resolve(items && items[CACHE_KEY] ? items[CACHE_KEY] : null);
          });
        });
        if (stored && stored._meta && Number.isInteger(stored._meta.version)) {
          _cache = stored;
        } else {
          _cache = FALLBACK_CONFIG;
        }
      } catch (_) {
        _cache = FALLBACK_CONFIG;
      }
      return _cache;
    })();
    return _cacheLoadPromise;
  }

  // Get the selector list for a given (service, role). Returns [] if missing.
  async function getSelectorsFor(service, role) {
    const cache = await loadCache();
    const svc = cache && cache[service];
    if (!svc) return [];
    const list = svc[role];
    if (!list) return [];
    return Array.isArray(list) ? list : [list];
  }

  // Synchronous helper: try each selector against `root` and return the first
  // element found. Returns null if nothing matches. Throws nothing — safe to
  // call inside polling loops.
  function findFirstSync(selectors, root) {
    const r = root || document;
    for (const sel of selectors) {
      try {
        const el = r.querySelector(sel);
        if (el) return el;
      } catch (_) {
        // Ignore invalid selectors (e.g. :has-text() not supported in some browsers)
      }
    }
    return null;
  }

  // Async helper: returns the first matching element, trying selectors in
  // order. Resolves immediately on first hit.
  async function findFirst(service, role, root) {
    const selectors = await getSelectorsFor(service, role);
    return findFirstSync(selectors, root);
  }

  // Poll until found or timeout. Default timeout 25s, poll interval 250ms.
  async function waitForFirst(service, role, opts) {
    const timeout = (opts && opts.timeout) || 25000;
    const interval = (opts && opts.interval) || 250;
    const root = (opts && opts.root) || document;
    const selectors = await getSelectorsFor(service, role);
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {
      const el = findFirstSync(selectors, root);
      if (el) return el;
      await new Promise((r) => setTimeout(r, interval));
    }
    return null;
  }

  // Find ALL matching elements (across all selectors, deduplicated).
  async function findAll(service, role, root) {
    const selectors = await getSelectorsFor(service, role);
    const r = root || document;
    const seen = new Set();
    const out = [];
    for (const sel of selectors) {
      let nodes = [];
      try {
        nodes = Array.from(r.querySelectorAll(sel));
      } catch (_) {
        continue;
      }
      for (const n of nodes) {
        if (!seen.has(n)) {
          seen.add(n);
          out.push(n);
        }
      }
    }
    return out;
  }

  // Is the user logged out on the current page? Uses the service's
  // logged_out_marker list — any match = logged out.
  async function isLoggedOut(service) {
    const el = await findFirst(service, "logged_out_marker");
    return el !== null;
  }

  // Called by background script after a successful fetch from the server.
  // Replaces in-memory + persistent cache atomically.
  async function applyServerUpdate(newConfig) {
    if (!newConfig || !newConfig._meta) return false;
    _cache = newConfig;
    await new Promise((resolve) => {
      chrome.storage.local.set({ [CACHE_KEY]: newConfig }, () => resolve());
    });
    return true;
  }

  // Get the current cached version number (for debug / version display).
  async function getCachedVersion() {
    const cache = await loadCache();
    return (cache && cache._meta && cache._meta.version) || 0;
  }

  // Listen for selector updates pushed from the background script. When the
  // background script's poller finds a new version on the server, it sends
  // each open service tab a `selectors_updated` message so the in-memory
  // cache refreshes immediately (no page reload required).
  try {
    chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
      if (msg && msg.type === "selectors_updated" && msg.config) {
        applyServerUpdate(msg.config).then(() => {
          sendResponse({ ok: true, version: msg.config._meta && msg.config._meta.version });
        }).catch((e) => {
          sendResponse({ ok: false, error: String(e && e.message || e) });
        });
        return true;  // async response
      }
    });
  } catch (_) {
    // chrome.runtime not available — unlikely in content script context
  }

  // Expose to window so content scripts can access without bundling.
  window.JarvisSelectors = {
    findFirst,
    findFirstSync,
    findAll,
    waitForFirst,
    getSelectorsFor,
    isLoggedOut,
    applyServerUpdate,
    getCachedVersion,
  };
})();
