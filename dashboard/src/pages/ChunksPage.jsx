import { useCallback } from 'react';
import { api } from '../api';
import { usePolling } from '../hooks/usePolling';

const STATUS_COLORS = {
  received: 'bg-gray-800 text-gray-400',
  transcribing: 'bg-blue-900/50 text-blue-400',
  transcribed: 'bg-green-900/50 text-green-400',
  processed: 'bg-purple-900/50 text-purple-400',
  failed: 'bg-red-900/50 text-red-400',
};

export default function ChunksPage() {
  const fetchData = useCallback(() => api.getChunks(100), []);
  const { data, loading, error } = usePolling(fetchData, 5000);

  if (loading) return <p className="text-gray-500">Loading...</p>;
  if (error) return <p className="text-red-400">Error: {error}</p>;

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h2 className="text-2xl font-bold">Audio Chunks</h2>
        <span className="text-sm text-gray-500">{data.length} chunks</span>
      </div>

      {data.length === 0 ? (
        <div className="bg-gray-900 border border-gray-800 rounded-xl p-10 text-center text-gray-500">
          No audio chunks yet. Start the desktop agent to begin capturing.
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm text-left">
            <thead className="text-xs text-gray-500 uppercase border-b border-gray-800">
              <tr>
                <th className="px-4 py-3">ID</th>
                <th className="px-4 py-3">Source</th>
                <th className="px-4 py-3">Duration</th>
                <th className="px-4 py-3">Speech</th>
                <th className="px-4 py-3">Window</th>
                <th className="px-4 py-3">Status</th>
                <th className="px-4 py-3">Time</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-800/50">
              {data.map((chunk) => (
                <tr key={chunk.chunk_id} className="hover:bg-gray-900/50">
                  <td className="px-4 py-3 font-mono text-xs text-gray-500">
                    {chunk.chunk_id?.slice(0, 8)}
                  </td>
                  <td className="px-4 py-3">
                    <span className={`text-xs px-2 py-0.5 rounded ${
                      chunk.source === 'loopback' ? 'bg-blue-900/50 text-blue-400' : 'bg-green-900/50 text-green-400'
                    }`}>
                      {chunk.source}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-gray-400">
                    {chunk.duration_sec?.toFixed(1)}s
                  </td>
                  <td className="px-4 py-3">
                    {chunk.has_speech ? (
                      <span className="text-green-400">{(chunk.speech_ratio * 100).toFixed(0)}%</span>
                    ) : (
                      <span className="text-gray-600">-</span>
                    )}
                  </td>
                  <td className="px-4 py-3 text-xs text-gray-500 max-w-48 truncate">
                    {chunk.active_window || '-'}
                  </td>
                  <td className="px-4 py-3">
                    <span className={`text-xs px-2 py-0.5 rounded ${STATUS_COLORS[chunk.status] || ''}`}>
                      {chunk.status}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-xs text-gray-600">
                    {chunk.created_at ? new Date(chunk.created_at).toLocaleTimeString() : ''}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
