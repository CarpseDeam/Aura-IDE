import { useState, useEffect, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import CompanionSocket, { socket } from '../api/socket';

interface Desktop {
  device_id: string;
  display_name: string;
  device_type: string;
  last_seen: string;
}

function DesktopsScreen() {
  const navigate = useNavigate();

  // Require pairing to access this screen
  if (!CompanionSocket.isPaired() && !socket.connected) {
    return (
      <div style={{ padding: 20, textAlign: 'center' }}>
        <h2>Not Paired</h2>
        <p>Please pair with a desktop first.</p>
        <button
          onClick={() => navigate('/login')}
          style={{
            padding: '10px 20px', background: '#4f46e5', color: 'white',
            border: 'none', borderRadius: 8, cursor: 'pointer',
          }}
        >
          Go to Login
        </button>
      </div>
    );
  }

  const [desktops, setDesktops] = useState<Desktop[]>([]);
  const [status, setStatus] = useState<'connecting' | 'connected' | 'disconnected'>('connecting');
  const [selectedDesktop, setSelectedDesktop] = useState<string>('');

  useEffect(() => {
    // Set initial connection status
    setStatus(socket.connected ? 'connected' : 'disconnected');

    // Listen for online list — broadcast on every connect/disconnect
    const unsubOnline = socket.on('system.online_list', (msg: any) => {
      const devices = msg.payload?.devices || [];
      setDesktops(devices.filter((d: any) => d.device_type === 'desktop'));
    });

    // Listen for welcome
    const unsubWelcome = socket.on('welcome', () => {
      setStatus('connected');
    });

    // Listen for error
    const unsubError = socket.on('error', (msg: any) => {
      console.warn('[Desktops] Relay error:', msg.payload?.message);
    });

    return () => {
      unsubOnline();
      unsubWelcome();
      unsubError();
    };
  }, []);

  const selectDesktop = useCallback((d: Desktop) => {
    setSelectedDesktop(d.device_id);
    // Store desktop_id in sessionStorage for other screens
    sessionStorage.setItem('companion_desktop_id', d.device_id);
    sessionStorage.setItem('companion_desktop_name', d.display_name);
    navigate('/chat');
  }, [navigate]);

  return (
    <div style={{ padding: '1rem' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1rem' }}>
        <h1 style={{ fontSize: '1.25rem' }}>Aura Desktop</h1>
        <span style={{
          fontSize: '0.8rem',
          padding: '0.25rem 0.5rem',
          borderRadius: '12px',
          background: status === 'connected' ? '#00b89433' : status === 'connecting' ? '#fdcb6e33' : '#e1705533',
          color: status === 'connected' ? '#00b894' : status === 'connecting' ? '#fdcb6e' : '#e17055',
        }}>
          {status === 'connected' ? '● Connected' : status === 'connecting' ? '● Connecting' : '● Disconnected'}
        </span>
      </div>

      {!socket.connected ? (
        <div style={{ textAlign: 'center', marginTop: '3rem', padding: '1rem' }}>
          <p style={{ fontSize: '1.1rem', marginBottom: '0.5rem', color: '#e17055' }}>
            Not connected
          </p>
          <p style={{ fontSize: '0.85rem', color: '#888', marginBottom: '1.5rem' }}>
            Connect from the Login screen to see your desktops.
          </p>
          <button
            onClick={() => navigate('/login')}
            style={{
              padding: '0.75rem 2rem',
              background: '#6c5ce7',
              border: 'none',
              borderRadius: '8px',
              color: '#fff',
              fontSize: '1rem',
              cursor: 'pointer',
            }}
          >
            Go to Login
          </button>
        </div>
      ) : desktops.length === 0 ? (
        <div style={{ textAlign: 'center', marginTop: '3rem', color: '#666' }}>
          <p style={{ fontSize: '1.1rem', marginBottom: '0.5rem' }}>
            No desktops found
          </p>
          <p style={{ fontSize: '0.85rem' }}>
            Open Aura Desktop and enable Companion in settings.
          </p>
        </div>
      ) : (
        desktops.map((d) => (
          <div
            key={d.device_id}
            onClick={() => selectDesktop(d)}
            style={{
              padding: '1rem',
              background: selectedDesktop === d.device_id ? '#2a2a45' : '#1e1e32',
              borderRadius: '12px',
              marginBottom: '0.75rem',
              cursor: 'pointer',
              border: selectedDesktop === d.device_id ? '1px solid #6c5ce7' : '1px solid transparent',
              transition: 'all 0.2s',
            }}
          >
            <div style={{ fontWeight: 600, fontSize: '1.1rem' }}>{d.display_name}</div>
            <div style={{ fontSize: '0.8rem', color: '#888', marginTop: '0.25rem' }}>
              🖥️ Desktop · {d.device_id?.slice(0, 12)}
            </div>
            <div style={{ fontSize: '0.75rem', color: '#555', marginTop: '0.25rem' }}>
              Last seen: {d.last_seen || 'just now'}
            </div>
          </div>
        ))
      )}
    </div>
  );
}

export default DesktopsScreen;
