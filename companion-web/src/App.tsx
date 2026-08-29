import { useEffect } from 'react'
import { Routes, Route, Navigate, useLocation, useNavigate } from 'react-router-dom'
import CompanionSocket, { socket } from './api/socket'
import { tokens } from './ui/theme'
import DesktopsScreen from './screens/Desktops'
import ProjectsScreen from './screens/Projects'
import ChatScreen from './screens/Chat'
import LoginScreen from './screens/Login'

const navItems = [
  { path: '/chat', label: 'Command', icon: '◐' },
  { path: '/projects', label: 'Switch', icon: '▤' },
];

function BottomNav() {
  const location = useLocation();
  const navigate = useNavigate();
  const currentPath = '/' + location.pathname.split('/')[1];

  return (
    <nav style={{
      display: 'flex',
      justifyContent: 'space-around',
      padding: '0.4rem 0.5rem env(safe-area-inset-bottom, 0.4rem)',
      borderTop: `1px solid ${tokens.border}`,
      background: 'rgba(10, 13, 20, 0.72)',
      backdropFilter: 'blur(20px) saturate(160%)',
      WebkitBackdropFilter: 'blur(20px) saturate(160%)',
    }}>
      {navItems.map((item) => {
        const active = currentPath === item.path;
        return (
          <button
            key={item.path}
            onClick={() => navigate(item.path)}
            style={{
              background: 'transparent',
              border: 'none',
              color: active ? tokens.accent : tokens.fgMuted,
              fontSize: '0.7rem',
              display: 'flex',
              flexDirection: 'column',
              alignItems: 'center',
              gap: 2,
              padding: '0.35rem 0.6rem',
              fontWeight: active ? 600 : 500,
              transition: 'color 120ms ease',
              cursor: 'pointer',
            }}
          >
            <span style={{ fontSize: '1.15rem', lineHeight: 1 }}>{item.icon}</span>
            <span>{item.label}</span>
          </button>
        );
      })}
    </nav>
  );
}

function AppLayout() {
  const location = useLocation();
  const navigate = useNavigate();

  // QR / ticket auto-fill flow: if URL has pair params, always land on Login.
  const search = window.location.search;
  const isPairRoute = location.pathname.startsWith('/pair');
  const hasPairParams = search.includes('code=') || search.includes('ticket=') || isPairRoute;
  const showNav = location.pathname !== '/login' && location.pathname !== '/pair';
  const isPaired = CompanionSocket.isPaired();
  const safeCtx = CompanionSocket.getStoredSafeContext();
  const storedDesktopId = sessionStorage.getItem('companion_desktop_id') || safeCtx.desktop_id || '';
  const pairedDefaultRoute = storedDesktopId ? '/chat' : '/desktops';
  const pairTarget = { pathname: isPairRoute ? '/pair' : '/login', search: location.search };
  const defaultRoute = hasPairParams ? pairTarget : (isPaired ? pairedDefaultRoute : '/login');

  // Connection guard
  useEffect(() => {
    if (location.pathname === '/login' || location.pathname === '/pair') return;
    if (!isPaired) {
      navigate('/login', { replace: true });
      return;
    }
    if (!socket.connected && !socket.connecting) {
      if (!storedDesktopId && location.pathname !== '/desktops') {
        navigate('/desktops', { replace: true });
      }
    }
  }, [location.pathname, navigate, isPaired, storedDesktopId]);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100dvh' }}>
      <div style={{ flex: 1, overflow: 'hidden', display: 'flex', flexDirection: 'column' }}>
        <Routes>
          <Route path="/pair" element={<LoginScreen />} />
          <Route path="/login" element={<LoginScreen />} />
          <Route path="/desktops" element={<DesktopsScreen />} />
          <Route path="/projects" element={<ProjectsScreen />} />
          <Route path="/chat/:threadId?" element={<ChatScreen />} />
          <Route path="/" element={<Navigate to={defaultRoute} replace />} />
          <Route path="*" element={<Navigate to={defaultRoute} replace />} />
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
