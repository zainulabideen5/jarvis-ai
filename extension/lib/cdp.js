// CDP (Chrome DevTools Protocol) helpers for JARVIS Bridge.
//
// We use chrome.debugger to drive WhatsApp/Teams/Gmail tabs at the protocol
// layer. This is the same mechanism Puppeteer/Playwright use internally and
// is the ONLY way on Windows to script Chrome tabs without the window coming
// to the foreground.
//
// Cost: Chrome shows a yellow infobar at the top of the debugged tab
// ("JARVIS Bridge started debugging this browser"). The tab is in the JARVIS
// minimized window so the user never sees the infobar — but if they minimize
// JARVIS and look, it'll be there.

export const CDP = {
  PROTOCOL_VERSION: "1.3",

  // Lock to serialize debugger attach/detach across overlapping commands —
  // attaching twice to the same tab is an error.
  _attachLocks: new Map(),

  async _acquireLock(tabId) {
    const prev = this._attachLocks.get(tabId) || Promise.resolve();
    let release;
    const next = new Promise((r) => { release = r; });
    const ourLink = prev.then(() => next);
    this._attachLocks.set(tabId, ourLink);
    await prev;
    let released = false;
    return () => {
      if (released) return;  // guard against double-release race
      released = true;
      release();
      // Only clear the map entry if it's still OUR link — otherwise we'd
      // wipe out the next waiter's promise and break serialization.
      if (this._attachLocks.get(tabId) === ourLink) {
        this._attachLocks.delete(tabId);
      }
    };
  },

  attach(tabId) {
    return new Promise((resolve, reject) => {
      chrome.debugger.attach({ tabId }, this.PROTOCOL_VERSION, () => {
        const err = chrome.runtime.lastError;
        if (err) {
          // Already attached is OK
          if (/already attached/i.test(err.message || "")) return resolve();
          return reject(new Error(err.message));
        }
        resolve();
      });
    });
  },

  detach(tabId) {
    return new Promise((resolve) => {
      chrome.debugger.detach({ tabId }, () => {
        // Ignore errors on detach — best effort
        resolve();
      });
    });
  },

  send(tabId, method, params = {}) {
    return new Promise((resolve, reject) => {
      chrome.debugger.sendCommand({ tabId }, method, params, (result) => {
        const err = chrome.runtime.lastError;
        if (err) return reject(new Error(err.message));
        resolve(result);
      });
    });
  },

  // Run JS in the tab and return the result. Errors in the page propagate
  // as exceptions here.
  async evaluate(tabId, expression, { awaitPromise = true, returnByValue = true } = {}) {
    const result = await this.send(tabId, "Runtime.evaluate", {
      expression,
      awaitPromise,
      returnByValue,
      userGesture: true,  // unlocks some autoplay/permission-requiring APIs
    });
    if (result.exceptionDetails) {
      const msg = result.exceptionDetails.exception?.description
        || result.exceptionDetails.text
        || "page exception";
      throw new Error(msg);
    }
    return result.result?.value;
  },

  // Convenience: attach, run callback, detach. Handles errors + cleanup.
  async withSession(tabId, fn) {
    const releaseLock = await this._acquireLock(tabId);
    try {
      await this.attach(tabId);
      try {
        return await fn();
      } finally {
        await this.detach(tabId);
      }
    } finally {
      releaseLock();
    }
  },

  // ===== Keyboard input via CDP =====
  // These dispatch REAL keyboard events at the protocol level — they
  // don't require any DOM selector, work on focused element, and survive
  // WhatsApp Web UI changes across versions/languages.

  // Modifier bitmask: Alt=1, Ctrl=2, Meta=4, Shift=8
  MOD_ALT: 1,
  MOD_CTRL: 2,
  MOD_META: 4,
  MOD_SHIFT: 8,

  async key(tabId, key, opts = {}) {
    const modifiers = opts.modifiers || 0;
    const code = opts.code || this._keyCode(key);
    const windowsVirtualKeyCode = opts.virtualKeyCode || this._vkCode(key);
    const text = opts.text;  // For printable chars only

    await this.send(tabId, "Input.dispatchKeyEvent", {
      type: "keyDown",
      key, code, modifiers, windowsVirtualKeyCode,
      ...(text ? { text, unmodifiedText: text } : {}),
    });
    await this.send(tabId, "Input.dispatchKeyEvent", {
      type: "keyUp",
      key, code, modifiers, windowsVirtualKeyCode,
    });
  },

  async typeText(tabId, text) {
    // Input.insertText fires a real input event on the focused element —
    // works on contenteditable + input + textarea without focus quirks.
    await this.send(tabId, "Input.insertText", { text });
  },

  _keyCode(key) {
    // Map common keys to their KeyboardEvent.code values
    const map = {
      "Enter": "Enter", "Tab": "Tab", "Escape": "Escape",
      "ArrowDown": "ArrowDown", "ArrowUp": "ArrowUp",
      "ArrowLeft": "ArrowLeft", "ArrowRight": "ArrowRight",
      "Backspace": "Backspace", "Delete": "Delete",
      "/": "Slash", "?": "Slash",
      " ": "Space",
    };
    return map[key] || ("Key" + key.toUpperCase());
  },

  _vkCode(key) {
    // Windows virtual key codes — needed by CDP for some shortcuts to register
    const map = {
      "Enter": 13, "Tab": 9, "Escape": 27,
      "ArrowDown": 40, "ArrowUp": 38, "ArrowLeft": 37, "ArrowRight": 39,
      "Backspace": 8, "Delete": 46,
      "/": 191, " ": 32,
    };
    return map[key] || 0;
  },
};
