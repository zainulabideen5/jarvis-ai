const BASE = '/api';

// Surface the server's error body to the user when a call fails — previously
// we threw `"API error: 422"` with no detail, hiding "missing field X" / "rate
// limited" / "consent not granted" messages the server actually sends.
async function readError(res) {
  let detail = '';
  try {
    const text = await res.text();
    if (text) {
      try {
        const j = JSON.parse(text);
        detail = j.detail || j.error || j.message || text;
      } catch {
        detail = text;
      }
    }
  } catch {
    // ignore — fall back to status
  }
  if (typeof detail === 'string' && detail.length > 300) {
    detail = detail.slice(0, 300) + '…';
  }
  return new Error(`API ${res.status}${detail ? ': ' + detail : ''}`);
}

async function fetchJSON(url) {
  const res = await fetch(`${BASE}${url}`);
  if (!res.ok) throw await readError(res);
  return res.json();
}

async function patchJSON(url, body) {
  const opts = { method: 'PATCH' };
  if (body) {
    opts.headers = { 'Content-Type': 'application/json' };
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(`${BASE}${url}`, opts);
  if (!res.ok) throw await readError(res);
  return res.json();
}

async function postJSON(url, body) {
  const res = await fetch(`${BASE}${url}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw await readError(res);
  return res.json();
}

async function putJSON(url, body) {
  const res = await fetch(`${BASE}${url}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw await readError(res);
  return res.json();
}

async function deleteJSON(url) {
  const res = await fetch(`${BASE}${url}`, { method: 'DELETE' });
  if (!res.ok) throw await readError(res);
  return res.json();
}

export const api = {
  getStats: () => fetchJSON('/stats'),
  getChunks: (limit = 50, offset = 0) => fetchJSON(`/chunks?limit=${limit}&offset=${offset}`),
  getTranscriptions: (limit = 50, offset = 0) => fetchJSON(`/transcriptions?limit=${limit}&offset=${offset}`),
  getTasks: (status, limit = 50, offset = 0) => {
    const params = new URLSearchParams({ limit, offset });
    if (status) params.set('status', status);
    return fetchJSON(`/tasks?${params}`);
  },
  approveTask: (id) => patchJSON(`/tasks/${id}/approve`),
  rejectTask: (id) => patchJSON(`/tasks/${id}/reject`),
  createTask: (task) => postJSON('/tasks', task),

  // Rules
  getRules: () => fetchJSON('/rules'),
  createRule: (rule) => postJSON('/rules', rule),
  updateRule: (id, rule) => putJSON(`/rules/${id}`, rule),
  deleteRule: (id) => deleteJSON(`/rules/${id}`),
  toggleRule: (id) => patchJSON(`/rules/${id}/toggle`),

  // Listening control
  getListening: () => fetchJSON('/listening'),
  startListening: () => postJSON('/listening/on', {}),
  stopListening: () => postJSON('/listening/off', {}),

  // Meeting
  startMeeting: (title) => postJSON('/meeting/start', { title }),
  endMeeting: () => postJSON('/meeting/end', {}),
  getActiveMeeting: () => fetchJSON('/meeting/active'),
  getMeetings: () => fetchJSON('/meetings'),
  generateReport: () => postJSON('/report/generate', {}),

  // Chat
  sendChat: (message, confirmToken = null, attachments = null) =>
    postJSON('/chat', { message, confirm_token: confirmToken, attachments }),
  getChatHistory: () => fetchJSON('/chat/history'),
  // Upload a file the user attached in the dashboard chat. Returns
  // `{ path, filename, size }`. The `path` is server-side; pass it back via
  // sendChat(..., attachments: [path]) so the engine/email can pick it up.
  uploadChatFile: async (file) => {
    const fd = new FormData();
    fd.append('file', file);
    const res = await fetch(`${BASE}/chat/upload`, { method: 'POST', body: fd });
    if (!res.ok) throw await readError(res);
    return res.json();
  },

  // Laptop control
  getLaptopActivity: () => fetchJSON('/laptop/activity'),
  laptopConfirm: (token, decision = 'yes') =>
    postJSON('/laptop/confirm', { token, decision }),

  // Verification flow
  getVerificationStatus: () => fetchJSON('/verification/status'),
  verificationAction: (action) => postJSON('/verification/action', { action }),

  // Consent
  getConsent: () => fetchJSON('/consent'),
  grantConsent: () => postJSON('/consent/grant', { user_agent: navigator.userAgent }),
  revokeConsent: () => postJSON('/consent/revoke', {}),

  // Universal engine (UIA + keyboard + PowerShell)
  engineStatus: () => fetchJSON('/engine/status'),
  engineRun: (task) => postJSON('/engine/run', { task }),
  engineProgress: () => fetchJSON('/engine/progress'),

  // Office COM check
  officeCheck: () => fetchJSON('/office/check'),

  // Native screen + windows
  nativeScreenInfo: () => fetchJSON('/native/screen-info'),
  nativeListWindows: () => fetchJSON('/native/windows'),

  // Image-match templates
  imgmatchTemplates: () => fetchJSON('/imgmatch/templates'),
  imgmatchDelete: (key) => postJSON('/imgmatch/delete', { key }),

  // Calendar
  getEvents: () => fetchJSON('/calendar'),
  confirmEvent: (id) => patchJSON(`/calendar/${id}/confirm`),
  deleteEvent: (id) => deleteJSON(`/calendar/${id}`),

  // Clients
  getClients: () => fetchJSON('/clients'),
  getClient: (id) => fetchJSON(`/clients/${id}`),
  createClient: (client) => postJSON('/clients', client),
  updateClient: (id, client) => putJSON(`/clients/${id}`, client),
  deleteClient: (id) => deleteJSON(`/clients/${id}`),
  assignSpeaker: (id, label) => patchJSON(`/clients/${id}/assign-speaker`, { speaker_label: label }),

  // Agents
  getAgents: () => fetchJSON('/agents'),
  stopAgent: (agentId) => postJSON(`/agents/${agentId}/stop`, {}),
};
