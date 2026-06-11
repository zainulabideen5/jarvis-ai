import { useState, useEffect } from 'react';
import { api } from '../api';
import { useToast } from './Toast';

const PERMISSIONS = [
  { icon: '📁', label: 'Files & Folders', desc: 'Saari files, folders, drives access' },
  { icon: '📱', label: 'Apps', desc: 'Chrome, Teams, Excel, Word, kuch bhi' },
  { icon: '🌐', label: 'Browser Tabs', desc: 'Chrome ki saari tabs ka control' },
  { icon: '🎤', label: 'Audio / Mic', desc: 'Meetings sun ke transcribe karna' },
  { icon: '👁️', label: 'Screen / Vision', desc: 'Screenshots aur screen analysis' },
  { icon: '⌨️', label: 'Keyboard / Mouse', desc: 'Auto-typing aur clicking' },
  { icon: '⚙️', label: 'System Commands', desc: 'Volume, lock, shutdown, wifi' },
  { icon: '💬', label: 'Send Messages', desc: 'WhatsApp, Teams, Email auto-send' },
];

const BLOCKED = [
  '🏦 Bank apps (HBL, Meezan, etc.)',
  '💳 Payment apps (JazzCash, EasyPaisa)',
  '🔐 Password managers',
];

export default function ConsentModal({ onConsentChange }) {
  const [show, setShow] = useState(false);
  const [loading, setLoading] = useState(true);
  const [consent, setConsent] = useState({ granted: false });
  const [busy, setBusy] = useState(false);
  const toast = useToast();

  useEffect(() => {
    api.getConsent()
      .then((c) => {
        setConsent(c);
        setShow(!c.granted);
        setLoading(false);
      })
      .catch(() => {
        setLoading(false);
      });
  }, []);

  const handleGrant = async () => {
    setBusy(true);
    try {
      const result = await api.grantConsent();
      setConsent(result);
      setShow(false);
      onConsentChange?.(true);
      toast.success('Permission granted — full laptop access activated', 4000);
    } catch (err) {
      toast.error(`Permission failed: ${err.message}`);
    } finally {
      setBusy(false);
    }
  };

  const handleReject = () => {
    setShow(false);
    onConsentChange?.(false);
    toast.warn('Permission rejected — sirf chat mode active', 4000);
  };

  if (loading || !show) return null;

  return (
    <div
      className="fixed inset-0 z-[100] flex items-center justify-center p-6"
      style={{
        background: 'rgba(0, 0, 0, 0.85)',
        backdropFilter: 'blur(8px)',
      }}
    >
      {/* Backdrop scan effect */}
      <div className="j-scan-line" />

      <div
        className="j-panel j-panel-bracket max-w-2xl w-full p-8 relative"
        style={{
          background: 'rgba(15, 22, 38, 0.95)',
          border: '1.5px solid var(--cyan)',
          boxShadow: '0 0 40px rgba(34, 211, 238, 0.4), 0 0 80px rgba(233, 30, 99, 0.2)',
        }}
      >
        {/* Top label */}
        <div className="flex items-center justify-between mb-2">
          <span className="font-mono text-[10px] tracking-[0.3em] uppercase"
                style={{ color: 'var(--text-dim)' }}>
            ◉ AUTHORIZATION_REQUIRED
          </span>
          <span className="font-mono text-[10px]" style={{ color: 'var(--cyan)' }}>
            v0.1.0
          </span>
        </div>

        {/* Title */}
        <div className="flex items-center gap-3 mb-6">
          <div
            className="w-12 h-12 rounded-full flex items-center justify-center"
            style={{
              background: 'radial-gradient(circle, #67e8f9 0%, #22d3ee 50%, transparent 80%)',
              border: '2px solid #67e8f9',
              boxShadow: '0 0 20px rgba(34, 211, 238, 0.7)',
              animation: 'jPulse 2s ease-in-out infinite',
            }}
          >
            <div className="w-2.5 h-2.5 rounded-full"
                 style={{ background: '#fff', boxShadow: '0 0 8px #fff' }} />
          </div>
          <div>
            <h1 className="font-display text-2xl uppercase tracking-[0.15em] j-title-glow">
              FULL ACCESS REQUIRED
            </h1>
            <p className="text-sm mt-1" style={{ color: 'var(--text-muted)' }}>
              Ye software aap ke laptop ka full access lega kaam karne ke liye
            </p>
          </div>
        </div>

        {/* Permissions list */}
        <div
          className="rounded p-4 mb-4"
          style={{
            background: 'rgba(0, 0, 0, 0.4)',
            border: '1px solid var(--border)',
          }}
        >
          <p className="font-display text-[10px] uppercase tracking-widest mb-3"
             style={{ color: 'var(--cyan-bright)' }}>
            ▸ ALLOWED ACCESS
          </p>
          <div className="grid grid-cols-2 gap-2.5">
            {PERMISSIONS.map((p) => (
              <div key={p.label} className="flex items-start gap-2 text-xs">
                <span className="text-base">{p.icon}</span>
                <div>
                  <div className="font-display uppercase tracking-wider"
                       style={{ color: 'var(--text)', fontSize: '11px' }}>
                    {p.label}
                  </div>
                  <div style={{ color: 'var(--text-muted)', fontSize: '10px' }}>
                    {p.desc}
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* Blocked */}
        <div
          className="rounded p-4 mb-5"
          style={{
            background: 'rgba(239, 68, 68, 0.05)',
            border: '1px solid rgba(239, 68, 68, 0.3)',
          }}
        >
          <p className="font-display text-[10px] uppercase tracking-widest mb-2"
             style={{ color: '#fca5a5' }}>
            🚫 BLOCKED — KABHI ACCESS NAHI
          </p>
          <div className="space-y-1">
            {BLOCKED.map((b) => (
              <p key={b} className="text-xs" style={{ color: 'var(--text-muted)' }}>{b}</p>
            ))}
          </div>
        </div>

        {/* Footer note */}
        <p className="text-xs mb-5 text-center" style={{ color: 'var(--text-muted)' }}>
          Aap kabhi bhi <span style={{ color: 'var(--cyan-bright)' }}>Settings → Revoke</span> se
          permissions hata sakte hain.
        </p>

        {/* Buttons */}
        <div className="flex gap-3">
          <button
            onClick={handleReject}
            disabled={busy}
            className="flex-1 px-6 py-3 rounded font-display uppercase tracking-widest text-sm transition-all"
            style={{
              background: 'rgba(239, 68, 68, 0.1)',
              border: '1px solid rgba(239, 68, 68, 0.5)',
              color: '#fca5a5',
            }}
          >
            ✕ Reject (Chat Only Mode)
          </button>
          <button
            onClick={handleGrant}
            disabled={busy}
            className="flex-1 px-6 py-3 rounded font-display uppercase tracking-widest text-sm font-bold transition-all"
            style={{
              background: 'linear-gradient(135deg, #22d3ee, #0891b2)',
              border: '1px solid #67e8f9',
              color: '#001018',
              boxShadow: '0 0 25px rgba(34, 211, 238, 0.6)',
            }}
          >
            {busy ? 'Activating...' : '✓ Grant Full Access'}
          </button>
        </div>
      </div>
    </div>
  );
}
