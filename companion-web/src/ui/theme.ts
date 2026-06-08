// Shared style tokens — match the CSS custom properties in index.html.

export const tokens = {
  bg: 'var(--bg)',
  bgElev: 'var(--bg-elev)',
  bgGlass: 'var(--bg-glass)',
  bgCard: 'var(--bg-card)',
  border: 'var(--border)',
  borderStrong: 'var(--border-strong)',
  fg: 'var(--fg)',
  fgDim: 'var(--fg-dim)',
  fgMuted: 'var(--fg-muted)',
  accent: 'var(--accent)',
  accentHover: 'var(--accent-hover)',
  accentGlow: 'var(--accent-glow)',
  success: 'var(--success)',
  warn: 'var(--warn)',
  danger: 'var(--danger)',
  userBubble: 'var(--user-bubble)',
  assistantBubble: 'var(--assistant-bubble)',
  radius: 'var(--radius)',
} as const;

export const glassCard: React.CSSProperties = {
  background: tokens.bgCard,
  border: `1px solid ${tokens.border}`,
  borderRadius: tokens.radius,
  backdropFilter: 'blur(18px) saturate(160%)',
  WebkitBackdropFilter: 'blur(18px) saturate(160%)',
  boxShadow: '0 10px 40px -20px rgba(0,0,0,0.6), inset 0 1px 0 rgba(255,255,255,0.04)',
};

export const primaryButton: React.CSSProperties = {
  width: '100%',
  padding: '0.85rem 1rem',
  background: tokens.accent,
  color: '#0a0f1f',
  border: 'none',
  borderRadius: '10px',
  fontSize: '0.95rem',
  fontWeight: 600,
  letterSpacing: '0.01em',
  boxShadow: `0 6px 24px -10px ${tokens.accentGlow}`,
};

export const ghostButton: React.CSSProperties = {
  width: '100%',
  padding: '0.75rem 1rem',
  background: 'transparent',
  color: tokens.fg,
  border: `1px solid ${tokens.borderStrong}`,
  borderRadius: '10px',
  fontSize: '0.9rem',
  fontWeight: 500,
};

export const inputBase: React.CSSProperties = {
  width: '100%',
  padding: '0.8rem 0.95rem',
  background: 'rgba(20, 24, 34, 0.55)',
  border: `1px solid ${tokens.border}`,
  borderRadius: '10px',
  color: tokens.fg,
  fontSize: '0.95rem',
  outline: 'none',
  transition: 'border-color 120ms ease',
};

export const statusPillStyle = (kind: 'connected' | 'connecting' | 'disconnected' | 'error'): React.CSSProperties => {
  const colorMap = {
    connected:    tokens.success,
    connecting:   tokens.warn,
    disconnected: tokens.danger,
    error:        tokens.danger,
  };
  const color = colorMap[kind];
  return {
    display: 'inline-flex',
    alignItems: 'center',
    gap: '0.45rem',
    padding: '0.25rem 0.7rem',
    borderRadius: 999,
    background: 'rgba(255,255,255,0.04)',
    border: `1px solid ${tokens.border}`,
    color,
    fontSize: '0.72rem',
    fontWeight: 600,
    letterSpacing: '0.04em',
  };
};
