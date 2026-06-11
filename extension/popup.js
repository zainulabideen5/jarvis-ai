// Popup status — reads the background worker's last-known state from storage.

const connEl = document.getElementById("conn");
const lastEl = document.getElementById("last");
const reloadBtn = document.getElementById("reload");

function paint(status, ts) {
  const dotClass = status === "connected" ? "ok" : status === "connecting" ? "warn" : "err";
  const label = status || "unknown";
  connEl.innerHTML = `<span class="dot ${dotClass}"></span>${label}`;
  if (ts) {
    const d = new Date(ts);
    lastEl.textContent = d.toLocaleTimeString();
  } else {
    lastEl.textContent = "—";
  }
}

async function refresh() {
  try {
    const { jarvis_status, jarvis_status_ts } = await chrome.storage.local.get([
      "jarvis_status", "jarvis_status_ts",
    ]);
    paint(jarvis_status || "disconnected", jarvis_status_ts);
  } catch (_) {
    paint("error", Date.now());
  }
}

reloadBtn.addEventListener("click", async () => {
  // Reload the service worker — fastest way to force a reconnect attempt
  reloadBtn.disabled = true;
  reloadBtn.textContent = "Reconnecting...";
  try { await chrome.runtime.reload(); } catch (_) {}
  setTimeout(() => {
    reloadBtn.disabled = false;
    reloadBtn.textContent = "Reconnect";
    refresh();
  }, 1500);
});

refresh();
setInterval(refresh, 1500);
