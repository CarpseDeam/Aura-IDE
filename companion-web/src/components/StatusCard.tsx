interface StatusCardProps {
  label: string;
  status: 'success' | 'warning' | 'error' | 'info';
  children?: React.ReactNode;
}

function StatusCard({ label, status, children }: StatusCardProps) {
  const colors = { success: '#00b894', warning: '#fdcb6e', error: '#e17055', info: '#74b9ff' };
  return (
    <div style={{
      padding: '0.75rem 1rem',
      background: '#1e1e32',
      borderRadius: '12px',
      borderLeft: `4px solid ${colors[status]}`,
      marginBottom: '0.75rem',
    }}>
      <div style={{ fontWeight: 600, marginBottom: '0.25rem' }}>{label}</div>
      {children && <div style={{ fontSize: '0.85rem', color: '#aaa' }}>{children}</div>}
    </div>
  );
}

export default StatusCard;
