import { useCallback } from 'react';
import { api } from '../api';
import { usePolling } from '../hooks/usePolling';

// JARVIS task-agents (har command ek background agent) ka LIVE — VIP panel.
const STATUS = {
  queued: { color: '#ff8a1f', label: 'QUEUED', icon: '◷' },
  running: { color: '#22d3ee', label: 'RUNNING', icon: '⚡' },
  completed: { color: '#10b981', label: 'DONE', icon: '✓' },
  failed: { color: '#ef4444', label: 'FAILED', icon: '✕' },
};

const CSS = `
@keyframes wkShimmer { 0%{background-position:-200% 0} 100%{background-position:200% 0} }
@keyframes wkSpin { to { transform: rotate(360deg) } }
@keyframes wkPulseGlow { 0%,100%{box-shadow:0 0 8px rgba(34,211,238,.25)} 50%{box-shadow:0 0 22px rgba(34,211,238,.6)} }
@keyframes wkFadeUp { from{opacity:0;transform:translateY(8px)} to{opacity:1;transform:none} }
@keyframes wkDots { 0%{opacity:.2} 20%{opacity:1} 100%{opacity:.2} }
.wk-card{animation:wkFadeUp .25s ease both}
.wk-run{animation:wkPulseGlow 1.8s ease-in-out infinite}
.wk-bar{height:3px;border-radius:3px;background:linear-gradient(90deg,transparent,#22d3ee,transparent);
  background-size:200% 100%;animation:wkShimmer 1.3s linear infinite}
.wk-ring{animation:wkSpin 1.4s linear infinite}
.wk-orb{transition:all .2s ease}
.wk-orb:hover{transform:translateY(-2px)}
.wk-dot1{animation:wkDots 1.2s infinite}.wk-dot2{animation:wkDots 1.2s .2s infinite}.wk-dot3{animation:wkDots 1.2s .4s infinite}
`;

function Orb({ n, label, color, glow }) {
  return (
    <div className="wk-orb flex flex-col items-center justify-center rounded-xl px-4 py-2"
      style={{ minWidth: 78, background: 'rgba(255,255,255,0.03)',
        border: `1px solid ${glow ? color : 'var(--border)'}`,
        boxShadow: glow && n > 0 ? `0 0 16px ${color}55` : 'none' }}>
      <span className="font-display font-bold leading-none" style={{ fontSize: 24, color }}>{n}</span>
      <span className="text-[9px] uppercase tracking-[0.2em] mt-1" style={{ color: 'var(--text-dim)' }}>{label}</span>
    </div>
  );
}

export default function WorkersPage() {
  const fetchJobs = useCallback(() => api.getAgentJobs(60), []);
  const { data, loading, error, refresh } = usePolling(fetchJobs, 2000);
  const del = async (id) => {
    try { await api.deleteAgentJob(id); refresh(); } catch (e) { /* ignore */ }
  };

  if (loading && !data) return <p style={{ color: 'var(--text-muted)' }}>Loading agents…</p>;
  if (error) return <p className="text-red-400">Error: {error}</p>;

  const jobs = (data && data.jobs) || [];
  const c = (s) => jobs.filter((j) => j.status === s).length;
  const running = c('running'), queued = c('queued'), done = c('completed'), failed = c('failed');

  return (
    <div>
      <style>{CSS}</style>

      {/* Header */}
      <div className="flex items-center justify-between mb-5 flex-wrap gap-4">
        <div className="flex items-center gap-3">
          <div className="relative flex items-center justify-center" style={{ width: 44, height: 44 }}>
            {running > 0 && (
              <div className="wk-ring absolute inset-0 rounded-full" style={{
                border: '2px solid transparent', borderTopColor: 'var(--cyan)',
                borderRightColor: 'var(--cyan)' }} />
            )}
            <span style={{ fontSize: 22 }}>🤖</span>
          </div>
          <div>
            <h2 className="text-2xl font-bold font-display" style={{ color: 'var(--cyan-bright)', letterSpacing: '0.04em' }}>
              LIVE AGENTS
            </h2>
            <p className="text-[11px]" style={{ color: 'var(--text-dim)' }}>
              JARVIS ke workers — har kaam ek agent · live · har 2s
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2.5">
          <Orb n={running} label="Running" color="#22d3ee" glow />
          <Orb n={queued} label="Queued" color="#ff8a1f" glow />
          <Orb n={done} label="Done" color="#10b981" />
          <Orb n={failed} label="Failed" color="#ef4444" />
        </div>
      </div>

      <div className="h-px w-full mb-4" style={{ background: 'linear-gradient(90deg,var(--cyan),transparent)' }} />

      {jobs.length === 0 ? (
        <div className="j-panel rounded-xl p-12 text-center" style={{ color: 'var(--text-muted)' }}>
          <div className="text-4xl mb-3">🪄</div>
          <p className="font-display">Koi agent abhi nahi.</p>
          <p className="text-xs mt-2" style={{ color: 'var(--text-dim)' }}>
            Chat mein kaam do — jaise <span style={{ color: 'var(--cyan)' }}>"ek file banao aur ek research karo"</span> —
            har kaam ek agent banega, sab yahan live dikhenge.
          </p>
        </div>
      ) : (
        <div className="space-y-3">
          {jobs.map((j) => {
            const s = STATUS[j.status] || STATUS.queued;
            const isRun = j.status === 'running';
            return (
              <div key={j.job_id}
                className={`wk-card rounded-xl overflow-hidden ${isRun ? 'wk-run' : ''}`}
                style={{ background: 'linear-gradient(135deg, rgba(255,255,255,0.025), rgba(255,255,255,0.005))',
                  border: `1px solid ${isRun ? 'rgba(34,211,238,0.45)' : j.status === 'failed' ? 'rgba(239,68,68,0.35)' : 'var(--border)'}` }}>
                <div className="p-4 flex items-start gap-3">
                  {/* Badge */}
                  <div className="relative flex items-center justify-center shrink-0" style={{ width: 40, height: 40 }}>
                    {isRun && <div className="wk-ring absolute inset-0 rounded-full"
                      style={{ border: '2px solid transparent', borderTopColor: s.color, borderRightColor: s.color }} />}
                    <div className="rounded-full flex items-center justify-center font-display font-bold"
                      style={{ width: 32, height: 32, fontSize: 12, color: s.color,
                        background: `${s.color}1a`, border: `1px solid ${s.color}55` }}>
                      {j.num}
                    </div>
                  </div>
                  {/* Body */}
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="font-bold font-display" style={{ color: 'var(--text)' }}>Agent #{j.num}</span>
                      <span className="text-[10px] font-bold px-2 py-0.5 rounded uppercase tracking-wider font-display flex items-center gap-1"
                        style={{ color: s.color, background: `${s.color}1a`, border: `1px solid ${s.color}40` }}>
                        <span>{s.icon}</span>{s.label}
                      </span>
                      <span className="ml-auto text-[11px] font-mono" style={{ color: 'var(--text-dim)' }}>{j.created_at}</span>
                      <button onClick={() => del(j.job_id)} title="Remove"
                        className="rounded flex items-center justify-center transition-all"
                        style={{ width: 20, height: 20, fontSize: 11, color: 'var(--text-dim)', background: 'rgba(255,255,255,0.05)' }}
                        onMouseEnter={(e) => { e.currentTarget.style.color = '#ef4444'; e.currentTarget.style.background = 'rgba(239,68,68,0.15)'; }}
                        onMouseLeave={(e) => { e.currentTarget.style.color = 'var(--text-dim)'; e.currentTarget.style.background = 'rgba(255,255,255,0.05)'; }}>
                        ✕
                      </button>
                    </div>
                    <div className="text-sm mt-1" style={{ color: 'var(--text-muted)' }}>{j.task}</div>
                    {isRun && (
                      <div className="text-[11px] mt-1.5 flex items-center gap-1" style={{ color: 'var(--cyan)' }}>
                        kaam ho raha hai
                        <span className="wk-dot1">.</span><span className="wk-dot2">.</span><span className="wk-dot3">.</span>
                      </div>
                    )}
                    {j.result && (
                      <div className="text-xs mt-1.5 break-words" style={{ color: 'rgba(16,185,129,0.9)' }}>✓ {j.result}</div>
                    )}
                    {j.error && (
                      <div className="text-xs mt-1.5 break-words" style={{ color: 'rgba(239,68,68,0.9)' }}>⚠ {j.error}</div>
                    )}
                  </div>
                </div>
                {isRun && <div className="wk-bar" />}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
