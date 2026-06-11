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
        <KeyRow name="JARVIS_GROQ_API_KEY" purpose="Whisper + chat LLM (task extraction)" source="console.groq.com" />
        <KeyRow name="JARVIS_GMAIL_ADDRESS" purpose="Email bhejne ke liye (SMTP)" source="Your Gmail address" />
        <KeyRow name="JARVIS_GMAIL_APP_PASSWORD" purpose="Gmail SMTP auth" source="Google Account > Security > App passwords" />
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
