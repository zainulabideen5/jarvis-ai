import { useCallback } from 'react';
import { api } from '../api';
import { usePolling } from '../hooks/usePolling';

export default function CalendarPage() {
  const fetchEvents = useCallback(() => api.getEvents(), []);
  const { data, loading, error, refresh } = usePolling(fetchEvents, 10000);

  const handleConfirm = async (id) => {
    await api.confirmEvent(id);
    refresh();
  };

  const handleDelete = async (id) => {
    if (!confirm('Delete this event?')) return;
    await api.deleteEvent(id);
    refresh();
  };

  if (loading) return <p className="text-gray-500">Loading...</p>;
  if (error) return <p className="text-red-400">Error: {error}</p>;

  // Group events by date
  const grouped = {};
  for (const event of (data || [])) {
    const date = event.event_date || 'No Date';
    if (!grouped[date]) grouped[date] = [];
    grouped[date].push(event);
  }

  const today = new Date().toISOString().split('T')[0];

  return (
    <div>
      <h2 className="text-2xl font-bold mb-6">Calendar</h2>

      {Object.keys(grouped).length === 0 ? (
        <div className="bg-gray-900 border border-gray-800 rounded-xl p-10 text-center text-gray-500">
          No events yet. Jab call pe koi date/meeting mention karega — yahan automatically aayega.
        </div>
      ) : (
        <div className="space-y-6">
          {Object.entries(grouped).sort().map(([date, events]) => {
            const isToday = date === today;
            const isPast = date < today;
            const dateObj = new Date(date + 'T00:00:00');
            const dayName = dateObj.toLocaleDateString('en-US', { weekday: 'long' });
            const dateFormatted = dateObj.toLocaleDateString('en-US', { day: 'numeric', month: 'short', year: 'numeric' });

            return (
              <div key={date}>
                <div className="flex items-center gap-3 mb-3">
                  <h3 className={`text-sm font-semibold ${isToday ? 'text-blue-400' : isPast ? 'text-gray-600' : 'text-gray-300'}`}>
                    {isToday ? 'Today' : dayName} — {dateFormatted}
                  </h3>
                  {isToday && <span className="text-xs bg-blue-600 text-white px-2 py-0.5 rounded">Today</span>}
                </div>

                <div className="space-y-2">
                  {events.map((event) => (
                    <div key={event.id} className={`bg-gray-900 border rounded-xl p-4 ${
                      event.status === 'confirmed' ? 'border-green-800/50' : 'border-gray-800'
                    }`}>
                      <div className="flex items-start justify-between">
                        <div className="flex-1">
                          <div className="flex items-center gap-2">
                            {event.event_time && (
                              <span className="text-sm font-mono text-blue-400">{event.event_time}</span>
                            )}
                            <h4 className="text-sm font-semibold text-gray-200">{event.title}</h4>
                            <span className={`text-xs px-2 py-0.5 rounded ${
                              event.status === 'confirmed' ? 'bg-green-900/50 text-green-400' : 'bg-yellow-900/50 text-yellow-400'
                            }`}>
                              {event.status}
                            </span>
                          </div>

                          <div className="flex items-center gap-4 mt-2 text-xs text-gray-500">
                            {event.duration_minutes && <span>{event.duration_minutes} min</span>}
                            {event.attendees?.length > 0 && (
                              <span>Attendees: {event.attendees.join(', ')}</span>
                            )}
                            {event.location && <span>Location: {event.location}</span>}
                          </div>

                          {event.notes && (
                            <p className="text-xs text-gray-600 mt-1">{event.notes}</p>
                          )}
                        </div>

                        <div className="flex gap-2 shrink-0 ml-4">
                          {event.status === 'pending' && (
                            <button onClick={() => handleConfirm(event.id)}
                              className="px-3 py-1 bg-green-600 hover:bg-green-500 text-white text-xs rounded">
                              Confirm
                            </button>
                          )}
                          <button onClick={() => handleDelete(event.id)}
                            className="px-3 py-1 bg-red-900/50 text-red-400 text-xs rounded hover:bg-red-900">
                            Delete
                          </button>
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
