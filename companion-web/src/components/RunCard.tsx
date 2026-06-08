interface RunCardProps {
  runId: string;
  label: string;
  kind: 'worker' | 'drone';
  status: string;
  onCancel?: () => void;
}

function RunCard({ runId, label, kind, status, onCancel }: RunCardProps) {
  const statusColors: Record<string, string> = {
    running: '#00b894',
    waiting_approval: '#fdcb6e',
    completed: '#74b9ff',
    failed: '#e17055',
    cancelled: '#888',
  };
  return (
    <div style={{
      padding: '0.75rem 1rem',
      background: '#1e1e32',
      borderRadius: '12px',
      marginBottom: '0.75rem',
    }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <div>
          <div style={{ fontWeight: 600 }}>{label}</div>
          <div style={{ fontSize: '0.8rem', color: '#888' }}>
            {kind === 'worker' ? '🧠 Worker' : '🤖 Drone'} · {runId.slice(0, 8)}
          </div>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
          <span style={{
            width: '8px',
            height: '8px',
            borderRadius: '50%',
            background: statusColors[status] || '#888',
            display: 'inline-block',
          }} />
          <span style={{ fontSize: '0.8rem', color: '#aaa' }}>{status}</span>
          {onCancel && (status === 'running' || status === 'waiting_approval') && (
            <button onClick={onCancel} style={{
              background: 'transparent',
              border: '1px solid #e17055',
              color: '#e17055',
              borderRadius: '6px',
              padding: '0.25rem 0.5rem',
              fontSize: '0.75rem',
              cursor: 'pointer',
            }}>
              Cancel
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

export default RunCard;
