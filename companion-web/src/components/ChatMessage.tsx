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
      marginBottom: '1rem',
    }}>
      <div style={{
        padding: '0.75rem 1rem',
        borderRadius: '16px',
        background: role === 'user' ? '#6c5ce7' : '#1e1e32',
        maxWidth: '80%',
        wordBreak: 'break-word',
      }}>
        {text}
      </div>
      {timestamp && (
        <div style={{ fontSize: '0.7rem', color: '#666', marginTop: '0.25rem' }}>{timestamp}</div>
      )}
    </div>
  );
}

export default ChatMessage;
