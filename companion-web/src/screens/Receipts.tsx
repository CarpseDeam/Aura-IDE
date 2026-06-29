import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import CompanionSocket, { socket } from '../api/socket';
import { useDesktopVerification } from '../hooks/useDesktopVerification';
import { tokens, glassCard, statusPillStyle } from '../ui/theme';

interface Receipt {
  run_id: string;
  kind: string;
  label: string;
  status: string;
  completed_at: string;
  summary: string;
}

function ReceiptsScreen() {
  const navigate = useNavigate();
  const isPaired = CompanionSocket.isPaired();
  const { phase, error: verifyError, retry, goToLogin } = useDesktopVerification();

  const [receipts, setReceipts] = useState<Receipt[]>([]);
  const [loading, setLoading] = useState(true);
  const [fetchError, setFetchError] = useState('');

  // Early redirect for unpaired / missing desktop
  useEffect(() => {
    if (!isPaired) {
      navigate('/login', { replace: true });
      return;
    }
    const desktopId =
      sessionStorage.getItem('companion_desktop_id') ||
      CompanionSocket.getStoredSafeContext().desktop_id ||
      '';
    if (!desktopId) {
      navigate('/login', { replace: true });
      return;
    }
  }, [isPaired, navigate]);

  // Phase-gated fetch — only send receipt.list_recent after verified
  useEffect(() => {
    if (phase !== 'connected') return;

    setLoading(true);
    setFetchError('');

    const desktopId =
      sessionStorage.getItem('companion_desktop_id') ||
      CompanionSocket.getStoredSafeContext().desktop_id ||
      '';
    if (!desktopId) return;

    const unsubList = socket.on('receipt.list_result', (msg: any) => {
      if (msg.payload?.error) {
        setFetchError(msg.payload.error);
        setLoading(false);
        return;
      }
      setReceipts(msg.payload?.receipts ?? []);
      setLoading(false);
    });

    socket.send('receipt.list_recent', {}, desktopId);

    return () => {
      unsubList();
    };
  }, [phase]);

  function formatDate(dateStr: string): string {
    if (!dateStr) return '';
    try {
      const d = new Date(dateStr);
      const now = Date.now();
      const diff = now - d.getTime();
      if (diff < 60000) return 'just now';
      if (diff < 3600000) return `${Math.floor(diff / 60000)}m ago`;
      if (diff < 86400000) return `${Math.floor(diff / 3600000)}h ago`;
      return d.toLocaleDateString(undefined, {
        month: 'short',
        day: 'numeric',
      });
    } catch {
      return dateStr;
    }
  }

  function statusColor(status: string): string {
    const map: Record<string, string> = {
      running: tokens.success,
      in_progress: tokens.success,
      completed: '#74b9ff',
      failed: tokens.danger,
      error: tokens.danger,
      cancelled: tokens.fgMuted,
      waiting_approval: tokens.warn,
    };
    return map[status] || tokens.fgMuted;
  }

  function statusLabel(status: string): string {
    return status.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
  }

  // Connecting / verifying full-screen spinner
  if (phase === 'connecting' || phase === 'verifying') {
    return (
      <div
        style={{
          display: 'flex',
          flexDirection: 'column',
          height: '100dvh',
          padding: '0 0.75rem',
          alignItems: 'center',
          justifyContent: 'center',
        }}
      >
        <div
          style={{
            ...glassCard,
            padding: '2rem 1.5rem',
            textAlign: 'center',
            maxWidth: 380,
          }}
        >
          <div
            style={{
              width: 36,
              height: 36,
              borderRadius: '50%',
              border: `3px solid ${tokens.border}`,
              borderTopColor: tokens.accent,
              animation: 'spin 0.9s linear infinite',
              margin: '0 auto 1rem',
            }}
          />
          <div style={{ color: tokens.fgDim, fontSize: '0.9rem' }}>
            {phase === 'connecting'
              ? 'Connecting to your Aura desktop…'
              : 'Verifying with your Aura desktop…'}
          </div>
          <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
        </div>
      </div>
    );
  }

  // Unavailable full-screen card
  if (phase === 'unavailable') {
    return (
      <div
        style={{
          display: 'flex',
          flexDirection: 'column',
          height: '100dvh',
          padding: '0 0.75rem',
          alignItems: 'center',
          justifyContent: 'center',
        }}
      >
        <div
          style={{
            ...glassCard,
            padding: '1.5rem',
            textAlign: 'center',
            maxWidth: 380,
            width: '100%',
          }}
        >
          <div
            style={{
              fontSize: '1.1rem',
              fontWeight: 600,
              color: tokens.danger,
              marginBottom: 4,
            }}
          >
            Previous desktop unavailable
          </div>
          <div
            style={{
              color: tokens.fgDim,
              fontSize: '0.9rem',
              marginBottom: '1rem',
            }}
          >
            {verifyError || 'Could not reach your Aura desktop.'}
          </div>
          <button
            onClick={goToLogin}
            style={{
              width: '100%',
              padding: '0.75rem 1rem',
              background: tokens.accent,
              color: '#0a0f1f',
              border: 'none',
              borderRadius: 10,
              fontSize: '0.9rem',
              fontWeight: 600,
              marginBottom: '0.5rem',
              cursor: 'pointer',
            }}
          >
            Go to Login
          </button>
          <button
            onClick={retry}
            style={{
              width: '100%',
              padding: '0.75rem 1rem',
              background: 'transparent',
              color: tokens.fg,
              border: `1px solid ${tokens.borderStrong}`,
              borderRadius: 10,
              fontSize: '0.9rem',
              fontWeight: 500,
              cursor: 'pointer',
            }}
          >
            Retry
          </button>
        </div>
      </div>
    );
  }

  // Connected — normal Receipts UI
  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        height: '100dvh',
        padding: '0 0.75rem',
      }}
    >
      {/* Header */}
      <header
        style={{
          ...glassCard,
          margin: '0.75rem 0 0.5rem',
          padding: '0.75rem 1rem',
          display: 'flex',
          alignItems: 'center',
          gap: '0.75rem',
        }}
      >
        <button
          onClick={() => navigate(-1)}
          aria-label="Back"
          style={{
            background: 'transparent',
            border: 'none',
            color: tokens.fgDim,
            fontSize: '1.4rem',
            padding: '0.1rem 0.4rem',
          }}
        >
          ←
        </button>
        <div style={{ flex: 1, fontWeight: 600, fontSize: '0.95rem' }}>
          Receipts
        </div>
        <span style={statusPillStyle('connected')}>● Online</span>
      </header>

      {/* Error banner */}
      {fetchError && (
        <div
          style={{
            padding: '0.55rem 0.85rem',
            background: 'rgba(247,118,142,0.08)',
            border: `1px solid ${tokens.danger}`,
            color: tokens.danger,
            borderRadius: 10,
            fontSize: '0.85rem',
            marginBottom: '0.5rem',
          }}
        >
          {fetchError}
        </div>
      )}

      {/* Main scrollable area */}
      <main style={{ flex: 1, overflow: 'auto', padding: '0.25rem 0 1rem' }}>
        {/* Loading */}
        {loading && (
          <div
            style={{
              textAlign: 'center',
              color: tokens.fgMuted,
              padding: '2rem 1rem',
              fontSize: '0.9rem',
            }}
          >
            Loading…
          </div>
        )}

        {/* Error with retry */}
        {!loading && fetchError && receipts.length === 0 && (
          <div style={{ textAlign: 'center', padding: '1rem' }}>
            <button
              onClick={() => {
                setFetchError('');
                setLoading(true);
                const desktopId =
                  sessionStorage.getItem('companion_desktop_id') ||
                  CompanionSocket.getStoredSafeContext().desktop_id ||
                  '';
                if (desktopId) {
                  socket.send('receipt.list_recent', {}, desktopId);
                }
              }}
              style={{
                padding: '0.5rem 1rem',
                background: 'transparent',
                color: tokens.fg,
                border: `1px solid ${tokens.borderStrong}`,
                borderRadius: 10,
                fontSize: '0.85rem',
                fontWeight: 500,
                cursor: 'pointer',
              }}
            >
              Retry
            </button>
          </div>
        )}

        {/* Empty state */}
        {!loading && !fetchError && receipts.length === 0 && (
          <div
            style={{
              textAlign: 'center',
              marginTop: '2rem',
              color: tokens.fgMuted,
              padding: '1rem',
            }}
          >
            <div
              style={{
                fontSize: '2rem',
                color: tokens.accent,
                opacity: 0.45,
                marginBottom: '0.5rem',
              }}
            >
              ⌗
            </div>
            <div style={{ fontSize: '0.95rem', color: tokens.fgDim }}>
              No receipts yet
            </div>
            <div style={{ fontSize: '0.8rem', marginTop: 6 }}>
              Run receipts will appear here after a drone or worker completes.
            </div>
          </div>
        )}

        {/* Receipt cards */}
        {!loading &&
          receipts.map((receipt) => (
            <div
              key={receipt.run_id}
              style={{
                ...glassCard,
                padding: '0.85rem 1rem',
                marginBottom: '0.5rem',
              }}
            >
              <div
                style={{
                  display: 'flex',
                  justifyContent: 'space-between',
                  alignItems: 'flex-start',
                  marginBottom: '0.3rem',
                }}
              >
                <div
                  style={{
                    fontWeight: 600,
                    fontSize: '0.9rem',
                    minWidth: 0,
                    flex: 1,
                    whiteSpace: 'nowrap',
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                  }}
                >
                  {receipt.label}
                </div>
                <div
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: '0.35rem',
                    flexShrink: 0,
                    marginLeft: '0.5rem',
                  }}
                >
                  <span
                    style={{
                      width: 7,
                      height: 7,
                      borderRadius: '50%',
                      background: statusColor(receipt.status),
                      flexShrink: 0,
                    }}
                  />
                  <span
                    style={{
                      fontSize: '0.78rem',
                      color: statusColor(receipt.status),
                      fontWeight: 500,
                    }}
                  >
                    {statusLabel(receipt.status)}
                  </span>
                  <span
                    style={{
                      fontSize: '0.72rem',
                      color: tokens.fgMuted,
                      whiteSpace: 'nowrap',
                    }}
                  >
                    {formatDate(receipt.completed_at)}
                  </span>
                </div>
              </div>
              {receipt.summary && (
                <div
                  style={{
                    fontSize: '0.8rem',
                    color: tokens.fgMuted,
                    lineHeight: 1.4,
                    marginBottom: '0.3rem',
                    display: '-webkit-box',
                    WebkitLineClamp: 2,
                    WebkitBoxOrient: 'vertical',
                    overflow: 'hidden',
                  }}
                >
                  {receipt.summary}
                </div>
              )}
              <div style={{ display: 'flex', gap: '0.4rem', marginTop: '0.2rem' }}>
                <span
                  style={{
                    fontSize: '0.65rem',
                    padding: '0.1rem 0.45rem',
                    borderRadius: 6,
                    background: 'rgba(255,255,255,0.06)',
                    border: `1px solid ${tokens.border}`,
                    color: tokens.fgMuted,
                    fontWeight: 600,
                    letterSpacing: '0.02em',
                  }}
                >
                  {receipt.kind === 'drone'
                    ? 'Drone'
                    : receipt.kind === 'worker'
                      ? 'Worker'
                      : receipt.kind}
                </span>
              </div>
            </div>
          ))}
      </main>
    </div>
  );
}

export default ReceiptsScreen;
