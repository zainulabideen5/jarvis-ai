import { useCallback } from 'react';
import { api } from '../api';
import { usePolling } from '../hooks/usePolling';

export default function AgentsPage() {
  const fetchAgents = useCallback(() => api.getAgents(), []);
  const { data, loading, error, refresh } = usePolling(fetchAgents, 5000);

  const handleStop = async (agentId) => {
    if (!confirm(`Stop agent "${agentId}"? It will disconnect.`)) return;
    try {
      await api.stopAgent(agentId);
      refresh();
    } catch (err) {
      alert(`Failed: ${err.message}`);
    }
  };

  if (loading) return <p className="text-gray-500">Loading...</p>;
  if (error) return <p className="text-red-400">Error: {error}</p>;

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h2 className="text-2xl font-bold">Connected Agents</h2>
        <span className="text-sm text-gray-500">
          {data.filter((a) => a.is_online).length} online / {data.length} total
        </span>
      </div>

      {data.length === 0 ? (
        <div className="bg-gray-900 border border-gray-800 rounded-xl p-10 text-center text-gray-500">
          No agents registered. Start a desktop agent to connect.
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {data.map((agent) => (
            <div
              key={agent.agent_id}
              className={`bg-gray-900 border rounded-xl p-5 ${
                agent.is_online ? 'border-green-800/50' : 'border-gray-800'
              }`}
            >
              <div className="flex items-center justify-between mb-3">
                <div className="flex items-center gap-2">
                  <span className={`w-3 h-3 rounded-full ${agent.is_online ? 'bg-green-500' : 'bg-gray-600'}`}></span>
                  <h3 className="text-lg font-semibold text-gray-200">
                    {agent.name || agent.agent_id}
                  </h3>
                </div>
                <div className="flex items-center gap-2">
                  {agent.is_online && (
                    <button
                      onClick={() => handleStop(agent.agent_id)}
                      className="px-3 py-1 bg-red-600 hover:bg-red-500 text-white text-xs rounded transition-colors"
                    >
                      Stop Agent
                    </button>
                  )}
                  <span className={`text-xs px-2 py-0.5 rounded ${
                    agent.is_online ? 'bg-green-900/50 text-green-400' : 'bg-gray-800 text-gray-500'
                  }`}>
                    {agent.is_online ? 'Online' : 'Offline'}
                  </span>
                </div>
              </div>

              <div className="space-y-1.5 text-sm">
                <InfoRow label="Agent ID" value={agent.agent_id} />
                {agent.hostname && <InfoRow label="Hostname" value={agent.hostname} />}
                <InfoRow label="Total Chunks" value={agent.total_chunks} />
                <InfoRow
                  label="Last Seen"
                  value={agent.last_seen ? new Date(agent.last_seen).toLocaleString() : 'Never'}
                />
                <InfoRow
                  label="Registered"
                  value={agent.created_at ? new Date(agent.created_at).toLocaleString() : ''}
                />
              </div>
            </div>
          ))}
        </div>
      )}

      <div className="mt-8 bg-gray-900 border border-gray-800 rounded-xl p-5">
        <h3 className="text-sm font-semibold text-gray-300 mb-3">Add Another Agent</h3>
        <p className="text-xs text-gray-500 mb-3">
          To connect Ahmed's laptop or any other machine, set a unique agent ID:
        </p>
        <pre className="bg-gray-950 border border-gray-800 rounded-lg p-3 text-xs text-gray-400">
{`# In .env on the second machine:
JARVIS_AGENT_ID=ahmed-laptop
JARVIS_SERVER_WS_URL=ws://YOUR_SERVER_IP:8000/ws/agent

# Then run:
python -m jarvis_agent`}
        </pre>
      </div>
    </div>
  );
}

function InfoRow({ label, value }) {
  return (
    <div className="flex items-center justify-between">
      <span className="text-gray-500">{label}</span>
      <span className="text-gray-300 font-mono text-xs">{value}</span>
    </div>
  );
}
