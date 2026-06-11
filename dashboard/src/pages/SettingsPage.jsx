import { useCallback, useEffect, useState } from 'react';
import { api } from '../api';
import { usePolling } from '../hooks/usePolling';
import { useToast } from '../components/Toast';

export default function SettingsPage() {
  const fetchHealth = useCallback(() => api.getStats(), []);
  const { data, loading } = usePolling(fetchHealth, 10000);

  return (
    <div>
      <h2 className="text-2xl font-bold mb-6">Settings & Status</h2>

      {/* Consent / Permissions */}
      <ConsentSection />

      {/* Unified workspace — ONE Chromium with WhatsApp + Teams together */}
      <WorkspaceConnectSection />

      {/* Server connection */}
      <Section title="Server">
        <StatusRow
          label="API Server"
          value={loading ? 'Checking...' : 'Connected'}
          ok={!loading}
        />
        <InfoRow label="API URL" value={window.location.origin + '/api'} />
        <InfoRow label="WebSocket" value={window.location.origin.replace('http', 'ws') + '/ws/agent'} />
      </Section>

      {/* Pipeline stats */}
      {data && (
        <Section title="Pipeline Stats">
          <InfoRow label="Total Chunks" value={data.total_chunks} />
          <InfoRow label="Transcriptions" value={data.total_transcriptions} />
          <InfoRow label="Tasks Extracted" value={data.total_tasks} />
          <InfoRow label="Pending Approval" value={data.pending_tasks} />
        </Section>
      )}

      {/* Desktop Agent setup */}
      <Section title="Desktop Agent Setup">
        <p className="text-xs text-gray-500 mb-3">
          Run these commands on the Windows machine where the agent will capture audio:
        </p>
        <CodeBlock lines={[
          'cd desktop-agent',
          'python -m venv venv',
          'venv\\Scripts\\activate',
          'pip install -r requirements.txt',
          'playwright install chromium   # only if using WhatsApp driver',
          'copy .env.example .env        # edit with your API keys',
          'python -m jarvis_agent',
        ]} />
      </Section>

      {/* Server setup */}
      <Section title="Server Setup">
        <CodeBlock lines={[
          'cd server',
          'python -m venv venv',
          'venv\\Scripts\\activate   # or source venv/bin/activate on Linux',
          'pip install -r requirements.txt',
          'copy .env.example .env   # add your GROQ_API_KEY',
          'uvicorn app.main:app --host 0.0.0.0 --port 8000',
        ]} />
      </Section>

      {/* Dashboard setup */}
      <Section title="Dashboard Setup">
        <CodeBlock lines={[
          'cd dashboard',
          'npm install',
          'npm run dev             # dev mode at http://localhost:3000',
          'npm run build           # production build to dist/',
        ]} />
      </Section>

      {/* Required API keys */}
      <Section title="Required API Keys">
        <KeyRow name="JARVIS_GROQ_API_KEY" purpose="Whisper + Vision LLM (task extraction, vision driver)" source="console.groq.com" />
        <KeyRow name="JARVIS_GMAIL_ADDRESS" purpose="Gmail driver (sending emails)" source="Your Gmail address" />
        <KeyRow name="JARVIS_GMAIL_APP_PASSWORD" purpose="Gmail SMTP auth" source="Google Account > Security > App passwords" />
        <KeyRow name="JARVIS_TELEGRAM_BOT_TOKEN" purpose="Telegram driver" source="@BotFather on Telegram" />
        <KeyRow name="JARVIS_TELEGRAM_CHAT_ID" purpose="Default Telegram chat" source="@userinfobot on Telegram" />
      </Section>
    </div>
  );
}

function Section({ title, children }) {
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-5 mb-4">
      <h3 className="text-sm font-semibold text-gray-300 mb-3">{title}</h3>
      {children}
    </div>
  );
}

function StatusRow({ label, value, ok }) {
  return (
    <div className="flex items-center justify-between py-1.5 text-sm">
      <span className="text-gray-400">{label}</span>
      <span className="flex items-center gap-2">
        <span className={`w-2 h-2 rounded-full ${ok ? 'bg-green-500' : 'bg-red-500'}`}></span>
        <span className={ok ? 'text-green-400' : 'text-red-400'}>{value}</span>
      </span>
    </div>
  );
}

function InfoRow({ label, value }) {
  return (
    <div className="flex items-center justify-between py-1.5 text-sm">
      <span className="text-gray-400">{label}</span>
      <span className="text-gray-300 font-mono text-xs">{value}</span>
    </div>
  );
}

function CodeBlock({ lines }) {
  return (
    <pre className="bg-gray-950 border border-gray-800 rounded-lg p-3 text-xs text-gray-400 overflow-x-auto">
      {lines.map((line, i) => (
        <div key={i}>{line}</div>
      ))}
    </pre>
  );
}

function KeyRow({ name, purpose, source }) {
  return (
    <div className="py-2 border-b border-gray-800/50 last:border-0">
      <div className="flex items-center justify-between">
        <code className="text-xs text-blue-400">{name}</code>
        <span className="text-xs text-gray-600">{source}</span>
      </div>
      <p className="text-xs text-gray-500 mt-0.5">{purpose}</p>
    </div>
  );
}

function WhatsAppConnectSection() {
  const [status, setStatus] = useState({ session_exists: false });
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState('');
  const toast = useToast();

  const refresh = useCallback(() => {
    api.getWhatsAppStatus().then(setStatus).catch(() => {});
  }, []);

  useEffect(() => {
    refresh();
    const tick = setInterval(refresh, 8000);
    return () => clearInterval(tick);
  }, [refresh]);

  const handleConnect = async () => {
    if (busy) return;
    setBusy(true);
    setProgress('Chromium khul rahi — 5-10 sec wait kar...');
    try {
      // Tell user what to do — Chromium opens server-side
      setTimeout(() => setProgress('Chromium window dikhi? Phone se QR scan kar (3 min time hai)'), 6000);
      const result = await api.setupWhatsApp();
      if (result.ok) {
        toast.success(result.message || 'WhatsApp connected — ab background mein sends chalenge', 5000);
        setProgress('');
        refresh();
      } else {
        toast.error(result.message || 'Setup fail hua');
        setProgress('');
      }
    } catch (err) {
      toast.error(`Setup fail: ${err.message}`);
      setProgress('');
    } finally {
      setBusy(false);
    }
  };

  const connected = !!status.session_exists;

  return (
    <div
      className="rounded-xl p-5 mb-4"
      style={{
        background: 'rgba(15, 22, 38, 0.7)',
        border: `1px solid ${connected ? 'rgba(34, 197, 94, 0.4)' : 'rgba(251, 191, 36, 0.4)'}`,
        boxShadow: connected ? '0 0 18px rgba(34, 197, 94, 0.15)' : '0 0 18px rgba(251, 191, 36, 0.15)',
      }}
    >
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div
            className="w-9 h-9 rounded flex items-center justify-center font-display font-bold text-sm"
            style={{
              background: connected ? 'linear-gradient(135deg, #22c55e, #15803d)' : 'rgba(251, 191, 36, 0.2)',
              border: `1.5px solid ${connected ? '#86efac' : '#fbbf24'}`,
              color: connected ? '#001a08' : '#fbbf24',
              boxShadow: connected ? '0 0 12px rgba(34, 197, 94, 0.5)' : 'none',
            }}
          >
            {connected ? '✓' : '!'}
          </div>
          <div>
            <h3 className="font-display text-sm uppercase tracking-widest"
                style={{ color: connected ? '#86efac' : '#fde68a' }}>
              WhatsApp Connection
            </h3>
            <p className="text-xs mt-0.5" style={{ color: 'var(--text-muted)' }}>
              {connected
                ? (status.background_alive
                    ? 'Connected · Background mein silent chal raha (window nahi dikhega)'
                    : 'Connected — pehla send par background invisible chalu hoga')
                : 'Not connected — ek baar QR scan kar to forever ke liye link ho jaye'}
            </p>
            {progress && (
              <p className="text-[11px] mt-1.5 font-mono" style={{ color: '#22d3ee' }}>
                {progress}
              </p>
            )}
            <p className="text-[10px] mt-1" style={{ color: 'var(--text-dim)' }}>
              {connected
                ? `Session: ${status.session_dir || '~/data/wa_session'}`
                : 'Click karoge → Chromium khulegi → phone se QR scan → cookies save → done'}
            </p>
          </div>
        </div>

        <button
          onClick={handleConnect}
          disabled={busy}
          className="px-5 py-2 rounded font-display uppercase tracking-widest text-xs font-bold transition-all"
          style={
            connected
              ? {
                  background: 'rgba(34, 197, 94, 0.15)',
                  border: '1px solid rgba(34, 197, 94, 0.5)',
                  color: '#86efac',
                }
              : {
                  background: 'linear-gradient(135deg, #fbbf24, #d97706)',
                  border: '1px solid #fcd34d',
                  color: '#3d1e00',
                  boxShadow: '0 0 18px rgba(251, 191, 36, 0.4)',
                }
          }
        >
          {busy ? 'Connecting...' : connected ? '✓ Connected (reconnect)' : '⚡ Connect WhatsApp'}
        </button>
      </div>
    </div>
  );
}

function WorkspaceConnectSection() {
  const [status, setStatus] = useState({
    browser_alive: false,
    whatsapp: { session_exists: false, page_alive: false },
    teams: { session_exists: false, page_alive: false },
    gmail: { session_exists: false, page_alive: false },
    trello: { session_exists: false, page_alive: false },
  });
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState('');
  const toast = useToast();

  const refresh = useCallback(() => {
    api.getWorkspaceStatus().then(setStatus).catch(() => {});
  }, []);

  useEffect(() => {
    refresh();
    const tick = setInterval(refresh, 8000);
    return () => clearInterval(tick);
  }, [refresh]);

  const handleConnect = async () => {
    if (busy) return;
    setBusy(true);
    setProgress('Chromium khul rahi — WhatsApp + Teams dono tabs...');
    try {
      setTimeout(
        () => setProgress('Phone se WhatsApp QR scan + Microsoft account sign-in karo (3 min)'),
        6000
      );
      const result = await api.openWorkspace();
      if (result.ok) {
        toast.success(result.message || 'Workspace ready — background mein silent chalega', 6000);
        setProgress('');
        refresh();
      } else {
        toast.warn(result.message || 'Workspace open partial');
        setProgress('');
      }
    } catch (err) {
      toast.error(`Workspace open fail: ${err.message}`);
      setProgress('');
    } finally {
      setBusy(false);
    }
  };

  // ready = user has actually logged in (cookies present in session DB).
  // page_alive only means a tab exists — could be sitting on a login screen.
  const waOk = !!status.whatsapp?.session_exists;
  const teamsOk = !!status.teams?.session_exists;
  const gmailOk = !!status.gmail?.session_exists;
  const trelloOk = !!status.trello?.session_exists;
  const browserOk = !!status.browser_alive;
  const allOk = waOk && teamsOk && gmailOk && trelloOk;
  const anyOk = waOk || teamsOk || gmailOk || trelloOk;

  const Badge = ({ label, ok }) => (
    <span
      className="px-2 py-0.5 rounded text-[10px] font-display uppercase tracking-widest font-bold"
      style={{
        background: ok ? 'rgba(34, 197, 94, 0.15)' : 'rgba(251, 191, 36, 0.15)',
        border: `1px solid ${ok ? 'rgba(34, 197, 94, 0.5)' : 'rgba(251, 191, 36, 0.5)'}`,
        color: ok ? '#86efac' : '#fcd34d',
      }}
    >
      {label} {ok ? '✓' : '○'}
    </span>
  );

  return (
    <div
      className="rounded-xl p-5 mb-4"
      style={{
        background: 'rgba(15, 22, 38, 0.7)',
        border: `1px solid ${allOk ? 'rgba(34, 197, 94, 0.5)' : 'rgba(251, 191, 36, 0.4)'}`,
        boxShadow: allOk ? '0 0 20px rgba(34, 197, 94, 0.18)' : '0 0 20px rgba(251, 191, 36, 0.18)',
      }}
    >
      <div className="flex items-center justify-between gap-4">
        <div className="flex items-center gap-3 flex-1 min-w-0">
          <div
            className="w-9 h-9 rounded flex items-center justify-center font-display font-bold text-sm flex-shrink-0"
            style={{
              background: allOk ? 'linear-gradient(135deg, #22c55e, #15803d)' : 'rgba(251, 191, 36, 0.2)',
              border: `1.5px solid ${allOk ? '#86efac' : '#fbbf24'}`,
              color: allOk ? '#001a08' : '#fbbf24',
              boxShadow: allOk ? '0 0 12px rgba(34, 197, 94, 0.5)' : 'none',
            }}
          >
            {allOk ? '✓' : '!'}
          </div>
          <div className="flex-1 min-w-0">
            <h3 className="font-display text-sm uppercase tracking-widest"
                style={{ color: allOk ? '#86efac' : '#fde68a' }}>
              JARVIS Browser (Workspace)
            </h3>
            <p className="text-xs mt-0.5" style={{ color: 'var(--text-muted)' }}>
              {anyOk
                ? 'Ek hi Chromium mein WhatsApp + Teams — silent background sends'
                : 'Ek baar setup — Chromium open hogi, QR scan + MS sign-in dono'}
            </p>
            {progress && (
              <p className="text-[11px] mt-1.5 font-mono" style={{ color: '#22d3ee' }}>
                {progress}
              </p>
            )}
            <div className="flex gap-2 mt-2 flex-wrap">
              <Badge label="WhatsApp" ok={waOk} />
              <Badge label="Teams" ok={teamsOk} />
              <Badge label="Gmail" ok={gmailOk} />
              <Badge label="Trello" ok={trelloOk} />
              {browserOk && (
                <span
                  className="px-2 py-0.5 rounded text-[10px] font-display uppercase tracking-widest font-bold"
                  style={{
                    background: 'rgba(99, 102, 241, 0.15)',
                    border: '1px solid rgba(99, 102, 241, 0.5)',
                    color: '#a5b4fc',
                  }}
                >
                  Browser LIVE
                </span>
              )}
            </div>
          </div>
        </div>

        <button
          onClick={handleConnect}
          disabled={busy}
          className="px-5 py-2 rounded font-display uppercase tracking-widest text-xs font-bold transition-all flex-shrink-0"
          style={
            allOk
              ? {
                  background: 'rgba(34, 197, 94, 0.15)',
                  border: '1px solid rgba(34, 197, 94, 0.5)',
                  color: '#86efac',
                }
              : {
                  background: 'linear-gradient(135deg, #22d3ee, #0891b2)',
                  border: '1px solid #67e8f9',
                  color: '#001018',
                  boxShadow: '0 0 18px rgba(34, 211, 238, 0.5)',
                }
          }
        >
          {busy ? 'Opening...' : allOk ? '✓ Reconnect' : '⚡ Open Workspace'}
        </button>
      </div>
    </div>
  );
}

function GmailAccountsSection() {
  const [accounts, setAccounts] = useState([]);
  const [busy, setBusy] = useState(false);
  const [editingIdx, setEditingIdx] = useState(null);
  const [draftLabel, setDraftLabel] = useState('');
  const toast = useToast();

  const refresh = useCallback(() => {
    api.gmailListAccounts().then((r) => setAccounts(r.accounts || [])).catch(() => {});
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  const handleDetect = async () => {
    if (busy) return;
    setBusy(true);
    toast.success('Scanning Gmail accounts in JARVIS Chromium... (yeh 30-60 sec lega)', 6000);
    try {
      const r = await api.gmailDetectAccounts();
      if (r.ok) {
        toast.success(`Detected ${r.count} Gmail accounts. Ab labels assign kar.`, 5000);
        setAccounts(r.accounts || []);
      } else { toast.error(r.error || 'Detect fail'); }
    } catch (e) { toast.error(`Fail: ${e.message}`); }
    finally { setBusy(false); }
  };

  const saveLabel = async (idx) => {
    if (!draftLabel.trim()) {
      setEditingIdx(null);
      return;
    }
    setBusy(true);
    try {
      const account = accounts.find(a => a.index === idx);
      await api.gmailSetLabel(idx, draftLabel.trim(), account?.email || '');
      toast.success(`u/${idx} → "${draftLabel.trim()}"`, 3000);
      refresh();
    } catch (e) { toast.error(`Fail: ${e.message}`); }
    finally {
      setBusy(false);
      setEditingIdx(null);
      setDraftLabel('');
    }
  };

  const removeLabel = async (idx) => {
    setBusy(true);
    try {
      await api.gmailRemoveLabel(idx);
      toast.warn(`u/${idx} label removed`, 3000);
      refresh();
    } catch (e) { toast.error(`Fail: ${e.message}`); }
    finally { setBusy(false); }
  };

  const openAccount = async (label) => {
    try {
      await api.gmailOpenAccount(label);
      toast.success(`"${label}" Gmail tab khol di`, 3000);
    } catch (e) { toast.error(`Fail: ${e.message}`); }
  };

  return (
    <div className="rounded-xl p-5 mb-4"
      style={{
        background: 'rgba(15, 22, 38, 0.7)',
        border: '1px solid rgba(239, 68, 68, 0.4)',
        boxShadow: '0 0 18px rgba(239, 68, 68, 0.12)',
      }}>
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-3">
          <div className="w-9 h-9 rounded flex items-center justify-center font-display font-bold text-sm"
            style={{
              background: 'linear-gradient(135deg, #ef4444, #b91c1c)',
              border: '1.5px solid #fca5a5',
              color: '#fef2f2',
            }}>
            M
          </div>
          <div>
            <h3 className="font-display text-sm uppercase tracking-widest" style={{ color: '#fca5a5' }}>
              Multi-Gmail Accounts (up to 14+)
            </h3>
            <p className="text-xs mt-0.5" style={{ color: 'var(--text-muted)' }}>
              Chrome mein logged-in Gmail accounts ko labels assign kar — phir chat se "Personal Gmail se bhej" kaam karega
            </p>
          </div>
        </div>
        <button onClick={handleDetect} disabled={busy}
          className="px-3 py-2 rounded font-display uppercase tracking-widest text-[11px] font-bold"
          style={{
            background: 'linear-gradient(135deg, #ef4444, #b91c1c)',
            border: '1px solid #fca5a5',
            color: '#fef2f2',
            boxShadow: '0 0 14px rgba(239, 68, 68, 0.4)',
          }}>
          {busy ? 'Scanning...' : '🔍 Detect Accounts'}
        </button>
      </div>

      {accounts.length === 0 ? (
        <p className="text-[11px]" style={{ color: 'var(--text-dim)' }}>
          Koi account detected nahi. JARVIS Chromium mein mail.google.com pe accounts login karo, phir "Detect Accounts" dabaa.
        </p>
      ) : (
        <div className="space-y-1.5">
          {accounts.map((a) => (
            <div key={a.index} className="flex items-center gap-2 py-1.5 px-2 rounded text-xs"
              style={{ background: 'rgba(0,0,0,0.3)', border: '1px solid rgba(239, 68, 68, 0.2)' }}>
              <span className="font-mono" style={{ color: '#fca5a5', minWidth: '36px' }}>u/{a.index}</span>
              <span className="font-mono text-[10px] flex-1 truncate" style={{ color: 'var(--text-muted)' }}>
                {a.email || '(email not detected)'}
              </span>
              {editingIdx === a.index ? (
                <>
                  <input
                    type="text"
                    value={draftLabel}
                    onChange={(e) => setDraftLabel(e.target.value)}
                    placeholder="Label (e.g. Personal)"
                    className="px-2 py-1 rounded text-xs font-mono"
                    style={{
                      background: 'rgba(0,0,0,0.5)',
                      border: '1px solid rgba(239,68,68,0.5)',
                      color: '#e2e8f0',
                      width: '120px',
                    }}
                    onKeyDown={(e) => { if (e.key === 'Enter') saveLabel(a.index); }}
                    autoFocus
                  />
                  <button onClick={() => saveLabel(a.index)} disabled={busy}
                    className="px-2 py-1 rounded text-[10px] font-bold"
                    style={{ background: 'rgba(34, 197, 94, 0.2)', border: '1px solid #86efac', color: '#86efac' }}>
                    Save
                  </button>
                </>
              ) : (
                <>
                  {a.label ? (
                    <span className="px-2 py-0.5 rounded text-[10px] font-bold"
                      style={{ background: 'rgba(34, 197, 94, 0.15)', border: '1px solid #86efac', color: '#86efac' }}>
                      {a.label}
                    </span>
                  ) : (
                    <span className="text-[10px]" style={{ color: 'var(--text-dim)' }}>(no label)</span>
                  )}
                  <button onClick={() => { setEditingIdx(a.index); setDraftLabel(a.label || ''); }}
                    className="px-2 py-1 rounded text-[10px]"
                    style={{ background: 'rgba(239,68,68,0.15)', border: '1px solid rgba(239,68,68,0.4)', color: '#fca5a5' }}>
                    ✏️
                  </button>
                  {a.label && (
                    <button onClick={() => openAccount(a.label)} disabled={busy}
                      className="px-2 py-1 rounded text-[10px]"
                      style={{ background: 'rgba(34, 211, 238, 0.15)', border: '1px solid rgba(34, 211, 238, 0.4)', color: '#67e8f9' }}>
                      ↗ Open
                    </button>
                  )}
                  <button onClick={() => removeLabel(a.index)} disabled={busy}
                    className="px-2 py-1 rounded text-[10px]"
                    style={{ background: 'rgba(239,68,68,0.15)', border: '1px solid rgba(239,68,68,0.4)', color: '#fca5a5' }}>
                    🗑
                  </button>
                </>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function NativeAppsSection() {
  const [office, setOffice] = useState(null);
  const [screen, setScreen] = useState(null);
  const [windows, setWindows] = useState(null);
  const [busy, setBusy] = useState(false);
  const toast = useToast();

  const checkAll = useCallback(async () => {
    setBusy(true);
    try {
      const [o, s, w] = await Promise.all([
        api.officeCheck().catch(() => null),
        api.nativeScreenInfo().catch(() => null),
        api.nativeListWindows().catch(() => null),
      ]);
      setOffice(o);
      setScreen(s);
      setWindows(w);
    } finally { setBusy(false); }
  }, []);

  useEffect(() => { checkAll(); }, [checkAll]);

  const officeApps = office?.office || {};
  const screenInfo = screen?.screen || {};

  return (
    <div className="rounded-xl p-5 mb-4"
      style={{
        background: 'rgba(15, 22, 38, 0.7)',
        border: '1px solid rgba(34, 211, 238, 0.3)',
      }}>
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-3">
          <div className="w-9 h-9 rounded flex items-center justify-center font-display font-bold text-sm"
            style={{
              background: 'linear-gradient(135deg, #22d3ee, #0891b2)',
              border: '1.5px solid #67e8f9',
              color: '#001018',
            }}>
              💻
          </div>
          <div>
            <h3 className="font-display text-sm uppercase tracking-widest" style={{ color: '#67e8f9' }}>
              Native Laptop Status
            </h3>
            <p className="text-xs mt-0.5" style={{ color: 'var(--text-muted)' }}>
              Office (silent COM), screen, open windows
            </p>
          </div>
        </div>
        <button onClick={checkAll} disabled={busy}
          className="px-3 py-2 rounded font-display uppercase tracking-widest text-[11px] font-bold"
          style={{
            background: 'rgba(34, 211, 238, 0.15)',
            border: '1px solid rgba(34, 211, 238, 0.5)',
            color: '#67e8f9',
          }}>
          {busy ? '...' : '↻ Refresh'}
        </button>
      </div>

      <div className="grid grid-cols-3 gap-2">
        {['excel', 'word', 'outlook'].map((app) => {
          const a = officeApps[app];
          const ok = a?.available;
          return (
            <div key={app} className="px-2 py-2 rounded text-center"
              style={{
                background: 'rgba(0,0,0,0.3)',
                border: `1px solid ${ok ? 'rgba(34, 197, 94, 0.4)' : 'rgba(251, 191, 36, 0.4)'}`,
              }}>
              <div className="text-[11px] uppercase font-bold" style={{ color: ok ? '#86efac' : '#fde68a' }}>
                {app}
              </div>
              <div className="text-[10px] font-mono mt-0.5" style={{ color: 'var(--text-muted)' }}>
                {ok ? `v${a.version} ✓` : 'unavailable'}
              </div>
            </div>
          );
        })}
      </div>

      <div className="mt-3 grid grid-cols-2 gap-2 text-[11px] font-mono">
        <div className="px-2 py-1.5 rounded" style={{ background: 'rgba(0,0,0,0.3)' }}>
          <span style={{ color: 'var(--text-muted)' }}>Screen: </span>
          <span style={{ color: '#67e8f9' }}>
            {screenInfo.width || '?'} × {screenInfo.height || '?'}
          </span>
        </div>
        <div className="px-2 py-1.5 rounded" style={{ background: 'rgba(0,0,0,0.3)' }}>
          <span style={{ color: 'var(--text-muted)' }}>Open windows: </span>
          <span style={{ color: '#67e8f9' }}>{windows?.count ?? '?'}</span>
        </div>
      </div>
    </div>
  );
}

function ImageMatchTemplatesSection() {
  const [templates, setTemplates] = useState({});
  const [busy, setBusy] = useState(false);
  const toast = useToast();

  const refresh = useCallback(() => {
    api.imgmatchTemplates().then((r) => setTemplates(r.templates || {})).catch(() => {});
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  const handleDelete = async (key) => {
    if (!confirm(`Template '${key}' delete kar do?`)) return;
    setBusy(true);
    try {
      await api.imgmatchDelete(key);
      toast.warn(`Deleted: ${key}`, 3000);
      refresh();
    } catch (e) { toast.error(`Fail: ${e.message}`); }
    finally { setBusy(false); }
  };

  const totalCount = Object.values(templates).reduce((sum, arr) => sum + arr.length, 0);

  return (
    <div className="rounded-xl p-5 mb-4"
      style={{
        background: 'rgba(15, 22, 38, 0.7)',
        border: '1px solid rgba(250, 204, 21, 0.3)',
      }}>
      <div className="flex items-center gap-3 mb-3">
        <div className="w-9 h-9 rounded flex items-center justify-center font-display font-bold text-sm"
          style={{
            background: 'linear-gradient(135deg, #facc15, #ca8a04)',
            border: '1.5px solid #fde047',
            color: '#1c1917',
          }}>
            🎯
        </div>
        <div className="flex-1">
          <h3 className="font-display text-sm uppercase tracking-widest" style={{ color: '#fde047' }}>
            Image-Match Templates ({totalCount})
          </h3>
          <p className="text-xs mt-0.5" style={{ color: 'var(--text-muted)' }}>
            Visual apps (Photoshop, Premiere) ke buttons jo capture kiye — local PNG templates, no LLM
          </p>
        </div>
        <button onClick={refresh} disabled={busy}
          className="px-3 py-1.5 rounded text-[11px] font-bold"
          style={{
            background: 'rgba(250, 204, 21, 0.15)',
            border: '1px solid rgba(250, 204, 21, 0.4)',
            color: '#fde047',
          }}>
          ↻
        </button>
      </div>

      {totalCount === 0 ? (
        <p className="text-[11px]" style={{ color: 'var(--text-dim)' }}>
          Koi template save nahi hua. POST /api/imgmatch/capture se region capture karo aur use {`{save_as: "app/button"}`} mein.
        </p>
      ) : (
        Object.entries(templates).map(([app, items]) => (
          <div key={app} className="mb-2">
            <div className="text-[11px] uppercase font-bold mb-1" style={{ color: '#fde047' }}>
              {app}
            </div>
            <div className="flex flex-wrap gap-1.5">
              {items.map((t) => (
                <div key={t.name} className="flex items-center gap-1 px-2 py-0.5 rounded text-[10px] font-mono"
                  style={{
                    background: 'rgba(0,0,0,0.4)',
                    border: '1px solid rgba(250, 204, 21, 0.3)',
                    color: '#fde047',
                  }}>
                  <span>{t.name}</span>
                  <span style={{ color: 'var(--text-dim)' }}>({t.size_kb}KB)</span>
                  <button onClick={() => handleDelete(`${app}/${t.name}`)} disabled={busy}
                    style={{ color: '#fca5a5', marginLeft: 4 }}>
                    ×
                  </button>
                </div>
              ))}
            </div>
          </div>
        ))
      )}
    </div>
  );
}

function UniversalBrowserSection() {
  const [sites, setSites] = useState([]);
  const [url, setUrl] = useState('');
  const [recordingKey, setRecordingKey] = useState('');
  const [busy, setBusy] = useState(false);
  const toast = useToast();

  const refresh = useCallback(() => {
    api.universalLearnedSites().then((r) => setSites(r.sites || [])).catch(() => {});
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const handleOpen = async () => {
    if (!url.trim() || busy) return;
    setBusy(true);
    try {
      const r = await api.universalOpen(url.trim());
      if (r.ok) toast.success(`Site opened: ${r.site_key}`, 4000);
      else toast.error(r.error || 'Open fail');
    } catch (e) { toast.error(`Fail: ${e.message}`); }
    finally { setBusy(false); }
  };

  const handleTeachStart = async () => {
    if (!url.trim() || busy) return;
    setBusy(true);
    try {
      const r = await api.universalTeachStart(url.trim());
      if (r.ok) {
        setRecordingKey(r.site_key);
        toast.success(`Recording started for ${r.site_key}. Ab manually compose + type + send karke dikha de, phir "Stop & Save" dabaa.`, 8000);
      } else { toast.error(r.error || 'Teach start fail'); }
    } catch (e) { toast.error(`Fail: ${e.message}`); }
    finally { setBusy(false); }
  };

  const handleTeachStop = async () => {
    if (!recordingKey || busy) return;
    setBusy(true);
    try {
      const r = await api.universalTeachStop(recordingKey, 'send');
      if (r.ok) {
        toast.success(`Saved ${r.steps} steps for ${r.site_key} — forever ke liye yaad rahega!`, 6000);
        setRecordingKey('');
        refresh();
      } else { toast.error(r.error || 'Save fail'); }
    } catch (e) { toast.error(`Fail: ${e.message}`); }
    finally { setBusy(false); }
  };

  return (
    <div
      className="rounded-xl p-5 mb-4"
      style={{
        background: 'rgba(15, 22, 38, 0.7)',
        border: '1px solid rgba(168, 85, 247, 0.4)',
        boxShadow: '0 0 18px rgba(168, 85, 247, 0.12)',
      }}
    >
      <div className="flex items-center gap-3 mb-3">
        <div
          className="w-9 h-9 rounded flex items-center justify-center font-display font-bold text-sm"
          style={{
            background: 'linear-gradient(135deg, #a855f7, #7e22ce)',
            border: '1.5px solid #c4b5fd',
            color: '#1a0a3a',
            boxShadow: '0 0 12px rgba(168, 85, 247, 0.5)',
          }}
        >
          ∞
        </div>
        <div>
          <h3 className="font-display text-sm uppercase tracking-widest" style={{ color: '#c4b5fd' }}>
            Universal Browser (Teach Any Site)
          </h3>
          <p className="text-xs mt-0.5" style={{ color: 'var(--text-muted)' }}>
            Slack, Discord, GitHub, Notion, X — koi bhi site. JARVIS try kare automatically, fail ho to tu 2 min mein sikhao.
          </p>
        </div>
      </div>

      <div className="flex gap-2 mt-3">
        <input
          type="text"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder="https://slack.com/  ya  https://discord.com/channels/..."
          className="flex-1 px-3 py-2 rounded text-xs font-mono"
          style={{
            background: 'rgba(0,0,0,0.4)',
            border: '1px solid rgba(168, 85, 247, 0.3)',
            color: '#e2e8f0',
          }}
        />
        <button
          onClick={handleOpen}
          disabled={busy || !url.trim()}
          className="px-3 py-2 rounded font-display uppercase tracking-widest text-[11px] font-bold"
          style={{
            background: 'rgba(168, 85, 247, 0.2)',
            border: '1px solid rgba(168, 85, 247, 0.5)',
            color: '#c4b5fd',
          }}
        >
          Open
        </button>
        {recordingKey ? (
          <button
            onClick={handleTeachStop}
            disabled={busy}
            className="px-3 py-2 rounded font-display uppercase tracking-widest text-[11px] font-bold"
            style={{
              background: 'linear-gradient(135deg, #ef4444, #b91c1c)',
              border: '1px solid #fca5a5',
              color: '#fff5f5',
              boxShadow: '0 0 14px rgba(239, 68, 68, 0.5)',
            }}
          >
            ⏹ Stop & Save
          </button>
        ) : (
          <button
            onClick={handleTeachStart}
            disabled={busy || !url.trim()}
            className="px-3 py-2 rounded font-display uppercase tracking-widest text-[11px] font-bold"
            style={{
              background: 'linear-gradient(135deg, #a855f7, #7e22ce)',
              border: '1px solid #c4b5fd',
              color: '#1a0a3a',
            }}
          >
            ⏺ Teach Site
          </button>
        )}
      </div>

      {recordingKey && (
        <p className="text-[11px] mt-2 font-mono" style={{ color: '#fbbf24' }}>
          ⏺ Recording: <strong>{recordingKey}</strong> — Chromium mein jaake compose box click kar, type kar, send dabaa. Phir Stop & Save.
        </p>
      )}

      {sites.length > 0 && (
        <div className="mt-3 pt-3" style={{ borderTop: '1px solid rgba(168, 85, 247, 0.2)' }}>
          <p className="text-[11px] mb-2" style={{ color: 'var(--text-muted)' }}>
            Learned sites ({sites.length}):
          </p>
          <div className="flex flex-wrap gap-1.5">
            {sites.map((s) => (
              <span
                key={s.site_key}
                className="px-2 py-0.5 rounded text-[10px] font-mono"
                style={{
                  background: 'rgba(168, 85, 247, 0.15)',
                  border: '1px solid rgba(168, 85, 247, 0.4)',
                  color: '#c4b5fd',
                }}
                title={`Labels: ${(s.labels || []).join(', ')}`}
              >
                {s.site_key}
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function TeamsConnectSection() {
  const [status, setStatus] = useState({ teams_session_exists: false, teams_page_alive: false });
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState('');
  const toast = useToast();

  const refresh = useCallback(() => {
    api.getTeamsStatus().then(setStatus).catch(() => {});
  }, []);

  useEffect(() => {
    refresh();
    const tick = setInterval(refresh, 8000);
    return () => clearInterval(tick);
  }, [refresh]);

  const handleConnect = async () => {
    if (busy) return;
    setBusy(true);
    setProgress('Chromium mein Teams tab khul rahi — 5-10 sec wait kar...');
    try {
      setTimeout(() => setProgress('Microsoft account se sign in kar (3 min time hai)'), 6000);
      const result = await api.setupTeams();
      if (result.ok) {
        toast.success(result.message || 'Teams connected — background mein silent chalega', 5000);
        setProgress('');
        refresh();
      } else {
        toast.error(result.message || 'Teams setup fail');
        setProgress('');
      }
    } catch (err) {
      toast.error(`Teams setup fail: ${err.message}`);
      setProgress('');
    } finally {
      setBusy(false);
    }
  };

  const connected = !!status.teams_session_exists && !!status.teams_page_alive;

  return (
    <div
      className="rounded-xl p-5 mb-4"
      style={{
        background: 'rgba(15, 22, 38, 0.7)',
        border: `1px solid ${connected ? 'rgba(99, 102, 241, 0.5)' : 'rgba(251, 191, 36, 0.4)'}`,
        boxShadow: connected ? '0 0 18px rgba(99, 102, 241, 0.18)' : '0 0 18px rgba(251, 191, 36, 0.15)',
      }}
    >
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div
            className="w-9 h-9 rounded flex items-center justify-center font-display font-bold text-sm"
            style={{
              background: connected ? 'linear-gradient(135deg, #6366f1, #4338ca)' : 'rgba(251, 191, 36, 0.2)',
              border: `1.5px solid ${connected ? '#a5b4fc' : '#fbbf24'}`,
              color: connected ? '#0c0926' : '#fbbf24',
              boxShadow: connected ? '0 0 12px rgba(99, 102, 241, 0.5)' : 'none',
            }}
          >
            {connected ? 'T' : '!'}
          </div>
          <div>
            <h3 className="font-display text-sm uppercase tracking-widest"
                style={{ color: connected ? '#a5b4fc' : '#fde68a' }}>
              Teams Connection
            </h3>
            <p className="text-xs mt-0.5" style={{ color: 'var(--text-muted)' }}>
              {connected
                ? 'Connected · Same browser, silent background sends'
                : 'Not connected — ek baar Microsoft account sign in karo'}
            </p>
            {progress && (
              <p className="text-[11px] mt-1.5 font-mono" style={{ color: '#a5b4fc' }}>
                {progress}
              </p>
            )}
          </div>
        </div>

        <button
          onClick={handleConnect}
          disabled={busy}
          className="px-5 py-2 rounded font-display uppercase tracking-widest text-xs font-bold transition-all"
          style={
            connected
              ? {
                  background: 'rgba(99, 102, 241, 0.15)',
                  border: '1px solid rgba(99, 102, 241, 0.5)',
                  color: '#a5b4fc',
                }
              : {
                  background: 'linear-gradient(135deg, #6366f1, #4338ca)',
                  border: '1px solid #a5b4fc',
                  color: '#0c0926',
                  boxShadow: '0 0 18px rgba(99, 102, 241, 0.4)',
                }
          }
        >
          {busy ? 'Connecting...' : connected ? '✓ Connected (reconnect)' : '⚡ Connect Teams'}
        </button>
      </div>
    </div>
  );
}

function ChromeBackgroundSection() {
  const [status, setStatus] = useState({ available: false });
  const [busy, setBusy] = useState(false);
  const toast = useToast();

  useEffect(() => {
    api.getChromeStatus().then(setStatus).catch(() => {});
    const tick = setInterval(
      () => api.getChromeStatus().then(setStatus).catch(() => {}),
      8000,
    );
    return () => clearInterval(tick);
  }, []);

  const handleEnable = async () => {
    if (!confirm('Chrome ko restart karna hoga (saari tabs save ho jayengi). Continue?')) return;
    setBusy(true);
    try {
      const result = await api.enableChromeBackground();
      setStatus(result.status);
      if (result.success) {
        toast.success('Chrome background mode active — messages invisible bhejenge', 5000);
      } else {
        toast.error(result.message || 'Failed to enable');
      }
    } catch (err) {
      toast.error(`Failed: ${err.message}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      className="rounded-xl p-5 mb-4"
      style={{
        background: 'rgba(15, 22, 38, 0.7)',
        border: `1px solid ${status.available ? 'rgba(34, 211, 238, 0.4)' : 'rgba(251, 191, 36, 0.4)'}`,
      }}
    >
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div
            className="w-9 h-9 rounded flex items-center justify-center font-display font-bold text-sm"
            style={{
              background: status.available
                ? 'linear-gradient(135deg, #22d3ee, #0891b2)'
                : 'rgba(251, 191, 36, 0.2)',
              border: `1.5px solid ${status.available ? '#67e8f9' : '#fbbf24'}`,
              color: status.available ? '#001018' : '#fbbf24',
              boxShadow: status.available ? '0 0 12px rgba(34, 211, 238, 0.5)' : 'none',
            }}
          >
            {status.available ? '◉' : '!'}
          </div>
          <div>
            <h3 className="font-display text-sm uppercase tracking-widest"
                style={{ color: status.available ? 'var(--cyan-bright)' : '#fde68a' }}>
              Background Mode (Chrome)
            </h3>
            <p className="text-xs mt-0.5" style={{ color: 'var(--text-muted)' }}>
              {status.available
                ? `Active · ${status.tabs} tabs · WhatsApp ${status.whatsapp_open ? 'detected' : 'not open'}`
                : 'Disabled — messages tera screen pe dikhe ge (window switch)'}
            </p>
            <p className="text-[10px] mt-1" style={{ color: 'var(--text-dim)' }}>
              {status.available
                ? 'Messages aur Chrome actions ab background mein chal rahe — tera kaam disturb nahi hoga'
                : 'Enable kar to invisible message sending — Chrome restart hoga (tabs restore)'}
            </p>
          </div>
        </div>

        {!status.available && (
          <button
            onClick={handleEnable}
            disabled={busy}
            className="px-5 py-2 rounded font-display uppercase tracking-widest text-xs font-bold transition-all"
            style={{
              background: 'linear-gradient(135deg, #22d3ee, #0891b2)',
              border: '1px solid #67e8f9',
              color: '#001018',
              boxShadow: '0 0 18px rgba(34, 211, 238, 0.5)',
            }}
          >
            {busy ? 'Restarting...' : '⚡ Enable Background'}
          </button>
        )}
      </div>
    </div>
  );
}

function ConsentSection() {
  const [consent, setConsent] = useState({ granted: false });
  const [busy, setBusy] = useState(false);
  const toast = useToast();

  useEffect(() => {
    api.getConsent().then(setConsent).catch(() => {});
  }, []);

  const handleToggle = async () => {
    setBusy(true);
    const wasGranted = consent.granted;
    try {
      const result = wasGranted
        ? await api.revokeConsent()
        : await api.grantConsent();
      // Re-fetch server state instead of trusting the toggle response, so the
      // UI never lies about consent if the server rejected the change.
      const fresh = await api.getConsent().catch(() => result);
      setConsent(fresh);
      const nowGranted = !!fresh?.granted;
      if (nowGranted === wasGranted) {
        toast.warn('Consent state unchanged — server rejected the request');
      } else if (nowGranted) {
        toast.success('Permission granted — full laptop access activated', 4000);
      } else {
        toast.warn('Permission removed — laptop access disabled', 4000);
      }
    } catch (err) {
      toast.error(`Failed: ${err.message}`);
      // Refresh after error too — keeps UI honest
      api.getConsent().then(setConsent).catch(() => {});
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      className="rounded-xl p-5 mb-4"
      style={{
        background: 'rgba(15, 22, 38, 0.7)',
        border: `1px solid ${consent.granted ? 'rgba(34, 211, 238, 0.4)' : 'rgba(239, 68, 68, 0.4)'}`,
        boxShadow: consent.granted
          ? '0 0 20px rgba(34, 211, 238, 0.15)'
          : '0 0 20px rgba(239, 68, 68, 0.15)',
      }}
    >
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div
            className="w-9 h-9 rounded-full flex items-center justify-center"
            style={{
              background: consent.granted
                ? 'radial-gradient(circle, #67e8f9 0%, #22d3ee 50%, transparent 80%)'
                : 'rgba(239, 68, 68, 0.2)',
              border: `2px solid ${consent.granted ? '#67e8f9' : '#fca5a5'}`,
              boxShadow: consent.granted
                ? '0 0 14px rgba(34, 211, 238, 0.6)'
                : 'none',
            }}
          >
            <span style={{ color: consent.granted ? '#001018' : '#fca5a5', fontWeight: 'bold' }}>
              {consent.granted ? '✓' : '✕'}
            </span>
          </div>
          <div>
            <h3 className="font-display text-sm uppercase tracking-widest"
                style={{ color: consent.granted ? 'var(--cyan-bright)' : '#fca5a5' }}>
              Laptop Access
            </h3>
            <p className="text-xs mt-0.5" style={{ color: 'var(--text-muted)' }}>
              {consent.granted
                ? 'Full access granted — sab features unlocked'
                : 'Access revoked — sirf chat mode active'}
            </p>
            {consent.granted_at && (
              <p className="text-[10px] mt-1 font-mono" style={{ color: 'var(--text-dim)' }}>
                Granted: {new Date(consent.granted_at).toLocaleString()}
              </p>
            )}
          </div>
        </div>

        <button
          onClick={handleToggle}
          disabled={busy}
          className="px-5 py-2 rounded font-display uppercase tracking-widest text-xs font-bold transition-all"
          style={
            consent.granted
              ? {
                  background: 'rgba(239, 68, 68, 0.15)',
                  border: '1px solid rgba(239, 68, 68, 0.5)',
                  color: '#fca5a5',
                }
              : {
                  background: 'linear-gradient(135deg, #22d3ee, #0891b2)',
                  border: '1px solid #67e8f9',
                  color: '#001018',
                  boxShadow: '0 0 18px rgba(34, 211, 238, 0.5)',
                }
          }
        >
          {busy ? '...' : consent.granted ? '✕ Revoke' : '✓ Grant Access'}
        </button>
      </div>
    </div>
  );
}
