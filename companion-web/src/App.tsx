import { useEffect } from 'react'
import { Routes, Route, Navigate, useLocation, useNavigate } from 'react-router-dom'
import { socket } from './api/socket'
import DesktopsScreen from './screens/Desktops'
import ProjectsScreen from './screens/Projects'
import ChatScreen from './screens/Chat'
import RunsScreen from './screens/Runs'
import ReceiptsScreen from './screens/Receipts'
import LoginScreen from './screens/Login'

const navItems = [
  { path: '/desktops', label: 'Desktops', icon: '🖥️' },
  { path: '/projects', label: 'Projects', icon: '📁' },
  { path: '/chat', label: 'Chat', icon: '💬' },
  { path: '/runs', label: 'Runs', icon: '⚡' },
  { path: '/receipts', label: 'History', icon: '📋' },
];

function BottomNav() {
  const location = useLocation();
  const navigate = useNavigate();
  const currentPath = '/' + location.pathname.split('/')[1];

  return (
    <div style={{
      display: 'flex',
      justifyContent: 'space-around',
      padding: '0.5rem 0',
      borderTop: '1px solid #222',
      background: '#0f0f1a',
    }}>
      {navItems.map((item) => (
        <button
          key={item.path}
          onClick={() => navigate(item.path)}
          style={{
            background: 'transparent',
            border: 'none',
            color: currentPath === item.path ? '#6c5ce7' : '#666',
            fontSize: '0.75rem',
            cursor: 'pointer',
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            gap: '0.2rem',
            padding: '0.25rem 0.5rem',
            opacity: currentPath === item.path ? 1 : 0.6,
          }}
        >
          <span style={{ fontSize: '1.1rem' }}>{item.icon}</span>
          <span>{item.label}</span>
        </button>
      ))}
    </div>
  );
}

function AppLayout() {
  const location = useLocation();
  const navigate = useNavigate();
  // Hide bottom nav on login screen
  const showNav = location.pathname !== '/login';

  // Connection guard: redirect to /login if socket is fully disconnected
  useEffect(() => {
    if (location.pathname !== '/login' && !socket.connected && !socket.connecting) {
      navigate('/login', { replace: true });
    }
  }, [location.pathname, navigate]);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100dvh' }}>
      <div style={{ flex: 1, overflow: 'auto' }}>
        <Routes>
          <Route path="/login" element={<LoginScreen />} />
          <Route path="/desktops" element={<DesktopsScreen />} />
          <Route path="/projects" element={<ProjectsScreen />} />
          <Route path="/chat/:threadId?" element={<ChatScreen />} />
          <Route path="/runs" element={<RunsScreen />} />
          <Route path="/receipts" element={<ReceiptsScreen />} />
          <Route path="*" element={<Navigate to="/desktops" replace />} />
        </Routes>
      </div>
      {showNav && <BottomNav />}
    </div>
  );
}

function App() {
  return <AppLayout />;
}

export default App;
