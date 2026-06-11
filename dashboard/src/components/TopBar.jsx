import { useState, useEffect } from 'react';
import { api } from '../api';

const TABS = [
  { id: 'stats', label: 'Intelligence' },
  { id: 'transcriptions', label: 'Notes' },
  { id: 'tasks', label: 'Tasks' },
];

export default function TopBar({ activePage, onNavigate }) {
  const [time, setTime] = useState(new Date());
  const [stats, setStats] = useState({});
  const [listening, setListening] = useState(false);

  useEffect(() => {
    const tick = setInterval(() => setTime(new Date()), 1000);
    const fetchData = () => {
      api.getStats().then(setStats).catch(() => {});
      api.getListening().then((d) => setListening(d.listening === 'on')).catch(() => {});
    };
    fetchData();
    const dataTick = setInterval(fetchData, 5000);
    return () => { clearInterval(tick); clearInterval(dataTick); };
  }, []);

  return (
    <header
      className="h-14 flex items-center justify-between px-6 border-b relative z-20"
      style={{
        borderColor: 'var(--border)',
        background: 'linear-gradient(180deg, rgba(10, 13, 24, 0.92), rgba(6, 8, 16, 0.95))',
        backdropFilter: 'blur(14px)',
      }}
    >
      {/* Left — Brand mark */}
      <div className="flex items-center gap-3">
        <div
          className="w-8 h-8 rounded flex items-center justify-center"
          style={{
            background: 'radial-gradient(circle, #67e8f9 0%, #22d3ee 50%, transparent 80%)',
            border: '1.5px solid #67e8f9',
            boxShadow: '0 0 16px rgba(34, 211, 238, 0.6)',
          }}
        >
          <div className="w-2 h-2 rounded-full" style={{ background: '#fff', boxShadow: '0 0 6px #fff' }} />
        </div>
      </div>

      {/* Center — Tabs */}
      <nav className="flex items-center gap-2 absolute left-1/2 -translate-x-1/2">
        {TABS.map((tab) => {
          const active = activePage === tab.id;
          return (
            <button
              key={tab.id}
              onClick={() => onNavigate?.(tab.id)}
              className="px-4 py-1.5 rounded font-display text-[11px] uppercase tracking-[0.2em] transition-all"
              style={{
                background: active
                  ? 'linear-gradient(90deg, rgba(34,211,238,0.2), rgba(233,30,99,0.15))'
                  : 'transparent',
                color: active ? 'var(--cyan-bright)' : 'var(--text-muted)',
                border: active ? '1px solid var(--cyan)' : '1px solid transparent',
                boxShadow: active ? '0 0 15px rgba(34, 211, 238, 0.4)' : 'none',
                fontWeight: active ? 700 : 500,
              }}
            >
              {tab.label}
            </button>
          );
        })}
      </nav>

      {/* Right — status + clock */}
      <div className="flex items-center gap-5">
        <StatusItem label="CHK" value={stats.total_chunks || 0} />
        <StatusItem label="TASKS" value={stats.total_tasks || 0} highlight={stats.pending_tasks > 5} />
        <Divider />
        <div className="flex items-center gap-2">
          <span className={listening ? 'j-dot j-dot-on' : 'j-dot j-dot-off'} />
          <span
            className="font-display text-[10px] tracking-widest uppercase"
            style={{ color: listening ? 'var(--cyan-bright)' : 'var(--text-muted)' }}
          >
            {listening ? 'LIVE' : 'STBY'}
          </span>
        </div>
        <Divider />
        <div className="text-right">
          <div className="font-mono text-sm" style={{ color: 'var(--cyan-bright)' }}>
            {time.toLocaleTimeString('en-US', { hour12: false })}
          </div>
        </div>
      </div>
    </header>
  );
}

function StatusItem({ label, value, highlight }) {
  return (
    <div className="flex items-center gap-1.5">
      <span
        className="font-display text-[9px] uppercase tracking-widest"
        style={{ color: 'var(--text-muted)' }}
      >
        {label}
      </span>
      <span
        className="font-display text-sm font-bold"
        style={{
          color: highlight ? '#ef4444' : 'var(--cyan-bright)',
          textShadow: highlight ? '0 0 6px rgba(239, 68, 68, 0.5)' : '0 0 6px rgba(34, 211, 238, 0.4)',
        }}
      >
        {value}
      </span>
    </div>
  );
}

function Divider() {
  return (
    <div
      className="h-5 w-px"
      style={{ background: 'linear-gradient(180deg, transparent, rgba(34,211,238,0.4), transparent)' }}
    />
  );
}
