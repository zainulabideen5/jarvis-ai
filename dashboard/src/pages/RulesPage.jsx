import { useState, useCallback } from 'react';
import { api } from '../api';
import { usePolling } from '../hooks/usePolling';

const EMPTY_RULE = {
  name: '',
  description: '',
  enabled: true,
  match_action_type: '',
  match_priority: '',
  match_keyword: '',
  match_assigned_to: '',
  match_window: '',
  auto_approve: false,
  override_priority: '',
  override_assigned_to: '',
  notify_telegram: false,
};

export default function RulesPage() {
  const fetchRules = useCallback(() => api.getRules(), []);
  const { data, loading, error, refresh } = usePolling(fetchRules, 10000);
  const [editing, setEditing] = useState(null); // null or rule object
  const [form, setForm] = useState(EMPTY_RULE);

  const startNew = () => {
    setForm({ ...EMPTY_RULE });
    setEditing('new');
  };

  const startEdit = (rule) => {
    setForm({
      ...EMPTY_RULE,
      ...Object.fromEntries(
        Object.entries(rule).map(([k, v]) => [k, v ?? ''])
      ),
    });
    setEditing(rule.id);
  };

  const save = async () => {
    const payload = {
      ...form,
      match_action_type: form.match_action_type || null,
      match_priority: form.match_priority || null,
      match_keyword: form.match_keyword || null,
      match_assigned_to: form.match_assigned_to || null,
      match_window: form.match_window || null,
      override_priority: form.override_priority || null,
      override_assigned_to: form.override_assigned_to || null,
    };
    try {
      if (editing === 'new') {
        await api.createRule(payload);
      } else {
        await api.updateRule(editing, payload);
      }
      setEditing(null);
      refresh();
    } catch (err) {
      alert(`Failed: ${err.message}`);
    }
  };

  const handleToggle = async (id) => {
    await api.toggleRule(id);
    refresh();
  };

  const handleDelete = async (id) => {
    if (!confirm('Delete this rule?')) return;
    await api.deleteRule(id);
    refresh();
  };

  if (loading) return <p className="text-gray-500">Loading...</p>;
  if (error) return <p className="text-red-400">Error: {error}</p>;

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h2 className="text-2xl font-bold">Rules Engine</h2>
        <button
          onClick={startNew}
          className="px-4 py-2 bg-blue-600 hover:bg-blue-500 text-white text-sm rounded transition-colors"
        >
          + New Rule
        </button>
      </div>

      {/* Editor */}
      {editing !== null && (
        <div className="bg-gray-900 border border-blue-800 rounded-xl p-5 mb-6">
          <h3 className="text-lg font-semibold mb-4">
            {editing === 'new' ? 'Create Rule' : 'Edit Rule'}
          </h3>
          <div className="grid grid-cols-2 gap-4 text-sm">
            <Input label="Name" value={form.name} onChange={(v) => setForm({ ...form, name: v })} />
            <Input label="Description" value={form.description} onChange={(v) => setForm({ ...form, description: v })} />

            <div className="col-span-2 text-xs text-gray-500 font-semibold mt-2">CONDITIONS (leave blank to skip)</div>

            <Input label="Match Action Type" value={form.match_action_type} onChange={(v) => setForm({ ...form, match_action_type: v })} placeholder="send_message, reminder, etc." />
            <Input label="Match Priority" value={form.match_priority} onChange={(v) => setForm({ ...form, match_priority: v })} placeholder="low, medium, high, urgent" />
            <Input label="Match Keyword" value={form.match_keyword} onChange={(v) => setForm({ ...form, match_keyword: v })} placeholder="word in title/description" />
            <Input label="Match Assigned To" value={form.match_assigned_to} onChange={(v) => setForm({ ...form, match_assigned_to: v })} />
            <Input label="Match Window" value={form.match_window} onChange={(v) => setForm({ ...form, match_window: v })} placeholder="chrome.exe, whatsapp, etc." />

            <div className="col-span-2 text-xs text-gray-500 font-semibold mt-2">ACTIONS</div>

            <Toggle label="Auto-Approve" checked={form.auto_approve} onChange={(v) => setForm({ ...form, auto_approve: v })} />
            <Toggle label="Notify Telegram" checked={form.notify_telegram} onChange={(v) => setForm({ ...form, notify_telegram: v })} />
            <Input label="Override Priority" value={form.override_priority} onChange={(v) => setForm({ ...form, override_priority: v })} placeholder="Leave blank to keep original" />
            <Input label="Override Assigned To" value={form.override_assigned_to} onChange={(v) => setForm({ ...form, override_assigned_to: v })} />
          </div>
          <div className="flex gap-2 mt-4">
            <button onClick={save} className="px-4 py-2 bg-green-600 hover:bg-green-500 text-white text-sm rounded">Save</button>
            <button onClick={() => setEditing(null)} className="px-4 py-2 bg-gray-700 hover:bg-gray-600 text-white text-sm rounded">Cancel</button>
          </div>
        </div>
      )}

      {/* Rules list */}
      {data.length === 0 ? (
        <div className="bg-gray-900 border border-gray-800 rounded-xl p-10 text-center text-gray-500">
          No rules yet. Create one to automate task handling.
        </div>
      ) : (
        <div className="space-y-3">
          {data.map((rule) => (
            <div key={rule.id} className={`bg-gray-900 border rounded-xl p-4 ${rule.enabled ? 'border-gray-800' : 'border-gray-800/50 opacity-60'}`}>
              <div className="flex items-start justify-between">
                <div>
                  <h3 className="text-sm font-semibold text-gray-200">{rule.name}</h3>
                  {rule.description && <p className="text-xs text-gray-500 mt-1">{rule.description}</p>}
                  <div className="flex flex-wrap gap-2 mt-2">
                    {rule.match_action_type && <Tag label="action" value={rule.match_action_type} />}
                    {rule.match_priority && <Tag label="priority" value={rule.match_priority} />}
                    {rule.match_keyword && <Tag label="keyword" value={rule.match_keyword} />}
                    {rule.match_assigned_to && <Tag label="assigned" value={rule.match_assigned_to} />}
                    {rule.auto_approve && <span className="text-xs bg-green-900/50 text-green-400 px-2 py-0.5 rounded">Auto-Approve</span>}
                    {rule.notify_telegram && <span className="text-xs bg-blue-900/50 text-blue-400 px-2 py-0.5 rounded">Notify TG</span>}
                  </div>
                </div>
                <div className="flex gap-2 shrink-0 ml-4">
                  <button onClick={() => handleToggle(rule.id)} className={`px-2 py-1 text-xs rounded ${rule.enabled ? 'bg-green-900/50 text-green-400' : 'bg-gray-800 text-gray-500'}`}>
                    {rule.enabled ? 'ON' : 'OFF'}
                  </button>
                  <button onClick={() => startEdit(rule)} className="px-2 py-1 text-xs bg-gray-800 text-gray-400 rounded hover:bg-gray-700">Edit</button>
                  <button onClick={() => handleDelete(rule.id)} className="px-2 py-1 text-xs bg-red-900/50 text-red-400 rounded hover:bg-red-900">Del</button>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function Input({ label, value, onChange, placeholder = '' }) {
  return (
    <label className="block">
      <span className="text-xs text-gray-500">{label}</span>
      <input
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className="mt-1 w-full bg-gray-800 border border-gray-700 rounded px-3 py-1.5 text-sm text-gray-200 focus:outline-none focus:border-blue-500"
      />
    </label>
  );
}

function Toggle({ label, checked, onChange }) {
  return (
    <label className="flex items-center gap-2 cursor-pointer">
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        className="accent-blue-500"
      />
      <span className="text-xs text-gray-400">{label}</span>
    </label>
  );
}

function Tag({ label, value }) {
  return (
    <span className="text-xs bg-gray-800 text-gray-400 px-2 py-0.5 rounded">
      {label}: {value}
    </span>
  );
}
