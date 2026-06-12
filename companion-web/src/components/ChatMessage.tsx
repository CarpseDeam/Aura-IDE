interface ChatMessageProps {
  role: 'user' | 'assistant';
  text: string;
  timestamp?: string;
}

function ChatMessage({ role, text, timestamp }: ChatMessageProps) {
  return (
    <div style={{
      display: 'flex',
      flexDirection: 'column',
      alignItems: role === 'user' ? 'flex-end' : 'flex-start',
      marginBottom: '0.75rem',
    }}>
      <div style={{
        padding: '0.75rem 1rem',
        borderRadius: '16px',
        background: role === 'user' ? 'var(--user-bubble)' : 'var(--assistant-bubble)',
        color: 'var(--fg)',
        maxWidth: '80%',
        wordBreak: 'break-word',
      }}>
        {text}
      </div>
      {timestamp && (
        <div style={{ fontSize: '0.7rem', color: 'var(--fg-muted)', marginTop: '0.25rem' }}>{timestamp}</div>
      )}
    </div>
  );
}

export default ChatMessage;
