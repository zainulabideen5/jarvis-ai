import { useCallback } from 'react';
import { api } from '../api';
import { usePolling } from '../hooks/usePolling';

export default function StatsPage() {
  const fetchStats = useCallback(() => api.getStats(), []);
  const { data, loading, error } = usePolling(fetchStats, 3000);

  if (loading) return <p style={{ color: 'var(--text-muted)' }}>Initializing system...</p>;
  if (error) return <p className="text-red-400">Error: {error}</p>;

  const stats = data || {};

  return (
    <div className="flex flex-col gap-5 relative">
      {/* Top Telemetry Bar */}
      <TelemetryBar stats={stats} />

      {/* Main grid */}
      <div className="grid grid-cols-12 gap-5 relative">
        <div className="col-span-3 flex flex-col gap-5">
          <CameraPanel />
          <HeadlinesPanel stats={stats} />
        </div>

        <div className="col-span-5 flex flex-col gap-5 relative">
          <InputHubPanel data={stats} />
          <ConnectorLines />
          <VisualHubPanel />
        </div>

        <div className="col-span-4 flex flex-col gap-5">
          <SpherePanel stats={stats} />
        </div>
      </div>
    </div>
  );
}

/* ===== Reusable Panel ===== */
function Panel({ children, className = '', label = '', id = '', glow = false }) {
  return (
    <div
      className={`${glow ? 'j-panel-glow' : 'j-panel'} j-panel-bracket j-hover-lift relative ${className}`}
    >
      {label && <span className="j-corner-label">◉ {label}</span>}
      {id && <span className="j-corner-id">{id}</span>}
      {children}
    </div>
  );
}

/* ===== Telemetry Bar ===== */
function TelemetryBar({ stats }) {
  const items = [
    { label: 'AUDIO_INPUT', value: stats.total_chunks || 0, max: 1000, color: '#22d3ee' },
    { label: 'TRANSCRIPTS', value: stats.total_transcriptions || 0, max: 800, color: '#67e8f9' },
    { label: 'TASK_QUEUE', value: stats.pending_tasks || 0, max: 50, color: '#e91e63' },
    { label: 'AGENTS', value: stats.agents_online || 0, max: 5, color: '#10b981' },
  ];

  return (
    <div className="j-panel j-panel-bracket px-6 py-4 flex items-center gap-8">
      <div className="flex items-center gap-3">
        <div className="j-loader-ring" />
        <div>
          <p className="font-display text-[9px] tracking-widest uppercase"
             style={{ color: 'var(--text-muted)' }}>
            System Telemetry
          </p>
          <p className="font-display text-xs uppercase tracking-wider j-glow"
             style={{ color: 'var(--cyan-bright)' }}>
            Real-time · Active
          </p>
        </div>
      </div>

      <div className="flex-1 grid grid-cols-4 gap-6">
        {items.map((item) => {
          const pct = Math.min(100, (item.value / item.max) * 100);
          return (
            <div key={item.label}>
              <div className="flex items-center justify-between mb-1.5">
                <span className="font-display text-[9px] tracking-widest uppercase"
                      style={{ color: 'var(--text-muted)' }}>
                  {item.label}
                </span>
                <span className="font-mono text-xs font-bold"
                      style={{ color: item.color }}>
                  {item.value}
                </span>
              </div>
              <div className="j-progress">
                <div
                  className="j-progress-fill"
                  style={{
                    width: `${pct}%`,
                    background: `linear-gradient(90deg, ${item.color}, ${item.color}dd)`,
                    boxShadow: `0 0 8px ${item.color}`,
                  }}
                />
              </div>
            </div>
          );
        })}
      </div>

      <div className="flex items-center gap-3 pl-6 border-l"
           style={{ borderColor: 'var(--border)' }}>
        <div className="j-holo-node">
          <span className="font-display text-[10px] font-bold" style={{ color: 'var(--cyan-bright)' }}>
            ◉
          </span>
        </div>
      </div>
    </div>
  );
}

/* ===== Camera Panel ===== */
function CameraPanel() {
  return (
    <Panel className="p-6 flex flex-col items-center justify-center min-h-[200px]" label="CAM_01" id="OFFLINE">
      <div className="w-14 h-14 rounded border-2 mb-3 flex items-center justify-center j-pulse-glow"
           style={{ borderColor: 'var(--border-bright)' }}>
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="var(--cyan)" strokeWidth="2">
          <path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/>
          <circle cx="12" cy="13" r="4"/>
        </svg>
      </div>
      <p className="font-display text-sm uppercase tracking-widest" style={{ color: 'var(--text-muted)' }}>
        Camera Offline
      </p>
      <button className="j-btn mt-4 text-[10px]">[ ENABLE ]</button>

      <div className="absolute bottom-2 left-2 right-2 flex justify-between font-mono text-[8px]"
           style={{ color: 'var(--text-dim)' }}>
        <span>0x0001</span>
        <span>FPS: --</span>
      </div>
    </Panel>
  );
}

/* ===== Headlines Panel ===== */
function HeadlinesPanel({ stats }) {
  const items = [
    { time: 'Now', text: `${stats.total_chunks || 0} audio chunks captured`, color: 'var(--cyan)' },
    { time: '5m', text: `${stats.agents_online || 0} agent(s) online`, color: 'var(--cyan-bright)' },
    { time: '12m', text: `${stats.pending_tasks || 0} tasks awaiting review`, color: 'var(--magenta)' },
    { time: '1h', text: `${stats.total_transcriptions || 0} transcripts processed`, color: 'var(--cyan)' },
  ];

  return (
    <Panel className="p-5 flex-1" label="HDLN" id={`${items.length} ENTRIES`}>
      <div className="flex items-center justify-between mb-4 mt-3">
        <h3 className="font-display text-xs uppercase tracking-widest j-glow"
            style={{ color: 'var(--cyan-bright)' }}>
          Today Headlines
        </h3>
        <div className="flex gap-1">
          <span className="j-dot j-dot-on" />
          <span className="j-dot j-dot-off" />
          <span className="j-dot j-dot-off" />
        </div>
      </div>

      <div className="space-y-3">
        {items.map((item, i) => (
          <div key={i} className="flex items-start gap-3 text-xs">
            <span className="font-mono px-2 py-0.5 rounded text-center"
                  style={{
                    color: item.color,
                    background: 'rgba(34, 211, 238, 0.08)',
                    border: '1px solid rgba(34, 211, 238, 0.2)',
                    minWidth: '40px',
                    fontSize: '10px',
                  }}>
              {item.time}
            </span>
            <span className="flex-1 pt-0.5" style={{ color: 'var(--text)' }}>{item.text}</span>
          </div>
        ))}
      </div>

      <div className="mt-5 pt-3 border-t flex items-center justify-between"
           style={{ borderColor: 'var(--border)' }}>
        <div className="flex items-center gap-2">
          <span className="j-dot j-dot-on" />
          <span className="font-mono text-[9px] tracking-widest"
                style={{ color: 'var(--text-dim)' }}>STREAM_ACTIVE</span>
        </div>
        <span className="font-mono text-[9px]" style={{ color: 'var(--cyan)' }}>↻ AUTO</span>
      </div>
    </Panel>
  );
}

/* ===== Input Hub ===== */
function InputHubPanel({ data }) {
  return (
    <Panel className="p-6" label="INPUT" id="DATA_STREAM">
      <div className="flex items-center justify-between mb-5 mt-2">
        <div className="flex items-center gap-2">
          <span className="j-dot j-dot-on" />
          <h3 className="font-display text-xs uppercase tracking-widest"
              style={{ color: 'var(--cyan-bright)' }}>
            Input Sources
          </h3>
        </div>
        <span className="font-mono text-[9px] tracking-widest"
              style={{ color: 'var(--text-dim)' }}>3 CHANNELS</span>
      </div>

      <div className="grid grid-cols-3 gap-4 mb-4 relative">
        <InputBlock icon="+" label="Add Files" subtitle="Drag & Drop"
                    color="#ff8a1f" value={data.total_chunks} />
        <InputBlock icon="◐" label="Audio" subtitle="Live Stream"
                    color="#22d3ee" value={data.total_transcriptions} active />
        <InputBlock icon="A" label="Agents" subtitle="Connected"
                    color="#e91e63" value={data.agents_online} />
      </div>

      <div className="text-center pt-3 border-t"
           style={{ borderColor: 'var(--border)' }}>
        <p className="font-display text-[10px] uppercase tracking-[0.3em]"
           style={{ color: 'var(--text-dim)' }}>
          ── Business · Documents ──
        </p>
      </div>
    </Panel>
  );
}

function InputBlock({ icon, label, subtitle, color, value, active }) {
  return (
    <div
      className="border rounded-lg p-4 text-center relative transition-all j-hover-lift cursor-pointer"
      style={{
        borderColor: active ? color : 'var(--border)',
        background: active ? `${color}15` : 'rgba(0,0,0,0.3)',
        boxShadow: active ? `0 0 25px ${color}50` : 'none',
      }}
    >
      <span className="absolute top-1.5 right-1.5 font-mono text-[8px]"
            style={{ color: active ? color : 'var(--text-dim)' }}>
        ◢
      </span>

      <div
        className="w-10 h-10 mx-auto rounded-full flex items-center justify-center mb-2 font-display font-bold"
        style={{
          background: `${color}20`,
          border: `1.5px solid ${color}`,
          color,
          boxShadow: `0 0 12px ${color}40`,
        }}
      >
        {icon}
      </div>
      <p className="font-display text-[10px] uppercase tracking-wider mb-0.5"
         style={{ color: 'var(--text)' }}>{label}</p>
      <p className="font-mono text-[9px]" style={{ color: 'var(--text-dim)' }}>{subtitle}</p>
      {value !== undefined && (
        <p className="font-display text-xl mt-2 font-bold" style={{ color }}>{value ?? 0}</p>
      )}

      {active && (
        <div className="absolute bottom-1.5 left-2 right-2 flex justify-between items-center">
          <div className="flex gap-0.5">
            <span className="w-1 h-1 rounded-full" style={{ background: color, animation: 'jPulse 1s infinite' }} />
            <span className="w-1 h-1 rounded-full" style={{ background: color, animation: 'jPulse 1s infinite 0.3s' }} />
            <span className="w-1 h-1 rounded-full" style={{ background: color, animation: 'jPulse 1s infinite 0.6s' }} />
          </div>
          <span className="font-mono text-[7px]" style={{ color }}>LIVE</span>
        </div>
      )}
    </div>
  );
}

/* ===== Connector Lines (between Input Hub and Visual Hub) ===== */
function ConnectorLines() {
  return (
    <div className="relative h-0">
      <svg
        width="100%"
        height="40"
        style={{
          position: 'absolute',
          top: '-20px',
          left: 0,
          pointerEvents: 'none',
          zIndex: 5,
        }}
      >
        <defs>
          <linearGradient id="lineGrad" x1="0%" y1="0%" x2="0%" y2="100%">
            <stop offset="0%" stopColor="#22d3ee" stopOpacity="0" />
            <stop offset="50%" stopColor="#22d3ee" stopOpacity="0.8" />
            <stop offset="100%" stopColor="#22d3ee" stopOpacity="0" />
          </linearGradient>
        </defs>
        <line x1="20%" y1="0" x2="50%" y2="40" stroke="url(#lineGrad)" strokeWidth="1.5"
              className="j-line-anim" />
        <line x1="50%" y1="0" x2="50%" y2="40" stroke="url(#lineGrad)" strokeWidth="1.5"
              className="j-line-anim" />
        <line x1="80%" y1="0" x2="50%" y2="40" stroke="url(#lineGrad)" strokeWidth="1.5"
              className="j-line-anim" />
        <circle cx="50%" cy="20" r="3" fill="#22d3ee">
          <animate attributeName="opacity" values="1;0.3;1" dur="1.5s" repeatCount="indefinite" />
        </circle>
      </svg>
    </div>
  );
}

/* ===== Visual Intelligence Hub ===== */
function VisualHubPanel() {
  return (
    <Panel
      glow
      className="p-8 flex flex-col items-center justify-center min-h-[280px] relative overflow-hidden j-hex-bg"
      label="VIH"
      id="HUB_PRIME"
    >
      <div className="absolute inset-0"
           style={{ background: 'radial-gradient(circle at center, rgba(34, 211, 238, 0.15), transparent 65%)' }} />

      <div className="relative z-10 flex flex-col items-center">
        <div className="relative mb-5">
          <div className="absolute inset-0 rounded-full"
               style={{
                 width: '120px', height: '120px',
                 left: '-28px', top: '-28px',
                 border: '1px solid rgba(34, 211, 238, 0.3)',
                 animation: 'jPulse 3s ease-in-out infinite',
               }} />
          <div className="absolute inset-0 rounded-full"
               style={{
                 width: '95px', height: '95px',
                 left: '-15.5px', top: '-15.5px',
                 border: '1px dashed rgba(233, 30, 99, 0.4)',
                 animation: 'jSpinX 12s linear infinite',
               }} />

          <button
            className="w-16 h-16 rounded-full flex items-center justify-center relative transition-all hover:scale-110 j-pulse-glow"
            style={{
              background: 'radial-gradient(circle at 30% 30%, #67e8f9 0%, #22d3ee 40%, #0891b2 100%)',
              border: '2px solid #67e8f9',
              boxShadow: '0 0 30px rgba(34, 211, 238, 0.7), inset 0 0 15px rgba(255,255,255,0.3)',
            }}
          >
            <svg width="22" height="22" fill="white" viewBox="0 0 24 24"
                 style={{ filter: 'drop-shadow(0 0 4px white)', marginLeft: '3px' }}>
              <path d="M8 5v14l11-7z"/>
            </svg>
          </button>
        </div>

        <h3 className="font-display text-base uppercase tracking-[0.25em] j-glow mb-2"
            style={{ color: 'var(--cyan-bright)' }}>
          Visual Intelligence Hub
        </h3>
        <p className="text-sm text-center max-w-xs" style={{ color: 'var(--text-muted)' }}>
          Images, flowcharts, and mindmaps will materialize here.
        </p>
        <div className="flex items-center gap-2 mt-4 px-4 py-1.5 rounded-full"
             style={{ background: 'rgba(34, 211, 238, 0.08)', border: '1px solid rgba(34, 211, 238, 0.3)' }}>
          <span className="j-dot j-dot-on" />
          <p className="font-mono text-[10px] uppercase tracking-widest"
             style={{ color: 'var(--cyan)' }}>
            Awaiting Data Input
          </p>
        </div>
      </div>

      <div className="absolute bottom-3 left-4 font-mono text-[9px]"
           style={{ color: 'var(--text-dim)' }}>
        VIH://NEURAL_NET_PRIMARY
      </div>
      <div className="absolute bottom-3 right-4 font-mono text-[9px]"
           style={{ color: 'var(--cyan)' }}>
        ◉ READY
      </div>
    </Panel>
  );
}

/* ===== Sphere Panel ===== */
function SpherePanel({ stats }) {
  return (
    <Panel className="p-5 flex-1 flex flex-col items-center justify-center relative overflow-hidden"
           label="CORE" id="ACTIVE">
      <div className="absolute inset-0"
           style={{ background: 'radial-gradient(circle at center, rgba(34, 211, 238, 0.1), transparent 70%)' }} />

      <div className="j-sphere mb-5 mt-4 relative z-10">
        <div className="j-sphere-ring j-sphere-ring-1" />
        <div className="j-sphere-ring j-sphere-ring-2" />
        <div className="j-sphere-ring j-sphere-ring-3" />
        <div className="j-sphere-ring j-sphere-ring-4" />
        <div className="j-sphere-core" />
        <span className="j-sphere-dot" />
        <span className="j-sphere-dot" />
        <span className="j-sphere-dot" />
        <span className="j-sphere-dot" />
      </div>

      <p className="font-display text-xs uppercase tracking-[0.3em] j-glow mb-1 text-center relative z-10"
         style={{ color: 'var(--cyan-bright)' }}>
        System Active
      </p>
      <p className="font-mono text-[10px] text-center mb-4 relative z-10"
         style={{ color: 'var(--text-dim)' }}>
        NEURAL · ONLINE
      </p>

      {/* Hexagonal stats nodes */}
      <div className="flex gap-2 mb-4 relative z-10">
        <div className="j-hex" title="Audio">
          <span className="font-display text-[10px] font-bold" style={{ color: 'var(--cyan-bright)' }}>
            {stats.total_chunks || 0}
          </span>
        </div>
        <div className="j-hex" title="Tasks" style={{ borderColor: 'var(--magenta)' }}>
          <span className="font-display text-[10px] font-bold" style={{ color: '#f472b6' }}>
            {stats.total_tasks || 0}
          </span>
        </div>
      </div>

      <button className="j-btn-magenta px-5 py-2 rounded font-display uppercase tracking-widest text-xs relative z-10">
        Translate
      </button>

      <div className="absolute bottom-2 left-2 right-2 flex justify-between font-mono text-[8px]"
           style={{ color: 'var(--text-dim)' }}>
        <span>FREQ: 8.4Hz</span>
        <span>SIG: 98%</span>
      </div>
    </Panel>
  );
}

/* ===== Transcription / System Log Panel ===== */
function TranscriptionPanel({ data }) {
  const logs = [
    { type: 'sys', text: 'System activated' },
    { type: 'info', text: `${data.total_chunks || 0} audio chunks captured` },
    { type: 'info', text: `${data.total_transcriptions || 0} transcripts processed` },
    { type: 'warn', text: `${data.pending_tasks || 0} tasks awaiting review` },
    { type: 'sys', text: 'Neural pipeline online' },
    { type: 'sys', text: 'Speaker isolation active' },
    { type: 'ok', text: 'All systems nominal' },
    { type: 'ok', text: 'Awaiting next command' },
  ];

  const colorMap = {
    sys: 'var(--cyan-bright)',
    info: 'var(--text)',
    warn: '#fbbf24',
    ok: '#34d399',
  };

  return (
    <Panel className="p-5 flex flex-col min-h-[640px]"
           label="SYS_LOG" id="STREAM_01">
      {/* Header — pushed below corner labels */}
      <div className="flex items-center justify-between mb-3 mt-6">
        <div className="flex items-center gap-2">
          <span className="j-dot j-dot-on" />
          <h3 className="font-display text-sm uppercase tracking-[0.15em] j-glow"
              style={{ color: 'var(--cyan-bright)' }}>
            System Log
          </h3>
        </div>
        <button className="j-btn text-[9px] py-1 px-2">[CHAT]</button>
      </div>

      {/* Sub-header */}
      <div className="flex items-center justify-between mb-3 pb-2 border-b"
           style={{ borderColor: 'var(--border)' }}>
        <p className="font-mono text-[10px] uppercase tracking-widest"
           style={{ color: 'var(--text-muted)' }}>
          ▸ Step Sequence
        </p>
        <div className="flex items-center gap-1.5">
          <span className="font-mono text-[9px]" style={{ color: 'var(--cyan)' }}>LIVE</span>
          <div className="j-spectrum" style={{ height: '10px' }}>
            <div className="j-spectrum-bar" />
            <div className="j-spectrum-bar" />
            <div className="j-spectrum-bar" />
            <div className="j-spectrum-bar" />
          </div>
        </div>
      </div>

      {/* Log entries */}
      <div className="flex-1 overflow-y-auto pr-1 mb-4 space-y-2">
        {logs.map((log, i) => (
          <div key={i} className="flex items-start gap-2 p-2 rounded transition-all hover:bg-cyan-500/5"
               style={{ background: 'rgba(0,0,0,0.25)', border: '1px solid rgba(34, 211, 238, 0.08)' }}>
            <span className="font-mono text-[9px] mt-0.5"
                  style={{ color: 'var(--text-dim)', minWidth: '40px' }}>
              [{String(i + 1).padStart(2, '0')}]
            </span>
            <span className="font-mono text-[10px] leading-relaxed flex-1"
                  style={{ color: colorMap[log.type] }}>
              › {log.text}
            </span>
            <span className={`j-dot ${log.type === 'warn' ? 'j-dot-orange' : log.type === 'ok' ? 'j-dot-green' : 'j-dot-on'}`} />
          </div>
        ))}
      </div>

      {/* Input area */}
      <div className="border-t pt-3" style={{ borderColor: 'var(--border)' }}>
        <div className="relative">
          <span className="absolute left-3 top-1/2 -translate-y-1/2 font-mono text-xs"
                style={{ color: 'var(--cyan)' }}>›</span>
          <input
            type="text"
            placeholder="Type Command..."
            className="j-input text-xs font-mono pl-7"
          />
        </div>
        <div className="flex justify-between items-center mt-2">
          <div className="flex items-center gap-2">
            <span className="j-dot j-dot-on" />
            <span className="font-mono text-[9px]" style={{ color: 'var(--text-dim)' }}>READY</span>
          </div>
          <button className="j-btn-primary text-[9px] py-1 px-3">SEND ⏎</button>
        </div>
      </div>
    </Panel>
  );
}
