import { useState, useRef, useEffect } from 'react';
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
  const endRef = useRef(null);
  const recognitionRef = useRef(null);
  const fileInputRef = useRef(null);

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
      api.getChatHistory().then((history) => {
        if (history.length > 0) {
          setMessages(history.map((m) => ({ role: m.role, content: m.content, actions: m.actions || [] })));
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

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

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

  const sendMessage = async (text, confirmToken = null) => {
    const hasAttachments = attachments.length > 0;
    if (!text.trim() && !hasAttachments) return;

    // Show user's bubble with attachment chips
    const displayText = text.trim() || (hasAttachments ? '(file bhej raha hoon)' : '');
    const userMsg = {
      role: 'user',
      content: displayText,
      attachments_preview: attachments.map((a) => a.file.name),
    };
    setMessages((prev) => [...prev, userMsg]);
    setInput('');
    setLoading(true);

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
      <div className="flex-1 overflow-y-auto mb-4 space-y-3">
        {messages.map((msg, i) => (
          <div key={msg.id ?? `${msg.role}-${msg.created_at ?? ''}-${i}`} className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}>
            <div className={`max-w-[75%] rounded-xl px-4 py-3 ${
              msg.role === 'user'
                ? 'bg-blue-600 text-white'
                : 'bg-gray-900 border border-gray-800 text-gray-200'
            }`}>
              <p className="text-sm whitespace-pre-wrap">{msg.content}</p>

              {/* Attachment chips on user's own message */}
              {msg.role === 'user' && msg.attachments_preview?.length > 0 && (
                <div className="mt-2 flex flex-wrap gap-1">
                  {msg.attachments_preview.map((name, k) => (
                    <span
                      key={`${name}-${k}`}
                      className="text-[11px] px-2 py-0.5 rounded bg-blue-500/30 border border-blue-400/40"
                      title={name}
                    >
                      📎 {name}
                    </span>
                  ))}
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
                      {a.action && `${a.action}: ${a.status}`}
                    </div>
                  ))}
                </div>
              )}

              {/* Pending confirmation buttons */}
              {msg.pending?.length > 0 && (
                <div className="mt-3 flex gap-2">
                  {msg.pending.map((p, j) => (
                    <div key={j} className="flex gap-2">
                      <button
                        onClick={() => handleConfirm(p.token, 'yes')}
                        className="px-3 py-1 text-xs bg-green-600 hover:bg-green-500 text-white rounded"
                      >
                        ✅ Haan, kar
                      </button>
                      <button
                        onClick={() => handleConfirm(p.token, 'no')}
                        className="px-3 py-1 text-xs bg-red-600 hover:bg-red-500 text-white rounded"
                      >
                        ❌ Cancel
                      </button>
                    </div>
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
            <div className="bg-gray-900 border border-gray-800 rounded-xl px-4 py-3">
              <p className="text-sm text-gray-500">Soch raha hun...</p>
            </div>
          </div>
        )}

        <div ref={endRef} />
      </div>

      {/* Input */}
      {attachments.length > 0 && (
        <div className="flex flex-wrap gap-2 mb-2">
          {attachments.map((a, i) => (
            <div
              key={`${a.file.name}-${i}`}
              className="flex items-center gap-2 bg-gray-900 border border-gray-700 rounded-lg px-3 py-1.5 text-xs text-gray-200"
            >
              <span>📎</span>
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
          ))}
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
        <input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={attachments.length > 0 ? `File + message bhejo... (e.g. "Saif ko WhatsApp pe bhejo")` : "Baat karo ya command do..."}
          disabled={loading}
          className="flex-1 bg-gray-900 border border-gray-800 rounded-xl px-4 py-2 text-sm text-gray-200 focus:outline-none focus:border-blue-500 disabled:opacity-50"
        />
        <button
          onClick={() => sendMessage(input)}
          disabled={loading || uploading || (!input.trim() && attachments.length === 0)}
          className="px-6 py-2 bg-blue-600 hover:bg-blue-500 disabled:opacity-50 text-white text-sm rounded-xl transition-colors"
        >
          {uploading ? 'Upload...' : 'Send'}
        </button>
      </div>
    </div>
  );
}

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
