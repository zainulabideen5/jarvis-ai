const NAV = [
  { id: 'chat', label: 'Chat', icon: 'M' },
  { id: 'stats', label: 'Dashboard', icon: 'D' },
  { id: 'activity', label: 'Activity', icon: 'A' },
  { id: 'transcriptions', label: 'Transcripts', icon: 'T' },
  { id: 'tasks', label: 'Tasks', icon: 'K' },
  { id: 'meetings', label: 'Meetings', icon: 'V' },
  { id: 'calendar', label: 'Calendar', icon: 'C' },
  { id: 'clients', label: 'Clients', icon: 'U' },
  { id: 'rules', label: 'Rules', icon: 'R' },
  { id: 'chunks', label: 'Chunks', icon: 'H' },
  { id: 'agents', label: 'Agents', icon: 'G' },
  { id: 'settings', label: 'Settings', icon: 'S' },
];

export default function Sidebar({ activePage, onNavigate, unreadChat = 0 }) {
  return (
    <aside
      className="w-52 flex flex-col border-r relative"
      style={{
        background: 'linear-gradient(180deg, rgba(10,13,24,0.95) 0%, rgba(6,8,16,0.98) 100%)',
        borderColor: 'var(--border)',
        backdropFilter: 'blur(14px)',
      }}
    >
      {/* Header tag */}
      <div className="px-5 pt-5 pb-3">
        <span
          className="text-[9px] tracking-[0.3em] uppercase font-display"
          style={{ color: 'var(--text-dim)' }}
        >
          ── Modules ──
        </span>
      </div>

      {/* Nav */}
      <nav className="flex-1 overflow-y-auto pb-3">
        {NAV.map((item) => {
          const active = activePage === item.id;
          return (
            <button
              key={item.id}
              onClick={() => onNavigate(item.id)}
              className="w-full flex items-center gap-3 px-5 py-2.5 text-sm relative group"
              style={{
                background: active
                  ? 'linear-gradient(90deg, rgba(34,211,238,0.15), transparent)'
                  : 'transparent',
                color: active ? 'var(--cyan-bright)' : 'var(--text-muted)',
                fontFamily: "'Rajdhani', sans-serif",
                fontWeight: active ? 600 : 500,
                letterSpacing: '0.06em',
                fontSize: '13px',
                textTransform: 'uppercase',
                borderLeft: active ? '2px solid var(--cyan)' : '2px solid transparent',
                transition: 'all 0.15s ease',
              }}
              onMouseEnter={(e) => {
                if (!active) {
                  e.currentTarget.style.background = 'rgba(34, 211, 238, 0.06)';
                  e.currentTarget.style.color = 'var(--text)';
                }
              }}
              onMouseLeave={(e) => {
                if (!active) {
                  e.currentTarget.style.background = 'transparent';
                  e.currentTarget.style.color = 'var(--text-muted)';
                }
              }}
            >
              <span
                className="w-6 h-6 rounded flex items-center justify-center font-display text-[11px] font-bold"
                style={{
                  background: active ? 'rgba(34, 211, 238, 0.2)' : 'rgba(34, 211, 238, 0.05)',
                  border: active ? '1px solid rgba(34, 211, 238, 0.5)' : '1px solid rgba(34, 211, 238, 0.15)',
                  color: active ? 'var(--cyan)' : 'var(--text-dim)',
                  boxShadow: active ? '0 0 8px rgba(34, 211, 238, 0.3)' : 'none',
                }}
              >
                {item.icon}
              </span>

              <span className="flex-1 text-left">{item.label}</span>

              {item.id === 'chat' && unreadChat > 0 && (
                <span
                  className="text-[9px] font-bold px-1.5 py-0.5 rounded-full font-display"
                  style={{
                    background: '#e91e63',
                    color: '#fff',
                    boxShadow: '0 0 10px rgba(233, 30, 99, 0.6)',
                    animation: 'jPulse 1.5s ease-in-out infinite',
                  }}
                >
                  {unreadChat}
                </span>
              )}
            </button>
          );
        })}
      </nav>

      {/* Footer */}
      <div className="px-5 py-3 border-t" style={{ borderColor: 'var(--border)' }}>
        <div className="flex items-center justify-between text-[10px] uppercase tracking-widest font-display">
          <div className="flex items-center gap-2">
            <span className="j-dot j-dot-on" />
            <span style={{ color: 'var(--text-muted)' }}>Online</span>
          </div>
          <span style={{ color: 'var(--text-dim)' }}>v0.1.0</span>
        </div>
      </div>
    </aside>
  );
}
