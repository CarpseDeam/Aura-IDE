import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import CompanionSocket, { socket } from '../api/socket';

function LoginScreen() {
  const navigate = useNavigate();
  const [relayUrl, setRelayUrl] = useState('ws://localhost:8765');
  const [pairingCode, setPairingCode] = useState('');
  const [deviceName, setDeviceName] = useState('Aura Companion');
  const [status, setStatus] = useState<'idle'|'connecting'|'connected'|'pairing'|'paired'|'error'>('idle');
  const [error, setError] = useState('');
  const [alreadyPaired, setAlreadyPaired] = useState(CompanionSocket.isPaired());

  const handleConnect = () => {
    setStatus('connecting');
    setError('');
    socket.connect(relayUrl);
    const unsub = socket.on('welcome', () => {
      unsub();
      setStatus('connected');
    });
    setTimeout(() => {
      setStatus((s) => (s === 'connecting' ? 'idle' : s));
      setError((s) => (s === '' ? 'Connection timed out' : s));
    }, 10000);
  };

  const handlePair = async () => {
    if (!pairingCode) {
      setError('Please enter a pairing code');
      return;
    }
    setStatus('pairing');
    setError('');
    try {
      await socket.pair(pairingCode, relayUrl, '', deviceName);
      setStatus('paired');
      navigate('/desktops');
    } catch (e: any) {
      setStatus('connected');
      setError(e.message || 'Pairing failed');
    }
  };

  const handleReconnect = () => {
    setStatus('connecting');
    socket.connect(relayUrl);
    const unsub = socket.on('welcome', () => {
      unsub();
      navigate('/desktops');
    });
  };

  const handleClearPairing = () => {
    socket.logout();
    CompanionSocket.setStoredToken('');
    setAlreadyPaired(false);
  };

  // Already paired screen
  if (alreadyPaired) {
    return (
      <div style={{ padding: '2rem', maxWidth: '400px', margin: '0 auto', marginTop: '2rem', textAlign: 'center' }}>
        <h1 style={{ fontSize: '1.5rem', marginBottom: '0.5rem' }}>Aura Companion</h1>
        <p style={{ color: '#888', marginBottom: '2rem' }}>Already paired with a desktop</p>
        <button
          onClick={handleReconnect}
          style={{
            width: '100%', padding: '0.75rem', background: '#6c5ce7', border: 'none',
            borderRadius: '8px', color: '#fff', fontSize: '1rem', fontWeight: 600, cursor: 'pointer',
            marginBottom: '0.75rem',
          }}
        >
          Reconnect
        </button>
        <button
          onClick={handleClearPairing}
          style={{
            width: '100%', padding: '0.75rem', background: 'transparent', border: '1px solid #555',
            borderRadius: '8px', color: '#aaa', fontSize: '1rem', cursor: 'pointer',
          }}
        >
          Pair Different Device
        </button>
      </div>
    );
  }

  return (
    <div style={{ padding: '2rem', maxWidth: '400px', margin: '0 auto', marginTop: '2rem' }}>
      <h1 style={{ fontSize: '1.5rem', marginBottom: '0.5rem' }}>Aura Companion</h1>
      <p style={{ color: '#888', marginBottom: '2rem' }}>
        {status === 'connected'
          ? 'Connected to relay. Enter your pairing code.'
          : 'Connect to your relay server, then pair with a desktop.'}
      </p>

      {error && (
        <div style={{
          padding: '0.5rem 0.75rem', background: '#e1705533', color: '#e17055',
          borderRadius: '8px', marginBottom: '1rem', fontSize: '0.85rem',
        }}>
          {error}
        </div>
      )}

      {/* Connection phase: shown when not yet connected */}
      {status !== 'connected' && status !== 'pairing' && (
        <>
          <div style={{ marginBottom: '1rem' }}>
            <label style={{ display: 'block', marginBottom: '0.25rem', color: '#aaa' }}>Relay URL</label>
            <input
              value={relayUrl}
              onChange={(e) => setRelayUrl(e.target.value)}
              placeholder="ws://localhost:8765"
              style={{
                width: '100%', padding: '0.75rem', background: '#1e1e32', border: '1px solid #333',
                borderRadius: '8px', color: '#e0e0e0', fontSize: '1rem', outline: 'none',
              }}
              disabled={status === 'connecting'}
            />
          </div>
          <button
            onClick={handleConnect}
            disabled={status === 'connecting'}
            style={{
              width: '100%', padding: '0.75rem',
              background: status === 'connecting' ? '#444' : '#6c5ce7',
              border: 'none', borderRadius: '8px', color: '#fff', fontSize: '1rem',
              fontWeight: 600, cursor: status === 'connecting' ? 'not-allowed' : 'pointer',
            }}
          >
            {status === 'connecting' ? 'Connecting...' : 'Connect'}
          </button>
        </>
      )}

      {/* Pairing phase: shown after connected */}
      {status === 'connected' && (
        <>
          <div style={{ marginBottom: '1rem' }}>
            <label style={{ display: 'block', marginBottom: '0.25rem', color: '#aaa' }}>Pairing Code</label>
            <input
              value={pairingCode}
              onChange={(e) => setPairingCode(e.target.value.toUpperCase().slice(0, 6))}
              placeholder="ABC123"
              style={{
                width: '100%', padding: '0.75rem', background: '#1e1e32', border: '1px solid #333',
                borderRadius: '8px', color: '#e0e0e0', fontSize: '1.5rem',
                textAlign: 'center', letterSpacing: '0.5rem', outline: 'none',
              }}
              maxLength={6}
            />
          </div>
          <div style={{ marginBottom: '1.5rem' }}>
            <label style={{ display: 'block', marginBottom: '0.25rem', color: '#aaa' }}>Device Name</label>
            <input
              value={deviceName}
              onChange={(e) => setDeviceName(e.target.value)}
              style={{
                width: '100%', padding: '0.75rem', background: '#1e1e32', border: '1px solid #333',
                borderRadius: '8px', color: '#e0e0e0', fontSize: '1rem', outline: 'none',
              }}
            />
          </div>
          <button
            onClick={handlePair}
            style={{
              width: '100%', padding: '0.75rem', background: '#6c5ce7', border: 'none',
              borderRadius: '8px', color: '#fff', fontSize: '1rem', fontWeight: 600, cursor: 'pointer',
            }}
          >
            Pair
          </button>
        </>
      )}

      {/* Pairing progress */}
      {status === 'pairing' && (
        <div style={{ textAlign: 'center', marginTop: '1rem', color: '#aaa' }}>
          <p>Pairing...</p>
        </div>
      )}
    </div>
  );
}

export default LoginScreen;
