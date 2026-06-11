import { useCallback } from 'react';
import { api } from '../api';
import { usePolling } from '../hooks/usePolling';

export default function TranscriptionsPage() {
  const fetchData = useCallback(() => api.getTranscriptions(100), []);
  const { data, loading, error } = usePolling(fetchData, 5000);

  if (loading) return <p className="text-gray-500">Loading...</p>;
  if (error) return <p className="text-red-400">Error: {error}</p>;

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h2 className="text-2xl font-bold">Transcriptions</h2>
        <span className="text-sm text-gray-500">{data.length} results</span>
      </div>

      {data.length === 0 ? (
        <div className="bg-gray-900 border border-gray-800 rounded-xl p-10 text-center text-gray-500">
          No transcriptions yet. Start the desktop agent to begin capturing audio.
        </div>
      ) : (
        <div className="space-y-3">
          {data.map((t) => (
            <div key={t.id} className="bg-gray-900 border border-gray-800 rounded-xl p-4">
              <div className="flex items-start justify-between mb-2">
                <div className="flex items-center gap-2">
                  <span className="text-xs font-mono text-gray-600">{t.chunk_id?.slice(0, 8)}</span>
                  {t.language && (
                    <span className="text-xs bg-blue-900/50 text-blue-400 px-2 py-0.5 rounded">
                      {t.language.toUpperCase()}
                    </span>
                  )}
                  {t.confidence != null && (
                    <span className="text-xs text-gray-500">
                      {(t.confidence * 100).toFixed(0)}% conf
                    </span>
                  )}
                </div>
                <span className="text-xs text-gray-600">
                  {t.created_at ? new Date(t.created_at).toLocaleString() : ''}
                </span>
              </div>
              <p className="text-sm text-gray-300 leading-relaxed">{t.text}</p>
              {t.processing_time_sec != null && (
                <p className="text-xs text-gray-600 mt-2">
                  Processed in {t.processing_time_sec.toFixed(1)}s
                </p>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
