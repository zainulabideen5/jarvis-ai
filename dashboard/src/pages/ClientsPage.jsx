import { useState, useCallback } from 'react';
import { api } from '../api';
import { usePolling } from '../hooks/usePolling';

const EMPTY_CLIENT = {
  name: '', company: '', role: '', email: '', phone: '',
  website: '', logo_url: '', notes: '', speaker_label: '',
};

export default function ClientsPage() {
  const fetchClients = useCallback(() => api.getClients(), []);
  const { data, loading, error, refresh } = usePolling(fetchClients, 10000);
  const [editing, setEditing] = useState(null);
  const [form, setForm] = useState(EMPTY_CLIENT);
  const [detail, setDetail] = useState(null);

  const startNew = () => { setForm({ ...EMPTY_CLIENT }); setEditing('new'); setDetail(null); };
  const startEdit = (c) => {
    setForm(Object.fromEntries(Object.entries(EMPTY_CLIENT).map(([k]) => [k, c[k] ?? ''])));
    setEditing(c.id);
    setDetail(null);
  };

  const save = async () => {
    if (!form.name.trim()) return alert('Name required');
    try {
      if (editing === 'new') await api.createClient(form);
      else await api.updateClient(editing, form);
      setEditing(null);
      refresh();
    } catch (err) { alert(`Failed: ${err.message}`); }
  };

  const handleDelete = async (id) => {
    if (!confirm('Delete this client?')) return;
    await api.deleteClient(id);
    refresh();
  };

  const viewDetail = async (id) => {
    try {
      const d = await api.getClient(id);
      setDetail(d);
      setEditing(null);
    } catch (err) { alert(`Failed: ${err.message}`); }
  };

  if (loading) return <p className="text-gray-500">Loading...</p>;
  if (error) return <p className="text-red-400">Error: {error}</p>;

  // Detail view
  if (detail) return (
    <div>
      <button onClick={() => setDetail(null)} className="text-sm text-blue-400 mb-4 hover:underline">&larr; Back to all clients</button>
      <div className="bg-gray-900 border border-gray-800 rounded-xl p-6 mb-6">
        <div className="flex items-start gap-4">
          {detail.logo_url && <img src={detail.logo_url} alt="" className="w-16 h-16 rounded-lg object-cover" />}
          <div className="flex-1">
            <h2 className="text-2xl font-bold text-white">{detail.name}</h2>
            {detail.role && <p className="text-sm text-gray-400">{detail.role}</p>}
            {detail.company && <p className="text-sm text-blue-400">{detail.company}</p>}
          </div>
          {detail.speaker_label && (
            <span className="text-xs bg-purple-900/50 text-purple-400 px-3 py-1 rounded">Voice: {detail.speaker_label}</span>
          )}
        </div>
        <div className="grid grid-cols-2 gap-4 mt-4 text-sm">
          {detail.email && <Info label="Email" value={detail.email} />}
          {detail.phone && <Info label="Phone" value={detail.phone} />}
          {detail.website && <Info label="Website" value={<a href={detail.website.startsWith('http') ? detail.website : `https://${detail.website}`} target="_blank" className="text-blue-400 hover:underline">{detail.website}</a>} />}
          <Info label="Total Conversations" value={detail.total_conversations} />
          <Info label="Total Tasks" value={detail.total_tasks} />
          {detail.last_contact && <Info label="Last Contact" value={new Date(detail.last_contact).toLocaleString()} />}
        </div>
        {detail.notes && <div className="mt-4 p-3 bg-gray-950 rounded text-sm text-gray-400">{detail.notes}</div>}
      </div>

      {/* Client's tasks */}
      <h3 className="text-lg font-semibold mb-3">Tasks ({detail.tasks?.length || 0})</h3>
      {detail.tasks?.length > 0 ? (
        <div className="space-y-2 mb-6">
          {detail.tasks.map((t) => (
            <div key={t.id} className="bg-gray-900 border border-gray-800 rounded-lg p-3 flex items-center justify-between">
              <div>
                <span className="text-sm text-gray-200">{t.title}</span>
                <span className="text-xs text-gray-600 ml-2">{t.action_type}</span>
              </div>
              <span className={`text-xs px-2 py-0.5 rounded ${t.status === 'completed' ? 'bg-green-900/50 text-green-400' : t.status === 'pending' ? 'bg-yellow-900/50 text-yellow-400' : 'bg-gray-800 text-gray-500'}`}>
                {t.status}
              </span>
            </div>
          ))}
        </div>
      ) : <p className="text-sm text-gray-600 mb-6">No tasks yet</p>}

      {/* Client's transcriptions */}
      <h3 className="text-lg font-semibold mb-3">Conversations ({detail.transcriptions?.length || 0})</h3>
      {detail.transcriptions?.length > 0 ? (
        <div className="space-y-2">
          {detail.transcriptions.map((t) => (
            <div key={t.id} className="bg-gray-900 border border-gray-800 rounded-lg p-3">
              <p className="text-sm text-gray-300">{t.text}</p>
              <p className="text-xs text-gray-600 mt-1">{t.created_at ? new Date(t.created_at).toLocaleString() : ''}</p>
            </div>
          ))}
        </div>
      ) : <p className="text-sm text-gray-600">No conversations yet</p>}
    </div>
  );

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h2 className="text-2xl font-bold">Clients</h2>
        <button onClick={startNew} className="px-4 py-2 bg-blue-600 hover:bg-blue-500 text-white text-sm rounded transition-colors">+ Add Client</button>
      </div>

      {/* Editor */}
      {editing !== null && (
        <div className="bg-gray-900 border border-blue-800 rounded-xl p-5 mb-6">
          <h3 className="text-lg font-semibold mb-4">{editing === 'new' ? 'Add New Client' : 'Edit Client'}</h3>
          <div className="grid grid-cols-2 gap-4 text-sm">
            <Input label="Name *" value={form.name} onChange={(v) => setForm({ ...form, name: v })} placeholder="Client ka naam" />
            <Input label="Company" value={form.company} onChange={(v) => setForm({ ...form, company: v })} placeholder="Company name" />
            <Input label="Role / Designation" value={form.role} onChange={(v) => setForm({ ...form, role: v })} placeholder="CEO, Manager, etc." />
            <Input label="Email" value={form.email} onChange={(v) => setForm({ ...form, email: v })} placeholder="client@example.com" />
            <Input label="Phone" value={form.phone} onChange={(v) => setForm({ ...form, phone: v })} placeholder="+92 300 1234567" />
            <Input label="Website" value={form.website} onChange={(v) => setForm({ ...form, website: v })} placeholder="example.com" />
            <Input label="Logo URL" value={form.logo_url} onChange={(v) => setForm({ ...form, logo_url: v })} placeholder="https://..." />
            <Input label="Speaker Label" value={form.speaker_label} onChange={(v) => setForm({ ...form, speaker_label: v })} placeholder="SPEAKER_00 (from diarization)" />
            <label className="col-span-2 block">
              <span className="text-xs text-gray-500">Notes</span>
              <textarea value={form.notes} onChange={(e) => setForm({ ...form, notes: e.target.value })} rows={2} placeholder="Project details, preferences..." className="mt-1 w-full bg-gray-800 border border-gray-700 rounded px-3 py-1.5 text-sm text-gray-200 focus:outline-none focus:border-blue-500" />
            </label>
          </div>
          <div className="flex gap-2 mt-4">
            <button onClick={save} className="px-4 py-2 bg-green-600 hover:bg-green-500 text-white text-sm rounded">Save</button>
            <button onClick={() => setEditing(null)} className="px-4 py-2 bg-gray-700 hover:bg-gray-600 text-white text-sm rounded">Cancel</button>
          </div>
        </div>
      )}

      {/* Client cards */}
      {data.length === 0 ? (
        <div className="bg-gray-900 border border-gray-800 rounded-xl p-10 text-center text-gray-500">
          No clients yet. Add your first client to start tracking.
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {data.map((c) => (
            <div key={c.id} className="bg-gray-900 border border-gray-800 rounded-xl p-4 hover:border-gray-700 transition-colors">
              <div className="flex items-center gap-3 mb-3">
                {c.logo_url ? (
                  <img src={c.logo_url} alt="" className="w-10 h-10 rounded-lg object-cover" />
                ) : (
                  <div className="w-10 h-10 rounded-lg bg-blue-900/50 flex items-center justify-center text-blue-400 font-bold text-lg">
                    {c.name.charAt(0).toUpperCase()}
                  </div>
                )}
                <div className="flex-1 min-w-0">
                  <h3 className="text-sm font-semibold text-gray-200 truncate">{c.name}</h3>
                  {c.company && <p className="text-xs text-gray-500 truncate">{c.role ? `${c.role} @ ` : ''}{c.company}</p>}
                </div>
              </div>
              <div className="flex items-center gap-2 text-xs text-gray-600 mb-3">
                {c.email && <span>{c.email}</span>}
                {c.speaker_label && <span className="bg-purple-900/50 text-purple-400 px-1.5 py-0.5 rounded">{c.speaker_label}</span>}
              </div>
              <div className="flex items-center gap-2 text-xs text-gray-600 mb-3">
                <span>{c.total_tasks} tasks</span>
                <span>·</span>
                <span>{c.total_conversations} conversations</span>
              </div>
              <div className="flex gap-2">
                <button onClick={() => viewDetail(c.id)} className="px-3 py-1 bg-blue-600/20 text-blue-400 text-xs rounded hover:bg-blue-600/30">View</button>
                <button onClick={() => startEdit(c)} className="px-3 py-1 bg-gray-800 text-gray-400 text-xs rounded hover:bg-gray-700">Edit</button>
                <button onClick={() => handleDelete(c.id)} className="px-3 py-1 bg-red-900/30 text-red-400 text-xs rounded hover:bg-red-900/50">Delete</button>
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
      <input type="text" value={value} onChange={(e) => onChange(e.target.value)} placeholder={placeholder}
        className="mt-1 w-full bg-gray-800 border border-gray-700 rounded px-3 py-1.5 text-sm text-gray-200 focus:outline-none focus:border-blue-500" />
    </label>
  );
}

function Info({ label, value }) {
  return (
    <div>
      <span className="text-xs text-gray-600">{label}</span>
      <p className="text-gray-300">{value}</p>
    </div>
  );
}
