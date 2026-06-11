import { useState, useCallback } from 'react';
import { api } from '../api';
import { usePolling } from '../hooks/usePolling';

const STATUS_FILTERS = [
  { value: '', label: 'All' },
  { value: 'pending', label: 'Pending' },
  { value: 'approved', label: 'Approved' },
  { value: 'rejected', label: 'Rejected' },
  { value: 'completed', label: 'Completed' },
];

const PRIORITY_COLORS = {
  urgent: 'bg-red-900/50 text-red-400',
  high: 'bg-orange-900/50 text-orange-400',
  medium: 'bg-yellow-900/50 text-yellow-400',
  low: 'bg-gray-800 text-gray-400',
};

const STATUS_COLORS = {
  pending: 'bg-yellow-900/50 text-yellow-400',
  approved: 'bg-green-900/50 text-green-400',
  rejected: 'bg-red-900/50 text-red-400',
  in_progress: 'bg-blue-900/50 text-blue-400',
  completed: 'bg-gray-800 text-gray-400',
};

const EMPTY_TASK = {
  title: '',
  description: '',
  assigned_to: '',
  priority: 'medium',
  action_type: 'other',
  action_payload: '',
};

export default function TasksPage() {
  const [filter, setFilter] = useState('');
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState(EMPTY_TASK);
  const fetchData = useCallback(() => api.getTasks(filter || undefined, 100), [filter]);
  const { data, loading, error, refresh } = usePolling(fetchData, 5000);

  const handleAction = async (taskId, action) => {
    try {
      if (action === 'approve') await api.approveTask(taskId);
      else await api.rejectTask(taskId);
      refresh();
    } catch (err) {
      alert(`Failed: ${err.message}`);
    }
  };

  const handleCreate = async () => {
    if (!form.title.trim()) return alert('Title required');
    try {
      await api.createTask({
        ...form,
        assigned_to: form.assigned_to || null,
        action_payload: form.action_payload || null,
      });
      setForm(EMPTY_TASK);
      setShowForm(false);
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
        <h2 className="text-2xl font-bold">Tasks</h2>
        <div className="flex items-center gap-3">
          <button
            onClick={() => setShowForm(!showForm)}
            className="px-4 py-1.5 bg-blue-600 hover:bg-blue-500 text-white text-xs rounded transition-colors"
          >
            + Add Task
          </button>
          <div className="flex gap-1">
          {STATUS_FILTERS.map((f) => (
            <button
              key={f.value}
              onClick={() => setFilter(f.value)}
              className={`px-3 py-1 rounded text-xs transition-colors ${
                filter === f.value
                  ? 'bg-blue-600 text-white'
                  : 'bg-gray-800 text-gray-400 hover:bg-gray-700'
              }`}
            >
              {f.label}
            </button>
          ))}
          </div>
        </div>
      </div>

      {/* Add Task Form */}
      {showForm && (
        <div className="bg-gray-900 border border-blue-800 rounded-xl p-5 mb-6">
          <h3 className="text-lg font-semibold mb-4">Add New Task</h3>
          <div className="grid grid-cols-2 gap-4 text-sm">
            <label className="col-span-2 block">
              <span className="text-xs text-gray-500">Title</span>
              <input type="text" value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} placeholder="e.g. Ahmed ko message karo" className="mt-1 w-full bg-gray-800 border border-gray-700 rounded px-3 py-1.5 text-sm text-gray-200 focus:outline-none focus:border-blue-500" />
            </label>
            <label className="col-span-2 block">
              <span className="text-xs text-gray-500">Description</span>
              <textarea value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} rows={2} placeholder="Details..." className="mt-1 w-full bg-gray-800 border border-gray-700 rounded px-3 py-1.5 text-sm text-gray-200 focus:outline-none focus:border-blue-500" />
            </label>
            <label className="block">
              <span className="text-xs text-gray-500">Assigned To</span>
              <input type="text" value={form.assigned_to} onChange={(e) => setForm({ ...form, assigned_to: e.target.value })} placeholder="Ahmed, Hasnain..." className="mt-1 w-full bg-gray-800 border border-gray-700 rounded px-3 py-1.5 text-sm text-gray-200 focus:outline-none focus:border-blue-500" />
            </label>
            <label className="block">
              <span className="text-xs text-gray-500">Priority</span>
              <select value={form.priority} onChange={(e) => setForm({ ...form, priority: e.target.value })} className="mt-1 w-full bg-gray-800 border border-gray-700 rounded px-3 py-1.5 text-sm text-gray-200">
                <option value="low">Low</option>
                <option value="medium">Medium</option>
                <option value="high">High</option>
                <option value="urgent">Urgent</option>
              </select>
            </label>
            <label className="block">
              <span className="text-xs text-gray-500">Action Type</span>
              <select value={form.action_type} onChange={(e) => setForm({ ...form, action_type: e.target.value })} className="mt-1 w-full bg-gray-800 border border-gray-700 rounded px-3 py-1.5 text-sm text-gray-200">
                <option value="send_message">Send Message</option>
                <option value="reminder">Reminder</option>
                <option value="create_doc">Create Doc</option>
                <option value="browser_action">Browser Action</option>
                <option value="api_call">API Call</option>
                <option value="other">Other</option>
              </select>
            </label>
            <label className="block">
              <span className="text-xs text-gray-500">Action Payload (JSON)</span>
              <input type="text" value={form.action_payload} onChange={(e) => setForm({ ...form, action_payload: e.target.value })} placeholder='{"platform":"whatsapp","to":"Ahmed","message":"..."}' className="mt-1 w-full bg-gray-800 border border-gray-700 rounded px-3 py-1.5 text-sm text-gray-200 focus:outline-none focus:border-blue-500" />
            </label>
          </div>
          <div className="flex gap-2 mt-4">
            <button onClick={handleCreate} className="px-4 py-2 bg-green-600 hover:bg-green-500 text-white text-sm rounded">Create Task</button>
            <button onClick={() => setShowForm(false)} className="px-4 py-2 bg-gray-700 hover:bg-gray-600 text-white text-sm rounded">Cancel</button>
          </div>
        </div>
      )}

      {data.length === 0 ? (
        <div className="bg-gray-900 border border-gray-800 rounded-xl p-10 text-center text-gray-500">
          No tasks found{filter ? ` with status "${filter}"` : ''}.
        </div>
      ) : (
        <div className="space-y-3">
          {data.map((task) => (
            <div key={task.id} className="bg-gray-900 border border-gray-800 rounded-xl p-4">
              <div className="flex items-start justify-between mb-2">
                <div>
                  <h3 className="text-sm font-semibold text-gray-200">{task.title}</h3>
                  {task.description && (
                    <p className="text-xs text-gray-500 mt-1">{task.description}</p>
                  )}
                </div>
                <div className="flex items-center gap-2 shrink-0 ml-4">
                  <span className={`text-xs px-2 py-0.5 rounded ${PRIORITY_COLORS[task.priority] || ''}`}>
                    {task.priority}
                  </span>
                  <span className={`text-xs px-2 py-0.5 rounded ${STATUS_COLORS[task.status] || ''}`}>
                    {task.status}
                  </span>
                </div>
              </div>
              <div className="flex items-center justify-between mt-3">
                <div className="flex items-center gap-3 text-xs text-gray-600">
                  {task.assigned_to && <span>Assigned: {task.assigned_to}</span>}
                  {task.action_type && <span>Action: {task.action_type}</span>}
                  <span>{task.created_at ? new Date(task.created_at).toLocaleString() : ''}</span>
                </div>
                {task.status === 'pending' && (
                  <div className="flex gap-2">
                    <button
                      onClick={() => handleAction(task.id, 'approve')}
                      className="px-3 py-1 bg-green-600 hover:bg-green-500 text-white text-xs rounded transition-colors"
                    >
                      Approve
                    </button>
                    <button
                      onClick={() => handleAction(task.id, 'reject')}
                      className="px-3 py-1 bg-red-600 hover:bg-red-500 text-white text-xs rounded transition-colors"
                    >
                      Reject
                    </button>
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
