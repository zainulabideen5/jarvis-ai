import { useState, useEffect, createContext, useContext, useCallback, useRef } from 'react';

const ToastContext = createContext(null);

// Cap the number of toasts visible at once. Older ones are dropped so the
// stack doesn't grow without bound during error storms.
const MAX_TOASTS = 5;

export function useToast() {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error('useToast must be used inside ToastProvider');
  return ctx;
}

export function ToastProvider({ children }) {
  const [toasts, setToasts] = useState([]);
  // Track outstanding dismiss timers so we can clear them on unmount —
  // previously a `setTimeout(dismiss, …)` could fire after unmount and try
  // to update state on a dead component.
  const timersRef = useRef(new Map());

  const dismiss = useCallback((id) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
    const timer = timersRef.current.get(id);
    if (timer) {
      clearTimeout(timer);
      timersRef.current.delete(id);
    }
  }, []);

  const show = useCallback((message, type = 'info', duration = 3500) => {
    const id = `t_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`;
    setToasts((prev) => {
      const next = [...prev, { id, message, type }];
      // Trim oldest if over cap (and clear its timer)
      while (next.length > MAX_TOASTS) {
        const oldest = next.shift();
        const t = timersRef.current.get(oldest.id);
        if (t) {
          clearTimeout(t);
          timersRef.current.delete(oldest.id);
        }
      }
      return next;
    });
    if (duration > 0) {
      const timer = setTimeout(() => dismiss(id), duration);
      timersRef.current.set(id, timer);
    }
    return id;
  }, [dismiss]);

  useEffect(() => {
    // Clear all pending dismiss timers on provider unmount
    return () => {
      timersRef.current.forEach((t) => clearTimeout(t));
      timersRef.current.clear();
    };
  }, []);

  const api = {
    success: (msg, dur) => show(msg, 'success', dur),
    error: (msg, dur) => show(msg, 'error', dur),
    warn: (msg, dur) => show(msg, 'warn', dur),
    info: (msg, dur) => show(msg, 'info', dur),
    dismiss,
  };

  return (
    <ToastContext.Provider value={api}>
      {children}
      <ToastContainer toasts={toasts} onDismiss={dismiss} />
    </ToastContext.Provider>
  );
}

function ToastContainer({ toasts, onDismiss }) {
  return (
    <div
      className="fixed top-20 right-6 z-[200] flex flex-col gap-3 pointer-events-none"
      style={{ maxWidth: '420px' }}
    >
      {toasts.map((t) => (
        <ToastItem key={t.id} toast={t} onDismiss={() => onDismiss(t.id)} />
      ))}
    </div>
  );
}

function ToastItem({ toast, onDismiss }) {
  const [show, setShow] = useState(false);

  useEffect(() => {
    requestAnimationFrame(() => setShow(true));
  }, []);

  const styles = {
    success: {
      border: '1px solid var(--cyan)',
      glow: 'rgba(34, 211, 238, 0.5)',
      icon: '✓',
      iconBg: 'linear-gradient(135deg, #22d3ee, #0891b2)',
      iconColor: '#001018',
      label: 'SUCCESS',
      labelColor: 'var(--cyan-bright)',
    },
    error: {
      border: '1px solid #f87171',
      glow: 'rgba(239, 68, 68, 0.5)',
      icon: '✕',
      iconBg: 'linear-gradient(135deg, #ef4444, #b91c1c)',
      iconColor: '#fff',
      label: 'ERROR',
      labelColor: '#fca5a5',
    },
    warn: {
      border: '1px solid #fbbf24',
      glow: 'rgba(251, 191, 36, 0.4)',
      icon: '!',
      iconBg: 'linear-gradient(135deg, #fbbf24, #d97706)',
      iconColor: '#1a0d00',
      label: 'WARNING',
      labelColor: '#fde68a',
    },
    info: {
      border: '1px solid var(--border-bright)',
      glow: 'rgba(34, 211, 238, 0.3)',
      icon: 'i',
      iconBg: 'rgba(34, 211, 238, 0.15)',
      iconColor: 'var(--cyan-bright)',
      label: 'INFO',
      labelColor: 'var(--text-muted)',
    },
  };
  const s = styles[toast.type] || styles.info;

  return (
    <div
      className="pointer-events-auto"
      style={{
        background: 'rgba(15, 22, 38, 0.95)',
        backdropFilter: 'blur(14px) saturate(140%)',
        border: s.border,
        borderRadius: '8px',
        boxShadow: `0 0 25px ${s.glow}, 0 4px 20px rgba(0,0,0,0.4)`,
        padding: '12px 14px',
        minWidth: '320px',
        transform: show ? 'translateX(0)' : 'translateX(120%)',
        opacity: show ? 1 : 0,
        transition: 'all 0.35s cubic-bezier(0.34, 1.56, 0.64, 1)',
        position: 'relative',
        overflow: 'hidden',
      }}
    >
      {/* Top accent line */}
      <div
        style={{
          position: 'absolute',
          top: 0,
          left: 0,
          right: 0,
          height: '2px',
          background: `linear-gradient(90deg, transparent, ${s.glow}, transparent)`,
        }}
      />

      <div className="flex items-start gap-3">
        {/* Icon */}
        <div
          className="w-8 h-8 rounded-full flex items-center justify-center flex-shrink-0 font-display font-bold"
          style={{
            background: s.iconBg,
            color: s.iconColor,
            boxShadow: `0 0 12px ${s.glow}`,
          }}
        >
          {s.icon}
        </div>

        {/* Content */}
        <div className="flex-1 min-w-0">
          <div className="flex items-center justify-between mb-0.5">
            <span
              className="font-display text-[9px] tracking-[0.25em] uppercase font-bold"
              style={{ color: s.labelColor }}
            >
              {s.label}
            </span>
            <button
              onClick={onDismiss}
              className="text-xs font-mono opacity-60 hover:opacity-100 transition"
              style={{ color: 'var(--text-muted)' }}
            >
              ✕
            </button>
          </div>
          <p className="text-sm leading-snug" style={{ color: 'var(--text)' }}>
            {toast.message}
          </p>
        </div>
      </div>
    </div>
  );
}
