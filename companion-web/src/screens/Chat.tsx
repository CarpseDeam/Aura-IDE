import { useState, useEffect, useRef, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import CompanionSocket, { socket } from '../api/socket';
import { useDesktopVerification } from '../hooks/useDesktopVerification';
import { tokens, glassCard, statusPillStyle } from '../ui/theme';

interface Message {
  id: string;
  role: 'user' | 'assistant';
  text: string;
  final?: boolean;
}

function ChatScreen() {
  const navigate = useNavigate();
  const { phase, error: verifyError, retry, goToLogin } = useDesktopVerification();

  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState('');
  const [streaming, setStreaming] = useState(false);
  const [chatError, setChatError] = useState('');
  const bottomRef = useRef<HTMLDivElement>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);
  const watchdogRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const safeCtx = CompanionSocket.getStoredSafeContext();
  const projectId = safeCtx.project_id || '';
  const conversationId = safeCtx.conversation_id || '';
  const desktopId = sessionStorage.getItem('companion_desktop_id') || safeCtx.desktop_id || '';
  const desktopName = safeCtx.desktop_name || sessionStorage.getItem('companion_desktop_name') || 'Aura Desktop';

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  // Phase-gated message listeners — only active when verified
  useEffect(() => {
    if (phase !== 'connected') return;

    const unsubDelta = socket.on('chat.message.delta', (msg: any) => {
      clearWatchdog();
      setChatError('');
      const text = msg.payload?.text || '';
      const kind = msg.payload?.type || 'content';
      if (kind === 'reasoning') return;
      setMessages(prev => {
        const last = prev[prev.length - 1];
        if (last && last.role === 'assistant' && !last.final) {
          const updated = [...prev];
          updated[updated.length - 1] = { ...last, text: last.text + text };
          return updated;
        }
        // If the last message is a final assistant from history whose text already starts with this delta text, skip — stale delta for a message we already have.
        if (last && last.role === 'assistant' && last.final && last.text.startsWith(text)) {
          return prev;
        }
        return [...prev, { id: `msg_${Date.now()}`, role: 'assistant', text, final: false }];
      });
    });
    const unsubComplete = socket.on('chat.message.complete', (msg: any) => {
      clearWatchdog();
      const text = msg.payload?.text || '';
      const finishReason = msg.payload?.finish_reason || '';
      setMessages(prev => {
        if (prev.length === 0) {
          return [{ id: `msg_${Date.now()}`, role: 'assistant', text, final: true }];
        }
        const updated = [...prev];
        const last = updated[updated.length - 1];
        if (last.role === 'assistant') {
          if (last.final) return prev; // Already complete from history, skip stale event
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
    const unsubChatErr = socket.on('chat.error', (msg: any) => {
      clearWatchdog();
      setChatError(msg.payload?.message || 'An error occurred');
      setStreaming(false);
    });
    return () => {
      clearWatchdog();
      unsubDelta();
      unsubComplete();
      unsubChatErr();
    };
  }, [phase]);

  // History loading — fetch conversation history once connected with project+conversation
  useEffect(() => {
    if (phase !== 'connected' || !projectId || !conversationId) return;

    const unsubHistory = socket.on('conversation.history_result', (msg: any) => {
      // Ignore stale results from previous selections
      if (msg.payload?.project_id !== projectId || msg.payload?.thread_id !== conversationId) {
        return;
      }
      if (msg.payload?.error) {
        console.warn('conversation.history_result error:', msg.payload.error);
        return;
      }
      const historyMsgs: Message[] = (msg.payload?.messages || []).map((m: any, i: number) => ({
        id: `hist_${i}_${m.role}`,
        role: m.role,
        text: m.content ?? '',
        final: true,
      }));
      setMessages(historyMsgs);
    });

    socket.send('conversation.history', { project_id: projectId, thread_id: conversationId }, desktopId, projectId, conversationId);

    return () => {
      unsubHistory();
    };
  }, [phase, desktopId, projectId, conversationId]);

  const sendMessage = useCallback((explicitText?: string) => {
    const text = explicitText !== undefined ? explicitText.trim() : input.trim();
    if (!text || streaming || !desktopId || !projectId || !conversationId) return;
    setMessages(prev => [...prev, { id: `msg_${Date.now()}`, role: 'user', text, final: true }]);
    if (explicitText === undefined) setInput('');
    setStreaming(true);
    setChatError('');
    socket.send('chat.send', { text }, desktopId, projectId, conversationId);
    clearWatchdog();
    watchdogRef.current = setTimeout(() => {
      setStreaming(false);
      setChatError('No response from desktop. Check Aura Desktop.');
    }, 60_000);
    if (taRef.current) taRef.current.style.height = 'auto';
  }, [input, streaming, desktopId, projectId, conversationId]);

  const cancel = useCallback(() => {
    clearWatchdog();
    socket.send('chat.cancel', {}, desktopId, projectId, conversationId);
    setStreaming(false);
  }, [desktopId, projectId, conversationId]);

  const clearWatchdog = () => {
    if (watchdogRef.current !== null) {
      clearTimeout(watchdogRef.current);
      watchdogRef.current = null;
    }
  };

  // Connecting / verifying full-screen spinner
  if (phase === 'connecting' || phase === 'verifying') {
    return (
      <div style={{ display: 'flex', flexDirection: 'column', height: '100dvh', padding: '0 0.75rem', alignItems: 'center', justifyContent: 'center' }}>
        <div style={{ ...glassCard, padding: '2rem 1.5rem', textAlign: 'center', maxWidth: 380 }}>
          <div style={{
            width: 36, height: 36, borderRadius: '50%',
            border: `3px solid ${tokens.border}`,
            borderTopColor: tokens.accent,
            animation: 'spin 0.9s linear infinite',
            margin: '0 auto 1rem',
          }} />
          <div style={{ color: tokens.fgDim, fontSize: '0.9rem' }}>
            {phase === 'connecting' ? 'Connecting to your Aura desktop…' : 'Verifying with your Aura desktop…'}
          </div>
          <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
        </div>
      </div>
    );
  }

  // Unavailable full-screen card
  if (phase === 'unavailable') {
    return (
      <div style={{ display: 'flex', flexDirection: 'column', height: '100dvh', padding: '0 0.75rem', alignItems: 'center', justifyContent: 'center' }}>
        <div style={{ ...glassCard, padding: '1.5rem', textAlign: 'center', maxWidth: 380, width: '100%' }}>
          <div style={{ fontSize: '1.1rem', fontWeight: 600, color: tokens.danger, marginBottom: 4 }}>
            Previous desktop unavailable
          </div>
          <div style={{ color: tokens.fgDim, fontSize: '0.9rem', marginBottom: '1rem' }}>
            {verifyError || 'Could not reach your Aura desktop.'}
          </div>
          <button
            onClick={goToLogin}
            style={{
              width: '100%', padding: '0.75rem 1rem',
              background: tokens.accent, color: '#0a0f1f',
              border: 'none', borderRadius: 10,
              fontSize: '0.9rem', fontWeight: 600,
              marginBottom: '0.5rem', cursor: 'pointer',
            }}
          >
            Go to Login
          </button>
          <button
            onClick={retry}
            style={{
              width: '100%', padding: '0.75rem 1rem',
              background: 'transparent', color: tokens.fg,
              border: `1px solid ${tokens.borderStrong}`,
              borderRadius: 10, fontSize: '0.9rem', fontWeight: 500,
              cursor: 'pointer',
            }}
          >
            Retry
          </button>
        </div>
      </div>
    );
  }

  // Connected — normal chat UI
  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100dvh', padding: '0 0.75rem' }}>
      {/* Header */}
      <header style={{
        ...glassCard,
        margin: '0.4rem 0 0.3rem',
        padding: '0.45rem 0.75rem',
        display: 'flex',
        alignItems: 'center',
        gap: '0.5rem',
      }}>
        <button
          onClick={() => navigate('/login')}
          aria-label="Back"
          style={{
            background: 'transparent', border: 'none',
            color: tokens.fgDim, fontSize: '1.2rem',
            padding: '0.1rem 0.4rem',
          }}
        >
          ←
        </button>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{
            fontSize: '0.9rem', fontWeight: 600,
            whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
          }}>
            {desktopName}
          </div>
          <div style={{ fontSize: '0.65rem', color: tokens.fgMuted, marginTop: 2 }}>
            Aura Desktop
          </div>
          {safeCtx.project_name && (
            <div style={{ fontSize: '0.65rem', color: tokens.fgMuted, marginTop: 2 }}>
              Project: {safeCtx.project_name}
            </div>
          )}
        </div>
        <span style={statusPillStyle('connected')}>
          ●{' '}
          {!projectId || !conversationId
            ? 'Select a conversation'
            : messages.length === 0 && !streaming
              ? 'Send a message to begin'
              : 'Online'}
        </span>
      </header>

      {/* Messages */}
      <main style={{ flex: 1, overflow: 'auto', padding: '0.3rem 0 0.2rem' }}>
        {messages.length === 0 ? (
          phase === 'connected' && (!projectId || !conversationId) ? (
            <div style={{
              textAlign: 'center',
              marginTop: '3rem',
              color: tokens.fgMuted,
              padding: '1rem',
            }}>
              <div style={{
                fontSize: '2.4rem',
                color: tokens.warn,
                opacity: 0.55,
                marginBottom: '0.5rem',
                letterSpacing: '0.2em',
                fontWeight: 700,
              }}>
                ◌
              </div>
              <div style={{ fontSize: '0.95rem', color: tokens.fgDim }}>
                No conversation selected
              </div>
              <div style={{ fontSize: '0.78rem', marginTop: 6, marginBottom: 14 }}>
                Pick a project thread to start chatting.
              </div>
              <button
                onClick={() => navigate('/projects')}
                style={{
                  padding: '0.6rem 1.2rem',
                  background: tokens.accent,
                  color: '#0a0f1f',
                  border: 'none',
                  borderRadius: 10,
                  fontSize: '0.85rem',
                  fontWeight: 600,
                  cursor: 'pointer',
                  boxShadow: `0 6px 22px -8px ${tokens.accentGlow}`,
                }}
              >
                Go to Projects
              </button>
            </div>
          ) : (
            <EmptyState />
          )
        ) : (
          messages.map((m, idx) => (
            <MessageBubble key={m.id} message={m} previous={messages[idx - 1]} />
          ))
        )}

        {/* Quick prompt chips */}
        {(messages.length > 0 || (phase === 'connected' && projectId && conversationId)) && (
          <div style={{
            display: 'flex',
            gap: '0.5rem',
            padding: '0.5rem 0 0.25rem',
            overflow: 'auto',
            flexShrink: 0,
            scrollbarWidth: 'none',
          }}>
            {[
              { label: "What's new?", text: "What's new?" },
              { label: 'Status', text: 'What is the current status?' },
              { label: 'Help', text: 'Help me with a coding task' },
            ].map((chip) => (
              <button
                key={chip.label}
                onClick={() => {
                  if (streaming || !desktopId || !projectId || !conversationId) return;
                  sendMessage(chip.text);
                }}
                style={{
                  background: 'rgba(255,255,255,0.04)',
                  border: `1px solid ${tokens.border}`,
                  borderRadius: 999,
                  padding: '0.35rem 0.9rem',
                  color: tokens.fgDim,
                  fontSize: '0.8rem',
                  fontWeight: 500,
                  whiteSpace: 'nowrap',
                  cursor: 'pointer',
                  transition: 'background 120ms ease, border-color 120ms ease',
                }}
              >
                {chip.label}
              </button>
            ))}
          </div>
        )}

        <div ref={bottomRef} />
      </main>

      {/* Input bar */}
      <footer style={{
        ...glassCard,
        margin: '0.5rem 0 0.75rem',
        padding: '0.6rem 0.7rem',
      }}>
        {chatError && (
          <div style={{
            padding: '0.4rem 0.7rem',
            background: 'rgba(247,118,142,0.10)',
            color: tokens.danger,
            border: `1px solid ${tokens.danger}`,
            borderRadius: 8,
            fontSize: '0.8rem',
            marginBottom: '0.5rem',
          }}>
            {chatError}
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
              onClick={() => sendMessage()}
              disabled={!input.trim() || !desktopId || !projectId || !conversationId}
              aria-label="Send"
              style={{
                width: 44, height: 44, borderRadius: 22,
                background: !input.trim() || !desktopId || !projectId || !conversationId ? tokens.borderStrong : tokens.accent,
                color: !input.trim() || !desktopId || !projectId || !conversationId ? tokens.fgMuted : '#0a0f1f',
                border: 'none', fontSize: '1.15rem', fontWeight: 700,
                boxShadow: !input.trim() || !desktopId || !projectId || !conversationId ? 'none' : `0 6px 22px -8px ${tokens.accentGlow}`,
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
        marginTop: tightTop ? 2 : 8,
        padding: '0 0.15rem',
      }}
    >
      <div style={{
        maxWidth: '82%',
        padding: '0.35rem 0.7rem',
        background: isUser ? tokens.userBubble : tokens.assistantBubble,
        border: `1px solid ${tokens.border}`,
        color: tokens.fg,
        borderRadius: 16,
        borderBottomRightRadius: isUser ? 6 : 16,
        borderBottomLeftRadius: !isUser ? 6 : 16,
        fontSize: '0.85rem',
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
