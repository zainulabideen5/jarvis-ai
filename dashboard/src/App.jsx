import { useState, useEffect, useRef } from 'react';
import Sidebar from './components/Sidebar';
import TopBar from './components/TopBar';
import ConsentModal from './components/ConsentModal';
import ChatPage from './pages/ChatPage';
import StatsPage from './pages/StatsPage';
import ActivityPage from './pages/ActivityPage';
import TranscriptionsPage from './pages/TranscriptionsPage';
import TasksPage from './pages/TasksPage';
import RulesPage from './pages/RulesPage';
import MeetingsPage from './pages/MeetingsPage';
import CalendarPage from './pages/CalendarPage';
import ChunksPage from './pages/ChunksPage';
import ClientsPage from './pages/ClientsPage';
import AgentsPage from './pages/AgentsPage';
import SettingsPage from './pages/SettingsPage';
import { api } from './api';

const PAGES = {
  chat: ChatPage,
  stats: StatsPage,
  activity: ActivityPage,
  transcriptions: TranscriptionsPage,
  tasks: TasksPage,
  meetings: MeetingsPage,
  calendar: CalendarPage,
  clients: ClientsPage,
  rules: RulesPage,
  chunks: ChunksPage,
  agents: AgentsPage,
  settings: SettingsPage,
};

function playNotificationSound() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.frequency.value = 800;
    gain.gain.value = 0.3;
    osc.start();
    osc.stop(ctx.currentTime + 0.15);
    setTimeout(() => {
      const osc2 = ctx.createOscillator();
      const gain2 = ctx.createGain();
      osc2.connect(gain2);
      gain2.connect(ctx.destination);
      osc2.frequency.value = 1000;
      gain2.gain.value = 0.3;
      osc2.start();
      osc2.stop(ctx.currentTime + 0.15);
    }, 200);
  } catch (e) {}
}

export default function App() {
  const [page, setPage] = useState('chat');
  const [unreadChat, setUnreadChat] = useState(0);
  const lastChatCountRef = useRef(0);
  const Page = PAGES[page];

  // Ask notification permission on mount
  useEffect(() => {
    if ('Notification' in window && Notification.permission === 'default') {
      Notification.requestPermission();
    }
  }, []);

  // Global chat notification poller — runs on ALL pages
  useEffect(() => {
    const checkNewMessages = () => {
      api.getChatHistory().then((history) => {
        if (lastChatCountRef.current > 0 && history.length > lastChatCountRef.current) {
          const newMessages = history.slice(lastChatCountRef.current);
          const assistantMsgs = newMessages.filter((m) => m.role === 'assistant');

          if (assistantMsgs.length > 0) {
            const latest = assistantMsgs[assistantMsgs.length - 1];

            // Sound
            playNotificationSound();

            // Browser notification
            if (Notification.permission === 'granted') {
              new Notification('AI Assistant', {
                body: latest.content.slice(0, 120),
                icon: '/favicon.svg',
              });
            }

            // Tab title flash
            document.title = '(New Message)';
            setTimeout(() => { document.title = 'AI Assistant Dashboard'; }, 5000);

            // Badge count (if not on chat page)
            if (page !== 'chat') {
              setUnreadChat((prev) => prev + assistantMsgs.length);
            }
          }
        }
        lastChatCountRef.current = history.length;
      }).catch(() => {});
    };

    checkNewMessages();
    const id = setInterval(checkNewMessages, 5000);
    return () => clearInterval(id);
  }, [page]);

  // Clear unread when opening chat
  const handleNavigate = (newPage) => {
    if (newPage === 'chat') setUnreadChat(0);
    setPage(newPage);
  };

  return (
    <div className="flex h-screen text-gray-100 relative">
      {/* Global scan line */}
      <div className="j-scan-line" />

      {/* Consent modal — first run */}
      <ConsentModal />

      <Sidebar activePage={page} onNavigate={handleNavigate} unreadChat={unreadChat} />
      <div className="flex-1 flex flex-col overflow-hidden">
        <TopBar activePage={page} onNavigate={handleNavigate} />
        <main className="flex-1 overflow-y-auto p-6 relative">
          <div className="max-w-[1600px] mx-auto">
            <Page />
          </div>
        </main>
      </div>
    </div>
  );
}
