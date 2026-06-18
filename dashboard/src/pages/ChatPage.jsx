import { useState, useRef, useEffect, memo } from 'react';
import { api } from '../api';

export default function ChatPage() {
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(false);
  const [voiceListening, setVoiceListening] = useState(false);
  const [historyLoaded, setHistoryLoaded] = useState(false);
  const [jarvisListening, setJarvisListening] = useState(false);
  const [activeMeeting, setActiveMeeting] = useState(null);
  // Files the user attached but hasn't sent yet. Each entry is
  // { file: File, status: 'pending'|'uploading'|'uploaded'|'failed', path?, error? }
  const [attachments, setAttachments] = useState([]);
  const [uploading, setUploading] = useState(false);
  // Live engine progress line ("Screen parh raha hun…" etc.) shown while a
  // multi-step task runs, so the wait doesn't feel frozen.
  const [progressLine, setProgressLine] = useState('');
  // When JARVIS asks a clarifying question with choices, we show a persistent
  // option-box (clickable buttons, Claude-style). Kept OUTSIDE `messages` so the
  // 5s history poll can't wipe it. Cleared when the user picks or sends anything.
  const [pendingQuestion, setPendingQuestion] = useState(null);
  const [showOtherInput, setShowOtherInput] = useState(false);  // "Other" → inline text field
  const [otherText, setOtherText] = useState('');
  // Editing a pending message draft (any app) before sending. Tracks which
  // pending token is open in the editor + the editable text.
  const [editingToken, setEditingToken] = useState(null);
  const [editText, setEditText] = useState('');
  // Image lightbox — chat ke andar full image (X se band), Claude jaisa
  const [lightboxUrl, setLightboxUrl] = useState(null);
  // Email draft edit — kaunsi message ka draft edit ho raha + uska text
  const [emailEditMsg, setEmailEditMsg] = useState(null);
  const [emailEditText, setEmailEditText] = useState('');
  const endRef = useRef(null);
  const messagesRef = useRef(null);     // scrollable container
  const prevCountRef = useRef(0);       // detect when a NEW message is added
  const recognitionRef = useRef(null);
  const fileInputRef = useRef(null);
  const inputRef = useRef(null);   // text input — "Other" option pe focus karne ke liye
  // Mirror `loading` into a ref so the history poller (a stable-closure
  // interval) can SKIP overwriting messages while a send is in flight —
  // otherwise the 5s poll wiped optimistic bubbles + confirm buttons mid-send.
  const loadingRef = useRef(false);
  useEffect(() => { loadingRef.current = loading; }, [loading]);

  // Esc se image lightbox band ho
  useEffect(() => {
    if (!lightboxUrl) return;
    const onKey = (e) => { if (e.key === 'Escape') setLightboxUrl(null); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [lightboxUrl]);

  // While a request is in flight, poll the engine's live progress so the
  // user sees what it's doing ("Likh raha hun…") instead of a frozen spinner.
  useEffect(() => {
    if (!loading) {
      setProgressLine('');
      return;
    }
    let alive = true;
    const tick = async () => {
      try {
        const p = await api.engineProgress();
        if (alive && p && p.active && p.line) setProgressLine(p.line);
      } catch {
        /* ignore — progress is best-effort */
      }
    };
    tick();
    const id = setInterval(tick, 1200);
    return () => { alive = false; clearInterval(id); };
  }, [loading]);

  // Poll listening + meeting state
  useEffect(() => {
    const check = () => {
      api.getListening().then((d) => setJarvisListening(d.listening === 'on')).catch(() => {});
      api.getActiveMeeting().then((m) => setActiveMeeting(m.id ? m : null)).catch(() => {});
    };
    check();
    const id = setInterval(check, 3000);
    return () => clearInterval(id);
  }, []);

  // JARVIS khud user ki PRECISE location jaan le. Laptop pe pehla reading aksar
  // coarse (IP-level) hota hai; watchPosition se kuch readings le kar sabse
  // precise wala (sabse chhota accuracy radius) bhejte hain — single cached
  // reading se behtar. Deny/unavailable ho to IP-based city fallback chalta hai.
  useEffect(() => {
    if (!navigator.geolocation) return;
    let best = null, sent = false;
    const flush = () => {
      if (sent || !best) return;
      sent = true;
      api.setGpsLocation(
        best.coords.latitude, best.coords.longitude, best.coords.accuracy
      ).catch(() => {});
    };
    const id = navigator.geolocation.watchPosition(
      (pos) => {
        if (!best || pos.coords.accuracy < best.coords.accuracy) best = pos;
        if (pos.coords.accuracy <= 100) { flush(); navigator.geolocation.clearWatch(id); }
      },
      () => { /* denied/unavailable — IP fallback handles it */ },
      { enableHighAccuracy: true, timeout: 15000, maximumAge: 0 }
    );
    // 15s baad jo best mila woh bhej do aur watch band kar do.
    const t = setTimeout(() => { flush(); navigator.geolocation.clearWatch(id); }, 15000);
    return () => { clearTimeout(t); navigator.geolocation.clearWatch(id); };
  }, []);

  const toggleJarvisListening = async () => {
    try {
      if (jarvisListening) {
        await api.stopListening();
        setJarvisListening(false);
      } else {
        await api.startListening();
        setJarvisListening(true);
      }
    } catch (err) {
      alert(`Failed: ${err.message}`);
    }
  };

  const handleStartMeeting = async () => {
    const title = prompt('Meeting ka naam daal (optional):') || '';
    try {
      await api.startMeeting(title);
      setJarvisListening(true);
    } catch (err) {
      alert(`Failed: ${err.message}`);
    }
  };

  const handleEndMeeting = async () => {
    if (!confirm('Meeting khatam karna hai? Summary generate hoga.')) return;
    try {
      await api.endMeeting();
      setActiveMeeting(null);
      setJarvisListening(false);
    } catch (err) {
      alert(`Failed: ${err.message}`);
    }
  };

  // Load chat history from DB on mount + poll for new messages every 5s
  useEffect(() => {
    const loadHistory = () => {
      // Don't clobber in-flight optimistic messages / confirm buttons.
      if (loadingRef.current && historyLoaded) return;
      api.getChatHistory().then((history) => {
        if (history.length > 0) {
          setMessages((prev) => {
            // Active confirm-buttons (pending) wali message DB mein nahi hoti —
            // poll se usko mat mitao warna user click hi nahi kar payega.
            const lastPrev = prev[prev.length - 1];
            if (lastPrev?.pending?.length > 0) return prev;
            // Email draft ke Send/Edit/Cancel buttons bhi poll se na mit-en
            if (lastPrev?.actions?.some?.((a) => a.action === 'email_draft' && a.status === 'awaiting_confirm')) return prev;
            const next = history.map((m) => {
              const msg = { role: m.role, content: m.content, actions: m.actions || [] };
              // User message ke attachments (server URL) actions mein save hain →
              // chat mein image/file render karne ke liye nikaal lo.
              const attEntry = (m.actions || []).find((a) => Array.isArray(a?.attachments));
              if (attEntry) msg.attachments_preview = attEntry.attachments;
              return msg;
            });
            // Kuch badla nahi? to wahi array rakho — bewajah re-render/scroll na ho.
            const same = prev.length === next.length
              && prev[prev.length - 1]?.content === next[next.length - 1]?.content
              && prev[prev.length - 1]?.role === next[next.length - 1]?.role;
            return same ? prev : next;
          });
        } else if (!historyLoaded) {
          setMessages([{ role: 'assistant', content: 'Salam boss! Main aap ka AI assistant hun. Kya karna hai? Bol ya type kar.', actions: [] }]);
        }
        setHistoryLoaded(true);
      }).catch(() => {
        if (!historyLoaded) {
          setMessages([{ role: 'assistant', content: 'Salam boss! Main aap ka AI assistant hun. Kya karna hai? Bol ya type kar.', actions: [] }]);
          setHistoryLoaded(true);
        }
      });
    };
    loadHistory();
    const id = setInterval(loadHistory, 5000);
    return () => clearInterval(id);
  }, [historyLoaded]);

  // Auto-scroll ONLY when it makes sense — warna har 5s history-poll user ko
  // wapas neeche khींch leti thi (ooper padhna mumkin nahi tha).
  useEffect(() => {
    const c = messagesRef.current;
    const grew = messages.length > prevCountRef.current;
    prevCountRef.current = messages.length;
    const last = messages[messages.length - 1];
    const nearBottom = c
      ? c.scrollHeight - c.scrollTop - c.clientHeight < 160
      : true;
    // neeche tab jao jab: user pehle se neeche ho, YA usne abhi khud message bheja
    if (nearBottom || (grew && last?.role === 'user')) {
      endRef.current?.scrollIntoView({ behavior: 'smooth' });
    }
  }, [messages]);

  // Refresh / first load → seedha LATEST (neeche) pe jao, beech mein nahi.
  useEffect(() => {
    if (!historyLoaded) return;
    const t = setTimeout(() => endRef.current?.scrollIntoView({ behavior: 'auto' }), 80);
    return () => clearTimeout(t);
  }, [historyLoaded]);

  const handleAttachClick = () => {
    fileInputRef.current?.click();
  };

  const handleFilesSelected = (e) => {
    const files = Array.from(e.target.files || []);
    if (!files.length) return;
    // 50 MB cap matches server side
    const MAX = 50 * 1024 * 1024;
    const ok = files.filter((f) => f.size <= MAX);
    if (ok.length < files.length) {
      alert(`Kuch file 50 MB se zyada thi, skip kar di.`);
    }
    setAttachments((prev) => [...prev, ...ok.map((f) => ({ file: f, status: 'pending' }))]);
    e.target.value = '';
  };

  const removeAttachment = (idx) => {
    setAttachments((prev) => prev.filter((_, i) => i !== idx));
  };

  // Screenshot / image paste — Ctrl+V clipboard se image seedha chat mein.
  const handlePaste = (e) => {
    const items = e.clipboardData?.items;
    if (!items) return;
    const imgs = [];
    for (const it of items) {
      if (it.kind === 'file' && (it.type || '').startsWith('image/')) {
        const blob = it.getAsFile();
        if (blob) {
          const ext = (blob.type.split('/')[1] || 'png').replace('jpeg', 'jpg');
          const named = new File([blob], blob.name || `screenshot-${Date.now()}.${ext}`, { type: blob.type });
          imgs.push({ file: named, status: 'pending' });
        }
      }
    }
    if (imgs.length) {
      e.preventDefault();   // raw blob text ko input mein paste hone se roko
      setAttachments((prev) => [...prev, ...imgs]);
    }
  };

  const sendMessage = async (text, confirmToken = null) => {
    const hasAttachments = attachments.length > 0;
    if (!text.trim() && !hasAttachments) return;

    // Show user's bubble with attachment chips
    const displayText = text.trim() || (hasAttachments ? '(file bhej raha hoon)' : '');
    const userMsg = {
      role: 'user',
      content: displayText,
      // Rich preview so the chat SHOWS images inline + file chips for the rest.
      // Local object URL = instant preview (no server round-trip needed).
      attachments_preview: attachments.map((a) => ({
        name: a.file.name,
        type: a.file.type || '',
        url: URL.createObjectURL(a.file),
      })),
    };
    setMessages((prev) => [...prev, userMsg]);
    setInput('');
    setLoading(true);
    setPendingQuestion(null);   // user ne kuch bheja → purana question-box hatao
    setShowOtherInput(false);

    // Upload attached files first to get server-side paths
    let serverPaths = null;
    if (hasAttachments) {
      setUploading(true);
      try {
        const uploaded = [];
        for (const a of attachments) {
          const res = await api.uploadChatFile(a.file);
          uploaded.push(res.path);
        }
        serverPaths = uploaded;
      } catch (err) {
        setMessages((prev) => [
          ...prev,
          { role: 'assistant', content: `File upload fail: ${err.message}`, actions: [] },
        ]);
        setLoading(false);
        setUploading(false);
        return;
      } finally {
        setUploading(false);
      }
      setAttachments([]); // clear chips on successful upload
    }

    try {
      const result = await api.sendChat(text, confirmToken, serverPaths);
      setMessages((prev) => [
        ...prev,
        {
          role: 'assistant',
          content: result.reply,
          actions: result.actions || [],
          pending: result.pending || [],
        },
      ]);
      // JARVIS ne choices ke saath sawal pucha → clickable option-box dikhao
      if (Array.isArray(result.options) && result.options.length > 0) {
        setPendingQuestion({ question: result.reply, options: result.options });
      }
    } catch (err) {
      setMessages((prev) => [
        ...prev,
        { role: 'assistant', content: `Error: ${err.message}`, actions: [] },
      ]);
    } finally {
      setLoading(false);
    }
  };

  const handleConfirm = async (token, decision) => {
    setLoading(true);
    try {
      const result = await api.laptopConfirm(token, decision);
      setMessages((prev) => [
        ...prev,
        { role: 'user', content: decision === 'yes' ? '✅ Confirm' : '❌ Cancel' },
        { role: 'assistant', content: result.reply, actions: result.actions || [] },
      ]);
    } catch (err) {
      setMessages((prev) => [...prev, { role: 'assistant', content: `Error: ${err.message}` }]);
    } finally {
      setLoading(false);
    }
  };

  // ✏️ Edit — send the edited draft to the backend, which updates the pending
  // message and returns a fresh draft (with Confirm/Edit/Cancel buttons again).
  const handleEditSave = async (token) => {
    const text = editText.trim();
    if (!text) return;
    setEditingToken(null);
    setLoading(true);
    try {
      const result = await api.laptopEdit(token, text);
      setMessages((prev) => [
        ...prev,
        { role: 'user', content: `✏️ Edit: ${text}` },
        {
          role: 'assistant',
          content: result.reply,
          actions: result.actions || [],
          pending: result.pending || [],
        },
      ]);
    } catch (err) {
      setMessages((prev) => [...prev, { role: 'assistant', content: `Error: ${err.message}` }]);
    } finally {
      setLoading(false);
    }
  };

  const handleStop = async () => {
    try {
      await api.stopTask();
      setMessages((prev) => [
        ...prev,
        { role: 'assistant', content: '🛑 Stop bhej diya — task agle step pe ruk jayega.' },
      ]);
    } catch (err) {
      setMessages((prev) => [...prev, { role: 'assistant', content: `Stop error: ${err.message}` }]);
    } finally {
      // Unblock the UI immediately — otherwise the input stays disabled + the
      // spinner keeps spinning until the (now-stopping) backend call returns.
      setLoading(false);
      setProgressLine('');
    }
  };

  const handleVerificationAction = async (action, label) => {
    setLoading(true);
    try {
      const result = await api.verificationAction(action);
      setMessages((prev) => [
        ...prev,
        { role: 'user', content: label || action },
        {
          role: 'assistant',
          content: result.reply,
          actions: result.verification ? [{ verification: result.verification }] : [],
        },
      ]);
    } catch (err) {
      setMessages((prev) => [
        ...prev,
        { role: 'assistant', content: `Error: ${err.message}` },
      ]);
    } finally {
      setLoading(false);
    }
  };

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage(input);
    }
  };

  // Voice input using Web Speech API
  const toggleVoice = () => {
    if (voiceListening) {
      recognitionRef.current?.stop();
      setVoiceListening(false);
      return;
    }

    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SpeechRecognition) {
      alert('Browser mein Speech Recognition support nahi hai. Chrome use karo.');
      return;
    }

    const recognition = new SpeechRecognition();
    recognition.lang = 'ur-PK';
    recognition.interimResults = false;
    recognition.continuous = false;

    recognition.onresult = (event) => {
      const text = event.results[0][0].transcript;
      setInput(text);
      sendMessage(text);
      setVoiceListening(false);
    };

    recognition.onerror = () => setVoiceListening(false);
    recognition.onend = () => setVoiceListening(false);

    recognitionRef.current = recognition;
    recognition.start();
    setVoiceListening(true);
  };

  return (
    <div
      className="flex flex-col"
      style={{ height: 'calc(100vh - 104px)' }}
    >
      {/* Control Bar — fixed at top */}
      <div
        className="flex items-center justify-between mb-4 py-3 px-4 rounded-lg j-panel j-panel-bracket flex-shrink-0"
        style={{
          background: 'rgba(10, 13, 24, 0.92)',
          backdropFilter: 'blur(16px)',
          border: '1px solid var(--border)',
          boxShadow: '0 4px 20px rgba(0, 0, 0, 0.5)',
        }}
      >
        <h2 className="font-display text-xl uppercase tracking-[0.15em] j-glow"
            style={{ color: 'var(--cyan-bright)' }}>
          ◉ AI Assistant
        </h2>
        <div className="flex items-center gap-3">
          {activeMeeting ? (
            <button
              onClick={handleEndMeeting}
              className="px-5 py-2.5 rounded font-display uppercase tracking-widest text-xs font-bold transition-all"
              style={{
                background: 'linear-gradient(135deg, #ef4444, #b91c1c)',
                color: '#fff',
                border: '1px solid #f87171',
                boxShadow: '0 0 20px rgba(239, 68, 68, 0.6)',
                animation: 'jPulse 1.5s ease-in-out infinite',
              }}
            >
              ◼ End Meeting
            </button>
          ) : (
            <button
              onClick={handleStartMeeting}
              className="j-btn-magenta px-5 py-2.5 rounded font-display uppercase tracking-widest text-xs font-bold"
            >
              ▶ Start Meeting
            </button>
          )}
          <button
            onClick={toggleJarvisListening}
            className="px-5 py-2.5 rounded font-display uppercase tracking-widest text-xs font-bold transition-all"
            style={
              jarvisListening
                ? {
                    background: 'linear-gradient(135deg, #ef4444, #b91c1c)',
                    color: '#fff',
                    border: '1px solid #f87171',
                    boxShadow: '0 0 18px rgba(239, 68, 68, 0.5)',
                  }
                : {
                    background: 'linear-gradient(135deg, #22d3ee, #0891b2)',
                    color: '#001018',
                    border: '1px solid #67e8f9',
                    boxShadow: '0 0 18px rgba(34, 211, 238, 0.5)',
                  }
            }
          >
            {jarvisListening ? '⏸ Stop Listening' : '🎤 Start Listening'}
          </button>
        </div>
      </div>

      {/* Meeting Status */}
      {activeMeeting && (
        <div className="flex items-center justify-between mb-3 px-4 py-2 bg-purple-900/20 border border-purple-800/50 rounded-xl">
          <div className="flex items-center gap-2">
            <span className="w-3 h-3 rounded-full bg-purple-500 animate-pulse"></span>
            <span className="text-sm text-purple-400">Meeting: {activeMeeting.title}</span>
          </div>
          <span className="text-xs text-purple-400">{activeMeeting.duration_minutes} min</span>
        </div>
      )}

      {/* Listening Status */}
      {jarvisListening && !activeMeeting && (
        <div className="flex items-center gap-2 mb-3 px-4 py-2 bg-green-900/20 border border-green-800/50 rounded-xl">
          <span className="w-3 h-3 rounded-full bg-green-500 animate-pulse"></span>
          <span className="text-sm text-green-400">Sun raha hun — call/meeting shuru karo</span>
        </div>
      )}

      {/* Messages */}
      <div ref={messagesRef} className="flex-1 overflow-y-auto mb-4 space-y-3">
        {messages.map((msg, i) => (
          <div key={msg.id ?? `${msg.role}-${msg.created_at ?? ''}-${i}`} className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}>
            <div className={`max-w-[75%] rounded-xl px-4 py-3 ${
              msg.role === 'user'
                ? 'bg-blue-600 text-white'
                : 'bg-gray-900 border border-gray-800 text-gray-200'
            }`}>
              {msg.role === 'assistant'
                ? <MarkdownText text={msg.content} />
                : <p className="text-sm whitespace-pre-wrap">{msg.content}</p>}

              {/* Attachments on user's own message — images show inline, baaki chip */}
              {msg.role === 'user' && msg.attachments_preview?.length > 0 && (
                <div className="mt-2 flex flex-wrap gap-2">
                  {msg.attachments_preview.map((att, k) => {
                    // Backward-compat: purani history mein att string (sirf naam) tha.
                    const a = typeof att === 'string' ? { name: att, type: '', url: '' } : att;
                    const isImg = (a.type || '').startsWith('image/') ||
                      /\.(png|jpe?g|jpe|jfif|jff|jif|gif|webp|bmp|tiff?|svg|ico|heic|heif|avif)$/i.test(a.name || '');
                    if (isImg && a.url) {
                      return (
                        <img
                          key={k}
                          src={a.url}
                          alt={a.name}
                          title={a.name}
                          onClick={() => setLightboxUrl(a.url)}
                          className="max-h-48 max-w-[220px] rounded-lg border border-blue-400/40 object-cover cursor-zoom-in"
                        />
                      );
                    }
                    const isPdf = /\.pdf$/i.test(a.name || '') || a.type === 'application/pdf';
                    return (
                      <a
                        key={k}
                        href={a.url || undefined}
                        target="_blank"
                        rel="noreferrer"
                        className="text-[11px] px-2 py-1 rounded bg-blue-500/30 border border-blue-400/40 hover:bg-blue-500/40"
                        title={a.name}
                      >
                        {isPdf ? '📄' : '📎'} {a.name}
                      </a>
                    );
                  })}
                </div>
              )}

              {/* Action badges */}
              {msg.actions?.length > 0 && (
                <div className="mt-2 space-y-1">
                  {msg.actions.map((a, j) => (
                    <div key={j} className={`text-xs px-2 py-1 rounded ${
                      a.status === 'success' ? 'bg-green-900/50 text-green-400'
                      : a.status === 'blocked' ? 'bg-red-900/50 text-red-400'
                      : a.status === 'failed' ? 'bg-orange-900/50 text-orange-400'
                      : 'bg-gray-800 text-gray-400'
                    }`}>
                      {a.type === 'task_created' && `Task created: ${a.title}`}
                      {a.type === 'task_assigned' && `Task "${a.title}" → ${a.assigned_to} ko assign ki`}
                      {a.type === 'client_created' && `Client added: ${a.name}`}
                      {a.action && a.action !== 'email_draft' && `${a.action}: ${a.status}`}
                    </div>
                  ))}
                </div>
              )}

              {/* Email draft — Send / Edit / Cancel buttons (professional) */}
              {msg.actions?.some?.((a) => a.action === 'email_draft' && a.status === 'awaiting_confirm') && (
                (() => {
                  const ed = msg.actions.find((a) => a.action === 'email_draft');
                  if (emailEditMsg === i) {
                    return (
                      <div className="mt-3 flex flex-col gap-2">
                        <textarea
                          value={emailEditText}
                          onChange={(e) => setEmailEditText(e.target.value)}
                          rows={5}
                          autoFocus
                          className="w-full px-3 py-2 text-sm bg-gray-900 border border-gray-600 text-white rounded resize-y"
                          placeholder="Email ka message edit karo…"
                        />
                        <div className="flex gap-2">
                          <button
                            onClick={() => { const t = emailEditText.trim(); if (t) { setEmailEditMsg(null); sendMessage(t); } }}
                            className="px-3 py-1 text-xs bg-green-600 hover:bg-green-500 text-white rounded"
                          >
                            💾 Save
                          </button>
                          <button
                            onClick={() => setEmailEditMsg(null)}
                            className="px-3 py-1 text-xs bg-gray-600 hover:bg-gray-500 text-white rounded"
                          >
                            ↩️ Wapas
                          </button>
                        </div>
                      </div>
                    );
                  }
                  return (
                    <div className="mt-3 flex gap-2">
                      <button
                        onClick={() => sendMessage('haan, bhej do')}
                        className="px-3 py-1 text-xs bg-green-600 hover:bg-green-500 text-white rounded"
                      >
                        ✅ Bhej do
                      </button>
                      <button
                        onClick={() => { setEmailEditMsg(i); setEmailEditText(ed?.body || ''); }}
                        className="px-3 py-1 text-xs bg-blue-600 hover:bg-blue-500 text-white rounded"
                      >
                        ✏️ Edit
                      </button>
                      <button
                        onClick={() => sendMessage('cancel')}
                        className="px-3 py-1 text-xs bg-red-600 hover:bg-red-500 text-white rounded"
                      >
                        ❌ Cancel
                      </button>
                    </div>
                  );
                })()
              )}

              {/* Pending confirmation buttons (+ Edit for message drafts, any app) */}
              {msg.pending?.length > 0 && (
                <div className="mt-3 flex flex-col gap-2">
                  {msg.pending.map((p, j) => (
                    editingToken === p.token ? (
                      // ✏️ Inline editor — message badlo phir Save (re-confirm)
                      <div key={j} className="flex flex-col gap-2">
                        <textarea
                          value={editText}
                          onChange={(e) => setEditText(e.target.value)}
                          onKeyDown={(e) => {
                            if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleEditSave(p.token); }
                            if (e.key === 'Escape') setEditingToken(null);
                          }}
                          rows={2}
                          autoFocus
                          className="w-full px-3 py-2 text-sm bg-gray-900 border border-gray-600 text-white rounded resize-y"
                          placeholder="Message edit karo…"
                        />
                        <div className="flex gap-2">
                          <button
                            onClick={() => handleEditSave(p.token)}
                            className="px-3 py-1 text-xs bg-green-600 hover:bg-green-500 text-white rounded"
                          >
                            💾 Save
                          </button>
                          <button
                            onClick={() => setEditingToken(null)}
                            className="px-3 py-1 text-xs bg-gray-600 hover:bg-gray-500 text-white rounded"
                          >
                            ↩️ Wapas
                          </button>
                        </div>
                      </div>
                    ) : (
                      <div key={j} className="flex gap-2">
                        <button
                          onClick={() => handleConfirm(p.token, 'yes')}
                          className="px-3 py-1 text-xs bg-green-600 hover:bg-green-500 text-white rounded"
                        >
                          ✅ Bhej do
                        </button>
                        {p.editable && (
                          <button
                            onClick={() => { setEditingToken(p.token); setEditText(p.message || ''); }}
                            className="px-3 py-1 text-xs bg-blue-600 hover:bg-blue-500 text-white rounded"
                          >
                            ✏️ Edit
                          </button>
                        )}
                        <button
                          onClick={() => handleConfirm(p.token, 'no')}
                          className="px-3 py-1 text-xs bg-red-600 hover:bg-red-500 text-white rounded"
                        >
                          ❌ Cancel
                        </button>
                      </div>
                    )
                  ))}
                </div>
              )}

              {/* Verification flow buttons */}
              {msg.actions?.some?.((a) => a.verification) && (
                <VerificationButtons
                  verification={msg.actions.find((a) => a.verification).verification}
                  onAction={handleVerificationAction}
                />
              )}
            </div>
          </div>
        ))}

        {loading && (
          <div className="flex justify-start">
            <div className="bg-gray-900 border border-gray-800 rounded-xl px-4 py-3 flex items-center gap-3">
              <p className="text-sm text-cyan-400 flex items-center gap-2">
                <span className="inline-block w-2 h-2 rounded-full bg-cyan-400 animate-pulse"></span>
                {progressLine || 'Soch raha hun...'}
              </p>
              <button
                onClick={handleStop}
                className="px-3 py-1 rounded-lg text-xs font-medium bg-red-600 hover:bg-red-500 text-white transition-colors"
                title="Chalti hui task rok do"
              >
                🛑 Stop
              </button>
            </div>
          </div>
        )}

        <div ref={endRef} />
      </div>

      {/* Question-box — JARVIS ka clarifying sawal + clickable options (Claude-style) */}
      {pendingQuestion && !loading && (
        <div
          className="mb-3 px-4 py-3 rounded-xl"
          style={{ background: 'rgba(34, 211, 238, 0.07)', border: '1px solid var(--border)' }}
        >
          <p className="text-xs uppercase tracking-widest mb-2" style={{ color: 'var(--text-dim)' }}>
            🤔 Chuno{showOtherInput ? ' — apna jawab likho:' : ' ya neeche type karo:'}
          </p>
          {!showOtherInput ? (
            <div className="flex flex-wrap gap-2">
              {pendingQuestion.options.map((opt, i) => (
                <button
                  key={`${opt}-${i}`}
                  onClick={() => { setPendingQuestion(null); sendMessage(opt); }}
                  className="px-4 py-2 rounded-lg text-sm font-medium transition-all"
                  style={{
                    background: 'linear-gradient(135deg, #22d3ee, #0891b2)',
                    color: '#001018',
                    border: '1px solid #67e8f9',
                    boxShadow: '0 0 12px rgba(34, 211, 238, 0.35)',
                  }}
                  onMouseEnter={(e) => { e.currentTarget.style.transform = 'translateY(-1px)'; }}
                  onMouseLeave={(e) => { e.currentTarget.style.transform = ''; }}
                >
                  {opt}
                </button>
              ))}
              {/* "Other" — inline text field kholo (Claude-style) */}
              <button
                onClick={() => { setShowOtherInput(true); setOtherText(''); }}
                className="px-4 py-2 rounded-lg text-sm font-medium transition-all"
                style={{ background: 'transparent', color: 'var(--text-dim)', border: '1px dashed var(--border)' }}
              >
                ✏️ Other (apna type karo)
              </button>
            </div>
          ) : (
            <div className="flex gap-2">
              <input
                autoFocus
                type="text"
                value={otherText}
                onChange={(e) => setOtherText(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && otherText.trim()) {
                    const t = otherText.trim();
                    setOtherText(''); setShowOtherInput(false); setPendingQuestion(null);
                    sendMessage(t);
                  }
                }}
                placeholder="Apna jawab yahan likho..."
                className="flex-1 bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-100 focus:outline-none focus:border-cyan-500"
              />
              <button
                onClick={() => {
                  if (otherText.trim()) {
                    const t = otherText.trim();
                    setOtherText(''); setShowOtherInput(false); setPendingQuestion(null);
                    sendMessage(t);
                  }
                }}
                className="px-4 py-2 rounded-lg text-sm font-medium"
                style={{ background: 'linear-gradient(135deg, #22d3ee, #0891b2)', color: '#001018', border: '1px solid #67e8f9' }}
              >
                Send
              </button>
              <button
                onClick={() => setShowOtherInput(false)}
                className="px-3 py-2 rounded-lg text-sm text-gray-400 border border-gray-700"
              >
                ←
              </button>
            </div>
          )}
        </div>
      )}

      {/* Input */}
      {attachments.length > 0 && (
        <div className="flex flex-wrap gap-2 mb-2">
          {attachments.map((a, i) => {
            const isImg = (a.file.type || '').startsWith('image/');
            return (
              <div
                key={`${a.file.name}-${i}`}
                className="flex items-center gap-2 bg-gray-900 border border-gray-700 rounded-lg px-3 py-1.5 text-xs text-gray-200"
              >
                {isImg
                  ? <img src={URL.createObjectURL(a.file)} alt={a.file.name}
                      className="h-8 w-8 rounded object-cover border border-gray-600" />
                  : <span>{/\.pdf$/i.test(a.file.name) ? '📄' : '📎'}</span>}
                <span className="truncate max-w-[180px]" title={a.file.name}>
                  {a.file.name}
                </span>
                <span className="text-gray-500">
                  {(a.file.size / 1024).toFixed(0)} KB
                </span>
                <button
                  onClick={() => removeAttachment(i)}
                  className="text-gray-500 hover:text-red-400 ml-1"
                  title="Remove"
                  disabled={uploading}
                >
                  ✕
                </button>
              </div>
            );
          })}
        </div>
      )}

      <div className="flex gap-2">
        <button
          onClick={toggleVoice}
          className={`px-4 py-2 rounded-xl text-sm transition-colors ${
            voiceListening
              ? 'bg-red-600 text-white animate-pulse'
              : 'bg-gray-800 text-gray-400 hover:bg-gray-700'
          }`}
        >
          {voiceListening ? 'Sun raha...' : 'Mic'}
        </button>
        <button
          onClick={handleAttachClick}
          disabled={loading || uploading}
          className="px-3 py-2 rounded-xl text-sm bg-gray-800 text-gray-400 hover:bg-gray-700 hover:text-gray-200 transition-colors disabled:opacity-50"
          title="File attach karo"
        >
          📎
        </button>
        <input
          ref={fileInputRef}
          type="file"
          multiple
          onChange={handleFilesSelected}
          className="hidden"
        />
        <button
          onClick={() => sendMessage('undo karo')}
          disabled={loading}
          title="Undo — pichla file/folder kaam ulta"
          className="px-3 py-2 rounded-xl text-sm bg-gray-800 text-gray-400 hover:bg-gray-700 hover:text-gray-200 transition-colors disabled:opacity-50"
        >
          ↩️
        </button>
        <button
          onClick={() => sendMessage('redo karo')}
          disabled={loading}
          title="Redo — undo kiya hua dobara"
          className="px-3 py-2 rounded-xl text-sm bg-gray-800 text-gray-400 hover:bg-gray-700 hover:text-gray-200 transition-colors disabled:opacity-50"
        >
          ↪️
        </button>
        <textarea
          ref={inputRef}
          rows={1}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          onPaste={handlePaste}
          placeholder={attachments.length > 0 ? `File + message bhejo... (e.g. "Saif ko WhatsApp pe bhejo")` : "Baat karo ya command do... (Shift+Enter = nayi line, screenshot paste ho jata hai)"}
          disabled={loading}
          className="flex-1 bg-gray-900 border border-gray-800 rounded-xl px-4 py-2 text-sm text-gray-200 focus:outline-none focus:border-blue-500 disabled:opacity-50 resize-none"
          style={{ maxHeight: '140px', minHeight: '40px' }}
        />
        <button
          onClick={() => sendMessage(input)}
          disabled={loading || uploading || (!input.trim() && attachments.length === 0)}
          className="px-6 py-2 bg-blue-600 hover:bg-blue-500 disabled:opacity-50 text-white text-sm rounded-xl transition-colors"
        >
          {uploading ? 'Upload...' : 'Send'}
        </button>
      </div>

      {/* Image lightbox — chat ke andar full image, X / backdrop / Esc se band */}
      {lightboxUrl && (
        <div
          onClick={() => setLightboxUrl(null)}
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 p-6"
        >
          <button
            onClick={() => setLightboxUrl(null)}
            title="Band karo (Esc)"
            className="absolute top-4 right-6 text-white text-4xl leading-none hover:text-red-400"
          >
            ×
          </button>
          <img
            src={lightboxUrl}
            alt="preview"
            onClick={(e) => e.stopPropagation()}
            className="max-h-[90vh] max-w-[90vw] rounded-lg object-contain shadow-2xl"
          />
        </div>
      )}
    </div>
  );
}

// Inline markdown: **bold**, `code`, [text](url)
function renderInline(text, kp) {
  const nodes = [];
  const regex = /(\*\*([^*]+)\*\*|`([^`]+)`|\[([^\]]+)\]\(([^)]+)\))/g;
  let last = 0, k = 0, m;
  while ((m = regex.exec(text)) !== null) {
    if (m.index > last) nodes.push(text.slice(last, m.index));
    if (m[2] !== undefined) nodes.push(<strong key={`${kp}-b${k++}`} className="text-white">{m[2]}</strong>);
    else if (m[3] !== undefined) nodes.push(<code key={`${kp}-c${k++}`} className="px-1 py-0.5 rounded bg-black/40 text-cyan-300 text-[12px]">{m[3]}</code>);
    else if (m[4] !== undefined) nodes.push(<a key={`${kp}-a${k++}`} href={m[5]} target="_blank" rel="noreferrer" className="text-cyan-400 underline break-all">{m[4]}</a>);
    last = regex.lastIndex;
  }
  if (last < text.length) nodes.push(text.slice(last));
  return nodes;
}

// Lightweight markdown → JSX (headings, bullets, numbered, bold, code, links,
// blank-line spacing). Professional rendering for ALL assistant replies.
// memo: typing pe (jab sirf `input` badle) yeh dobara parse NA ho — warna har
// keystroke pe saare messages re-render hote the => typing laggy/hang.
const MarkdownText = memo(function MarkdownText({ text }) {
  const lines = (text || '').split('\n');
  const blocks = [];
  let list = null;
  const flush = (key) => {
    if (list) {
      blocks.push(<ul key={`ul-${key}`} className="list-disc pl-5 space-y-1 my-1">{list}</ul>);
      list = null;
    }
  };
  lines.forEach((line, idx) => {
    const t = line.trim();
    if (/^#{1,6}\s/.test(t)) {
      flush(idx);
      blocks.push(
        <div key={idx} className="font-bold text-cyan-200 mt-2 mb-1">
          {renderInline(t.replace(/^#{1,6}\s/, ''), idx)}
        </div>
      );
    } else if (/^[-*•]\s+/.test(t)) {
      (list = list || []).push(<li key={idx}>{renderInline(t.replace(/^[-*•]\s+/, ''), idx)}</li>);
    } else if (t === '') {
      flush(idx);
      blocks.push(<div key={idx} className="h-2" />);
    } else {
      flush(idx);
      blocks.push(<div key={idx}>{renderInline(line, idx)}</div>);
    }
  });
  flush('end');
  return <div className="text-sm leading-relaxed space-y-1">{blocks}</div>;
});

function VerificationButtons({ verification, onAction }) {
  if (!verification?.buttons) return null;

  const colorMap = {
    green: { bg: 'linear-gradient(135deg, #10b981, #059669)', border: '#34d399' },
    red: { bg: 'linear-gradient(135deg, #ef4444, #b91c1c)', border: '#f87171' },
    purple: { bg: 'linear-gradient(135deg, #8b5cf6, #6d28d9)', border: '#a78bfa' },
    blue: { bg: 'linear-gradient(135deg, #22d3ee, #0891b2)', border: '#67e8f9' },
  };

  return (
    <div className="mt-3 flex flex-wrap gap-2">
      {verification.buttons.map((btn) => {
        const c = colorMap[btn.color] || colorMap.blue;
        return (
          <button
            key={btn.id}
            onClick={() => onAction(btn.id, btn.label)}
            className="px-4 py-2 rounded font-display uppercase tracking-widest text-xs font-bold transition-all"
            style={{
              background: c.bg,
              color: '#fff',
              border: `1px solid ${c.border}`,
              boxShadow: `0 0 15px ${c.border}66`,
            }}
            onMouseEnter={(e) => {
              e.currentTarget.style.transform = 'translateY(-1px)';
              e.currentTarget.style.boxShadow = `0 0 25px ${c.border}aa`;
            }}
            onMouseLeave={(e) => {
              e.currentTarget.style.transform = '';
              e.currentTarget.style.boxShadow = `0 0 15px ${c.border}66`;
            }}
          >
            {btn.label}
          </button>
        );
      })}
      {verification.task_index !== undefined && (
        <span className="ml-auto self-center font-mono text-[10px]"
              style={{ color: 'var(--text-dim)' }}>
          Task {verification.task_index + 1}/{verification.total}
        </span>
      )}
    </div>
  );
}
