import { useState, useCallback } from 'react';
import { api } from '../api';
import { usePolling } from '../hooks/usePolling';

function formatDuration(m) {
  const secs = m.duration_seconds;
  if (typeof secs === 'number') {
    if (secs < 60) return `${secs}s`;
    const mins = Math.floor(secs / 60);
    const rem = secs % 60;
    return rem ? `${mins}m ${rem}s` : `${mins}m`;
  }
  return `${m.duration_minutes ?? 0} min`;
}

export default function MeetingsPage() {
  const fetchMeetings = useCallback(() => api.getMeetings(), []);
  const { data, loading, error } = usePolling(fetchMeetings, 10000);
  const [expanded, setExpanded] = useState(null);

  if (loading) return <p className="text-gray-500">Loading...</p>;
  if (error) return <p className="text-red-400">Error: {error}</p>;

  const activeMeetings = (data || []).filter((m) => m.status === 'active');
  const pastMeetings = (data || []).filter((m) => m.status !== 'active');

  return (
    <div>
      <h2 className="text-2xl font-bold mb-6">Meeting History</h2>

      {/* Active Meeting */}
      {activeMeetings.length > 0 && (
        <div className="mb-6">
          <h3 className="text-sm font-semibold text-purple-400 mb-3">Active Meeting</h3>
          {activeMeetings.map((m) => (
            <div key={m.id} className="bg-purple-900/20 border border-purple-800/50 rounded-xl p-5 animate-pulse">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-3">
                  <span className="w-3 h-3 rounded-full bg-purple-500 animate-pulse"></span>
                  <h3 className="text-lg font-semibold text-white">{m.title}</h3>
                </div>
                <span className="text-sm text-purple-400">{formatDuration(m)}</span>
              </div>
              <p className="text-xs text-purple-400 mt-2">
                Started: {m.started_at ? new Date(m.started_at).toLocaleString() : ''}
              </p>
            </div>
          ))}
        </div>
      )}

      {/* Past Meetings */}
      {pastMeetings.length === 0 ? (
        <div className="bg-gray-900 border border-gray-800 rounded-xl p-10 text-center text-gray-500">
          Koi purani meeting nahi hai. Chat page se "Start Meeting" dabao.
        </div>
      ) : (
        <div className="space-y-3">
          {pastMeetings.map((m) => (
            <div key={m.id} className="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden">
              {/* Header */}
              <div
                className="p-4 cursor-pointer hover:bg-gray-800/50 transition-colors"
                onClick={() => setExpanded(expanded === m.id ? null : m.id)}
              >
                <div className="flex items-center justify-between">
                  <div className="flex-1">
                    <div className="flex items-center gap-3">
                      <h3 className="text-sm font-semibold text-gray-200">{m.title}</h3>
                      <span className="text-xs bg-gray-800 text-gray-500 px-2 py-0.5 rounded">
                        {formatDuration(m)}
                      </span>
                    </div>
                    <div className="flex items-center gap-4 mt-1 text-xs text-gray-600">
                      <span>{m.started_at ? new Date(m.started_at).toLocaleDateString('en-US', { weekday: 'short', day: 'numeric', month: 'short' }) : ''}</span>
                      <span>
                        {m.started_at ? new Date(m.started_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : ''}
                        {m.ended_at ? ` — ${new Date(m.ended_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}` : ''}
                      </span>
                      <span>{m.total_chunks ?? 0} chunks</span>
                      <span>{(m.tasks?.length ?? m.total_tasks) || 0} tasks</span>
                    </div>
                  </div>
                  <span className="text-gray-600 text-lg">{expanded === m.id ? '▲' : '▼'}</span>
                </div>
              </div>

              {/* Expanded: summary + tasks extracted in this meeting */}
              {expanded === m.id && (
                <div className="border-t border-gray-800 p-5">
                  {m.summary ? (
                    <div className="prose prose-invert prose-sm max-w-none mb-4">
                      <pre className="whitespace-pre-wrap text-sm text-gray-300 font-sans leading-relaxed bg-gray-950 rounded-lg p-4">
                        {m.summary}
                      </pre>
                    </div>
                  ) : (
                    <p className="text-sm text-gray-600 mb-4">Koi summary nahi — meeting mein transcription nahi hui.</p>
                  )}

                  {/* Tasks extracted during this meeting */}
                  {Array.isArray(m.tasks) && m.tasks.length > 0 ? (
                    <div className="mb-4">
                      <p className="text-xs uppercase tracking-wider text-cyan-400 mb-2">
                        Tasks Extracted ({m.tasks.length})
                      </p>
                      <div className="space-y-2">
                        {m.tasks.map((t) => (
                          <div key={t.id} className="bg-gray-950 border border-gray-800 rounded-lg p-3 text-sm">
                            <div className="flex items-start justify-between gap-3">
                              <div className="flex-1 min-w-0">
                                <p className="text-gray-200 font-medium truncate" title={t.title}>{t.title}</p>
                                {t.description && (
                                  <p className="text-xs text-gray-500 mt-0.5 line-clamp-2">{t.description}</p>
                                )}
                              </div>
                              <div className="flex flex-col items-end gap-1 text-xs">
                                {t.priority && (
                                  <span className={`px-2 py-0.5 rounded uppercase tracking-wider ${
                                    t.priority === 'urgent' ? 'bg-red-900/40 text-red-300' :
                                    t.priority === 'high' ? 'bg-orange-900/40 text-orange-300' :
                                    t.priority === 'medium' ? 'bg-yellow-900/30 text-yellow-300' :
                                    'bg-gray-800 text-gray-400'
                                  }`}>{t.priority}</span>
                                )}
                                {t.assigned_to && (
                                  <span className="text-gray-500">→ {t.assigned_to}</span>
                                )}
                              </div>
                            </div>
                          </div>
                        ))}
                      </div>
                    </div>
                  ) : (
                    <p className="text-sm text-gray-600 mb-4">Iss meeting se koi task extract nahi hua.</p>
                  )}

                  <div className="grid grid-cols-3 gap-4 mt-4">
                    <StatBox label="Duration" value={`${formatDuration(m)}`} />
                    <StatBox label="Audio Chunks" value={m.total_chunks} />
                    <StatBox label="Tasks Extracted" value={m.tasks?.length ?? m.total_tasks} />
                  </div>
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function StatBox({ label, value }) {
  return (
    <div className="bg-gray-950 rounded-lg p-3 text-center">
      <p className="text-xs text-gray-600">{label}</p>
      <p className="text-lg font-bold text-gray-300">{value}</p>
    </div>
  );
}
