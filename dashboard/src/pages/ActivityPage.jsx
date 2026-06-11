import { useCallback } from 'react';
import { api } from '../api';
import { usePolling } from '../hooks/usePolling';

export default function ActivityPage() {
  const fetchAll = useCallback(async () => {
    const [chunks, transcriptions, tasks] = await Promise.all([
      api.getChunks(20),
      api.getTranscriptions(20),
      api.getTasks(undefined, 20),
    ]);

    // Merge into a single timeline
    const events = [];

    for (const c of chunks) {
      events.push({
        type: 'chunk',
        time: c.created_at,
        icon: c.source === 'loopback' ? 'speaker' : 'mic',
        title: `Audio chunk captured`,
        detail: `${c.source} | ${c.duration_sec?.toFixed(1)}s | speech: ${c.has_speech ? (c.speech_ratio * 100).toFixed(0) + '%' : 'none'}`,
        status: c.status,
        color: 'blue',
      });
    }

    for (const t of transcriptions) {
      events.push({
        type: 'transcription',
        time: t.created_at,
        icon: 'text',
        title: 'Transcription',
        detail: t.text?.length > 120 ? t.text.slice(0, 120) + '...' : t.text,
        status: t.language?.toUpperCase(),
        color: 'green',
      });
    }

    for (const t of tasks) {
      events.push({
        type: 'task',
        time: t.created_at,
        icon: 'task',
        title: t.title,
        detail: `${t.action_type || 'other'} | ${t.priority} priority`,
        status: t.status,
        color: t.status === 'completed' ? 'green' : t.status === 'pending' ? 'yellow' : t.status === 'approved' ? 'blue' : 'red',
      });
    }

    // Sort by time descending
    events.sort((a, b) => new Date(b.time) - new Date(a.time));
    return events.slice(0, 50);
  }, []);

  const { data, loading, error } = usePolling(fetchAll, 4000);

  if (loading) return <p className="text-gray-500">Loading...</p>;
  if (error) return <p className="text-red-400">Error: {error}</p>;

  const COLOR_MAP = {
    blue: 'border-blue-500/50 bg-blue-500',
    green: 'border-green-500/50 bg-green-500',
    yellow: 'border-yellow-500/50 bg-yellow-500',
    red: 'border-red-500/50 bg-red-500',
    purple: 'border-purple-500/50 bg-purple-500',
  };

  const TYPE_ICON = {
    chunk: 'audio',
    transcription: 'txt',
    task: 'task',
  };

  return (
    <div>
      <h2 className="text-2xl font-bold mb-6">Activity Feed</h2>

      {data.length === 0 ? (
        <div className="bg-gray-900 border border-gray-800 rounded-xl p-10 text-center text-gray-500">
          No activity yet.
        </div>
      ) : (
        <div className="relative">
          {/* Timeline line */}
          <div className="absolute left-4 top-0 bottom-0 w-px bg-gray-800"></div>

          <div className="space-y-1">
            {data.map((event, i) => {
              const dotColor = COLOR_MAP[event.color] || COLOR_MAP.blue;
              const [borderC, bgC] = dotColor.split(' ');
              const stableKey = event.id ?? `${event.type ?? ''}-${event.created_at ?? event.time ?? ''}-${i}`;
              return (
                <div key={stableKey} className="flex gap-4 pl-2 py-2">
                  <div className="relative flex-shrink-0">
                    <div className={`w-5 h-5 rounded-full border-2 ${borderC} ${bgC}/20 flex items-center justify-center`}>
                      <div className={`w-2 h-2 rounded-full ${bgC}`}></div>
                    </div>
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="text-xs font-mono bg-gray-800 text-gray-500 px-1.5 py-0.5 rounded">
                        {TYPE_ICON[event.type] || event.type}
                      </span>
                      <span className="text-sm text-gray-200 truncate">{event.title}</span>
                      {event.status && (
                        <span className="text-xs text-gray-600 ml-auto shrink-0">{event.status}</span>
                      )}
                    </div>
                    {event.detail && (
                      <p className="text-xs text-gray-500 mt-0.5 truncate">{event.detail}</p>
                    )}
                    <p className="text-xs text-gray-700 mt-0.5">
                      {event.time ? new Date(event.time).toLocaleString() : ''}
                    </p>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
