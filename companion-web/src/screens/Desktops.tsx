import { useState, useEffect, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import CompanionSocket, { socket } from '../api/socket';
import { tokens, glassCard, statusPillStyle, primaryButton } from '../ui/theme';

interface Desktop {
  device_id: string;
  display_name: string;
  device_type: string;
  last_seen: string;
}

function DesktopsScreen() {
  const navigate = useNavigate();
  const isPaired = CompanionSocket.isPaired();

  const [desktops, setDesktops] = useState<Desktop[]>([]);
  const [status, setStatus] = useState<'connecting' | 'connected' | 'disconnected'>(
    socket.connected ? 'connected' : 'disconnected'
  );

  useEffect(() => {
    if (!isPaired && !socket.connected) {
      navigate('/login', { replace: true });
    }
  }, [isPaired, navigate]);

  useEffect(() => {
    const unsubOnline = socket.on('system.online_list', (msg: any) => {
      const devices = msg.payload?.devices || [];
      setDesktops(devices.filter((d: any) => d.device_type === 'desktop'));
    });
    const unsubWelcome = socket.on('welcome', () => setStatus('connected'));
    return () => {
      unsubOnline();
      unsubWelcome();
    };
  }, []);

  const selectDesktop = useCallback((d: Desktop) => {
    sessionStorage.setItem('companion_desktop_id', d.device_id);
    sessionStorage.setItem('companion_desktop_name', d.display_name);
    navigate('/chat', { replace: true });
  }, [navigate]);

  return (
    <div style={{ padding: '1.25rem 1rem' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1.25rem' }}>
        <div>
          <div style={{ fontSize: '0.65rem', color: tokens.accent, letterSpacing: '0.2em', fontWeight: 700 }}>
            AURA
          </div>
          <h1 style={{ fontSize: '1.25rem', fontWeight: 600 }}>Desktops</h1>
        </div>
        <span style={statusPillStyle(status === 'connected' ? 'connected' : status === 'connecting' ? 'connecting' : 'disconnected')}>
          ● {status === 'connected' ? 'Online' : status === 'connecting' ? 'Connecting' : 'Offline'}
        </span>
      </div>

      {!socket.connected ? (
        <div style={{ ...glassCard, padding: '1.5rem', textAlign: 'center' }}>
          <p style={{ color: tokens.fgDim, marginBottom: '1rem' }}>
            Not connected to a relay.
          </p>
          <button onClick={() => navigate('/login')} style={primaryButton}>
            Pair a desktop
          </button>
        </div>
      ) : desktops.length === 0 ? (
        <div style={{ ...glassCard, padding: '1.5rem', textAlign: 'center', color: tokens.fgDim }}>
          <div style={{ fontSize: '1.6rem', color: tokens.accent, opacity: 0.7, marginBottom: '0.5rem' }}>◌</div>
          <div style={{ fontSize: '0.95rem', marginBottom: 6 }}>No desktops online</div>
          <div style={{ fontSize: '0.8rem', color: tokens.fgMuted }}>
            Open Aura Desktop and enable Companion in settings.
          </div>
        </div>
      ) : (
        desktops.map((d) => (
          <div
            key={d.device_id}
            onClick={() => selectDesktop(d)}
            style={{
              ...glassCard,
              padding: '0.95rem 1rem',
              marginBottom: '0.65rem',
            }}
          >
            <div style={{ fontWeight: 600, fontSize: '1rem' }}>{d.display_name}</div>
            <div style={{ fontSize: '0.75rem', color: tokens.fgMuted, marginTop: 4, fontFamily: '"JetBrains Mono", monospace' }}>
              {d.device_id?.slice(0, 18)}…
            </div>
          </div>
        ))
      )}
    </div>
  );
}

export default DesktopsScreen;
