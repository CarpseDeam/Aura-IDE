import { useState, useEffect, useRef, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import CompanionSocket, { socket } from '../api/socket';

interface Message {
  id: string;
  role: 'user' | 'assistant';
  text: string;
  final?: boolean;
}

function ChatScreen() {
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

  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState('');
  const [streaming, setStreaming] = useState(false);
  const [error, setError] = useState('');
  const [connected, setConnected] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);

  const desktopId = sessionStorage.getItem('companion_desktop_id') || '';
  const desktopName = sessionStorage.getItem('companion_desktop_name') || 'Desktop';

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  useEffect(() => {
    if (!desktopId) {
      navigate('/desktops');
      return;
    }

    // Set initial connected state
    setConnected(socket.connected);

    // Listen for welcome → reconnected
    const unsubWelcome = socket.on('welcome', () => {
      setConnected(true);
    });

    // Listen for chat message deltas
    const unsubDelta = socket.on('chat.message.delta', (msg: any) => {
      const payload = msg.payload || {};
      const text = payload.text || '';
      setMessages((prev) => {
        const last = prev[prev.length - 1];
        if (last && last.role === 'assistant' && !last.final) {
          // Append to existing streaming message
          const updated = [...prev];
          updated[updated.length - 1] = { ...last, text: last.text + text };
          return updated;
        }
        // Start new assistant message
        return [...prev, { id: `msg_${Date.now()}`, role: 'assistant', text, final: false }];
      });
    });

    // Listen for chat message complete
    const unsubComplete = socket.on('chat.message.complete', (msg: any) => {
      const payload = msg.payload || {};
      const text = payload.text || '';
      setMessages((prev) => {
        if (prev.length === 0) {
          return [{ id: `msg_${Date.now()}`, role: 'assistant', text, final: true }];
        }
        const updated = [...prev];
        const last = updated[updated.length - 1];
        if (last.role === 'assistant') {
          updated[updated.length - 1] = { ...last, text, final: true };
        } else {
          updated.push({ id: `msg_${Date.now()}`, role: 'assistant', text, final: true });
        }
        return updated;
      });
      setStreaming(false);
    });

    // Listen for chat errors
    const unsubError = socket.on('chat.error', (msg: any) => {
      const payload = msg.payload || {};
      setError(payload.message || 'An error occurred');
      setStreaming(false);
    });

    return () => {
      unsubWelcome();
      unsubDelta();
      unsubComplete();
      unsubError();
    };
  }, [desktopId, navigate]);

  const sendMessage = useCallback(() => {
    if (!input.trim() || streaming || !desktopId) return;
    const text = input.trim();
    const userMsg: Message = { id: `msg_${Date.now()}`, role: 'user', text, final: true };
    setMessages((prev) => [...prev, userMsg]);
    setInput('');
    setStreaming(true);
    setError('');
    socket.send('chat.send', { text }, desktopId);
  }, [input, streaming, desktopId]);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100dvh' }}>
      {/* Header */}
      <div style={{
        padding: '0.75rem 1rem',
        borderBottom: '1px solid #222',
        display: 'flex',
        alignItems: 'center',
        gap: '0.75rem',
      }}>
        <button
          onClick={() => navigate('/desktops')}
          style={{
            background: 'transparent',
            border: 'none',
            color: '#6c5ce7',
            fontSize: '1.25rem',
            cursor: 'pointer',
            padding: '0.25rem',
          }}
        >
          ←
        </button>
        <div>
          <div style={{ fontWeight: 600 }}>{desktopName}</div>
          <div style={{ fontSize: '0.75rem', color: connected ? '#00b894' : '#e17055' }}>
            {connected ? '● Connected' : '● Disconnected'}
          </div>
        </div>
      </div>

      {/* Connection lost banner */}
      {!connected && (
        <div style={{
          padding: '0.5rem 1rem',
          background: '#e1705533',
          color: '#e17055',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          gap: '0.5rem',
          fontSize: '0.85rem',
        }}>
          <span>Connection lost</span>
          <button
            onClick={() => navigate('/login')}
            style={{
              background: '#e17055',
              border: 'none',
              borderRadius: '4px',
              color: '#fff',
              padding: '0.25rem 0.75rem',
              cursor: 'pointer',
              fontSize: '0.8rem',
            }}
          >
            Reconnect
          </button>
        </div>
      )}

      {/* Messages */}
      <div style={{ flex: 1, overflow: 'auto', padding: '1rem' }}>
        {messages.length === 0 && (
          <div style={{ textAlign: 'center', marginTop: '3rem', color: '#666' }}>
            <p>Send a message to start chatting with Aura.</p>
          </div>
        )}
        {messages.map((m) => (
          <div key={m.id} style={{
            marginBottom: '1rem',
            display: 'flex',
            flexDirection: 'column',
            alignItems: m.role === 'user' ? 'flex-end' : 'flex-start',
          }}>
            <div style={{
              display: 'inline-block',
              padding: '0.75rem 1rem',
              borderRadius: '16px',
              background: m.role === 'user' ? '#6c5ce7' : '#1e1e32',
              maxWidth: '80%',
              wordBreak: 'break-word',
              borderBottomRightRadius: m.role === 'user' ? '4px' : '16px',
              borderBottomLeftRadius: m.role === 'assistant' ? '4px' : '16px',
            }}>
              {m.text}
              {!m.final && m.role === 'assistant' && (
                <span style={{ animation: 'pulse 1s infinite', marginLeft: '0.25rem' }}>▊</span>
              )}
            </div>
          </div>
        ))}
        <div ref={bottomRef} />
      </div>

      {/* Input */}
      <div style={{ padding: '0.75rem', borderTop: '1px solid #222' }}>
        {error && (
          <div style={{
            padding: '0.5rem 0.75rem',
            background: '#e1705533',
            color: '#e17055',
            borderRadius: '8px',
            marginBottom: '0.5rem',
            fontSize: '0.85rem',
          }}>
            {error}
          </div>
        )}
        <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center' }}>
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && sendMessage()}
            placeholder={desktopId ? 'Type a message...' : 'Select a desktop first'}
            disabled={streaming || !desktopId}
            style={{
              flex: 1,
              padding: '0.75rem 1rem',
              background: '#1e1e32',
              border: '1px solid #333',
              borderRadius: '24px',
              color: '#e0e0e0',
              fontSize: '1rem',
              outline: 'none',
            }}
          />
          <button
            onClick={sendMessage}
            disabled={streaming || !input.trim() || !desktopId}
            style={{
              width: '44px',
              height: '44px',
              background: streaming || !input.trim() || !desktopId ? '#444' : '#6c5ce7',
              border: 'none',
              borderRadius: '50%',
              color: '#fff',
              fontSize: '1.25rem',
              cursor: streaming || !input.trim() || !desktopId ? 'not-allowed' : 'pointer',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
            }}
          >
            ↑
          </button>
          {streaming && (
            <button
              onClick={() => {
                socket.send('chat.cancel', {}, desktopId);
                setStreaming(false);
                setError('');
              }}
              style={{
                padding: '0.5rem 1rem',
                background: '#e17055',
                border: 'none',
                borderRadius: '16px',
                color: '#fff',
                fontSize: '0.85rem',
                fontWeight: 600,
                cursor: 'pointer',
                whiteSpace: 'nowrap',
              }}
            >
              Cancel
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

export default ChatScreen;
