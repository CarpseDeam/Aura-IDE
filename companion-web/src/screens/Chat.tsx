import { useState, useEffect, useRef, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import CompanionSocket, { socket } from '../api/socket';
import { tokens, glassCard, statusPillStyle } from '../ui/theme';

interface Message {
  id: string;
  role: 'user' | 'assistant';
  text: string;
  final?: boolean;
}

function ChatScreen() {
  const navigate = useNavigate();
  const isPaired = CompanionSocket.isPaired();

  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState('');
  const [streaming, setStreaming] = useState(false);
  const [error, setError] = useState('');
  const [connected, setConnected] = useState(socket.connected);
  const bottomRef = useRef<HTMLDivElement>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);

  const safeCtx = CompanionSocket.getStoredSafeContext();
  const projectId = safeCtx.project_id || '';
  const conversationId = safeCtx.conversation_id || '';
  const desktopId = sessionStorage.getItem('companion_desktop_id') || '';
  const desktopName = safeCtx.desktop_name || sessionStorage.getItem('companion_desktop_name') || 'Aura Desktop';

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  useEffect(() => {
    if (!isPaired) {
      navigate('/login', { replace: true });
      return;
    }
    if (!desktopId) {
      navigate('/login', { replace: true });
      return;
    }
    if (!socket.connected) {
      // Reconnect using stored token
      socket.connect(localStorage.getItem('companion_relay_url') || 'ws://localhost:8765');
    }
  }, [isPaired, desktopId, navigate]);

  useEffect(() => {
    setConnected(socket.connected);

    const unsubWelcome = socket.on('welcome', () => setConnected(true));
    const unsubDelta = socket.on('chat.message.delta', (msg: any) => {
      const text = msg.payload?.text || '';
      const kind = msg.payload?.type || 'content';
      if (kind === 'reasoning') return;  // skip reasoning in MVP
      setMessages(prev => {
        const last = prev[prev.length - 1];
        if (last && last.role === 'assistant' && !last.final) {
          const updated = [...prev];
          updated[updated.length - 1] = { ...last, text: last.text + text };
          return updated;
        }
        return [...prev, { id: `msg_${Date.now()}`, role: 'assistant', text, final: false }];
      });
    });
    const unsubComplete = socket.on('chat.message.complete', (msg: any) => {
      const text = msg.payload?.text || '';
      const finishReason = msg.payload?.finish_reason || '';
      setMessages(prev => {
        if (prev.length === 0) {
          return [{ id: `msg_${Date.now()}`, role: 'assistant', text, final: true }];
        }
        const updated = [...prev];
        const last = updated[updated.length - 1];
        if (last.role === 'assistant') {
          // Use the streamed text we already have if final text is empty/cancelled,
          // otherwise replace with the canonical full text.
          if (finishReason === 'cancelled' && !text) {
            updated[updated.length - 1] = { ...last, final: true };
          } else {
            updated[updated.length - 1] = { ...last, text: text || last.text, final: true };
          }
        } else {
          updated.push({ id: `msg_${Date.now()}`, role: 'assistant', text, final: true });
        }
        return updated;
      });
      setStreaming(false);
    });
    const unsubError = socket.on('chat.error', (msg: any) => {
      setError(msg.payload?.message || 'An error occurred');
      setStreaming(false);
    });
    return () => {
      unsubWelcome();
      unsubDelta();
      unsubComplete();
      unsubError();
    };
  }, []);

  const sendMessage = useCallback(() => {
    const text = input.trim();
    if (!text || streaming || !desktopId) return;
    setMessages(prev => [...prev, { id: `msg_${Date.now()}`, role: 'user', text, final: true }]);
    setInput('');
    setStreaming(true);
    setError('');
    socket.send('chat.send', { text }, desktopId, projectId, conversationId);
    if (taRef.current) taRef.current.style.height = 'auto';
  }, [input, streaming, desktopId, projectId, conversationId]);

  const cancel = useCallback(() => {
    socket.send('chat.cancel', {}, desktopId, projectId, conversationId);
    setStreaming(false);
  }, [desktopId, projectId, conversationId]);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100dvh', padding: '0 0.75rem' }}>
      {/* Header */}
      <header style={{
        ...glassCard,
        margin: '0.75rem 0 0.5rem',
        padding: '0.75rem 1rem',
        display: 'flex',
        alignItems: 'center',
        gap: '0.75rem',
      }}>
        <button
          onClick={() => navigate('/login')}
          aria-label="Back"
          style={{
            background: 'transparent', border: 'none',
            color: tokens.fgDim, fontSize: '1.4rem',
            padding: '0.1rem 0.4rem',
          }}
        >
          ←
        </button>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{
            fontSize: '0.95rem', fontWeight: 600,
            whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
          }}>
            {desktopName}
          </div>
          <div style={{ fontSize: '0.7rem', color: tokens.fgMuted, marginTop: 2 }}>
            Aura Desktop
          </div>
          {safeCtx.project_name && (
            <div style={{ fontSize: '0.7rem', color: tokens.fgMuted, marginTop: 2 }}>
              Project: {safeCtx.project_name}
            </div>
          )}
        </div>
        <span style={statusPillStyle(connected ? 'connected' : 'disconnected')}>
          ● {connected ? 'Online' : 'Offline'}
        </span>
      </header>

      {/* Connection lost banner */}
      {!connected && (
        <div style={{
          padding: '0.55rem 0.85rem',
          background: 'rgba(247,118,142,0.08)',
          border: `1px solid ${tokens.danger}`,
          color: tokens.danger,
          borderRadius: 10,
          fontSize: '0.85rem',
          marginBottom: '0.5rem',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: 8,
        }}>
          <span>Connection lost. Trying to reconnect…</span>
          <button
            onClick={() => navigate('/login')}
            style={{
              background: 'transparent', border: `1px solid ${tokens.danger}`,
              borderRadius: 8, color: tokens.danger,
              padding: '0.25rem 0.7rem', fontSize: '0.78rem', fontWeight: 600,
            }}
          >
            Reconnect
          </button>
        </div>
      )}

      {/* Messages */}
      <main style={{ flex: 1, overflow: 'auto', padding: '0.5rem 0 0.25rem' }}>
        {messages.length === 0 && (
          <EmptyState />
        )}
        {messages.map((m, idx) => (
          <MessageBubble key={m.id} message={m} previous={messages[idx - 1]} />
        ))}
        <div ref={bottomRef} />
      </main>

      {/* Input bar */}
      <footer style={{
        ...glassCard,
        margin: '0.5rem 0 0.75rem',
        padding: '0.6rem 0.7rem',
      }}>
        {error && (
          <div style={{
            padding: '0.4rem 0.7rem',
            background: 'rgba(247,118,142,0.10)',
            color: tokens.danger,
            border: `1px solid ${tokens.danger}`,
            borderRadius: 8,
            fontSize: '0.8rem',
            marginBottom: '0.5rem',
          }}>
            {error}
          </div>
        )}
        <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'flex-end' }}>
          <textarea
            ref={taRef}
            value={input}
            onChange={e => {
              setInput(e.target.value);
              const el = e.currentTarget;
              el.style.height = 'auto';
              el.style.height = Math.min(el.scrollHeight, 120) + 'px';
            }}
            onKeyDown={e => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                sendMessage();
              }
            }}
            placeholder={streaming ? 'Aura is responding…' : 'Message Aura'}
            rows={1}
            disabled={streaming || !desktopId}
            style={{
              flex: 1,
              padding: '0.65rem 0.9rem',
              background: 'rgba(20, 24, 34, 0.6)',
              border: `1px solid ${tokens.border}`,
              borderRadius: 14,
              color: tokens.fg,
              fontSize: '0.95rem',
              outline: 'none',
              resize: 'none',
              maxHeight: 120,
              lineHeight: 1.35,
            }}
          />
          {streaming ? (
            <button
              onClick={cancel}
              aria-label="Cancel"
              style={{
                width: 44, height: 44, borderRadius: 22,
                background: tokens.danger,
                border: 'none', color: '#0a0f1f',
                fontWeight: 700, fontSize: '1.1rem',
              }}
            >
              ◼
            </button>
          ) : (
            <button
              onClick={sendMessage}
              disabled={!input.trim() || !desktopId}
              aria-label="Send"
              style={{
                width: 44, height: 44, borderRadius: 22,
                background: !input.trim() || !desktopId ? tokens.borderStrong : tokens.accent,
                color: !input.trim() || !desktopId ? tokens.fgMuted : '#0a0f1f',
                border: 'none', fontSize: '1.15rem', fontWeight: 700,
                boxShadow: !input.trim() || !desktopId ? 'none' : `0 6px 22px -8px ${tokens.accentGlow}`,
              }}
            >
              ↑
            </button>
          )}
        </div>
      </footer>
    </div>
  );
}

function MessageBubble({ message: m, previous }: { message: Message; previous?: Message }) {
  const isUser = m.role === 'user';
  const tightTop = previous && previous.role === m.role;
  return (
    <div
      className="fade-in"
      style={{
        display: 'flex',
        justifyContent: isUser ? 'flex-end' : 'flex-start',
        marginTop: tightTop ? 4 : 12,
        padding: '0 0.25rem',
      }}
    >
      <div style={{
        maxWidth: '82%',
        padding: '0.7rem 0.95rem',
        background: isUser ? tokens.userBubble : tokens.assistantBubble,
        border: `1px solid ${tokens.border}`,
        color: tokens.fg,
        borderRadius: 16,
        borderBottomRightRadius: isUser ? 6 : 16,
        borderBottomLeftRadius: !isUser ? 6 : 16,
        fontSize: '0.95rem',
        lineHeight: 1.42,
        whiteSpace: 'pre-wrap',
        wordBreak: 'break-word',
        boxShadow: isUser ? `0 8px 28px -16px ${tokens.accentGlow}` : 'none',
      }}>
        {m.text || (m.role === 'assistant' && !m.final ? '' : ' ')}
        {!m.final && m.role === 'assistant' && (
          <span style={{
            display: 'inline-block',
            width: 7, height: 14,
            marginLeft: 4,
            verticalAlign: 'middle',
            background: tokens.accent,
            animation: 'pulse 1.1s ease-in-out infinite',
          }} />
        )}
      </div>
    </div>
  );
}

function EmptyState() {
  return (
    <div style={{
      textAlign: 'center',
      marginTop: '3rem',
      color: tokens.fgMuted,
      padding: '1rem',
    }}>
      <div style={{
        fontSize: '2.4rem',
        color: tokens.accent,
        opacity: 0.55,
        marginBottom: '0.5rem',
        letterSpacing: '0.2em',
        fontWeight: 700,
      }}>
        ◌
      </div>
      <div style={{ fontSize: '0.95rem', color: tokens.fgDim }}>
        Send a message to start chatting with Aura.
      </div>
      <div style={{ fontSize: '0.78rem', marginTop: 6 }}>
        Your desktop streams the response back here in real-time.
      </div>
    </div>
  );
}

export default ChatScreen;
