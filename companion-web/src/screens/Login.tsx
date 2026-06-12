import { useState, useRef, useEffect, useCallback } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import CompanionSocket, { socket } from '../api/socket';
import { tokens, glassCard, primaryButton, ghostButton, inputBase, statusPillStyle } from '../ui/theme';

type Phase = 'idle' | 'checking' | 'connecting' | 'connected' | 'pairing' | 'paired' | 'error' | 'unavailable';

function LoginScreen() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();

  // QR/URL params — when phone opens the pair URL, all of these are pre-filled.
  const qrRelay = searchParams.get('relay') || '';
  const qrDesktop = searchParams.get('desktop') || '';
  const qrDesktopName = searchParams.get('name') || '';
  const qrCode = searchParams.get('code') || '';
  const qrTicket = searchParams.get('ticket') || '';

  const [relayUrl, setRelayUrl] = useState(qrRelay || 'ws://localhost:8765');
  const [pairingCode, setPairingCode] = useState(qrCode);
  const [desktopId, setDesktopId] = useState(qrDesktop);
  const [desktopName] = useState(qrDesktopName);
  const [phoneName, setPhoneName] = useState('Aura Companion');
  const [phase, setPhase] = useState<Phase>('idle');
  const [error, setError] = useState('');
  const forceRender = useState(0)[1];
  const alreadyPaired = CompanionSocket.isPaired();
  const timeoutRef = useRef<ReturnType<typeof setTimeout>>();
  const autoStartedRef = useRef(false);
  const clearedOldPairing = useRef(false);
  const autoCheckRef = useRef(false);

  // Clear old pairing state when QR/ticket params are present, so the scanned
  // QR flow wins over any stale stored token/context.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const hasQrParams = !!(params.get('ticket') || (params.get('code') && params.get('desktop') && params.get('relay')));
    if (hasQrParams && alreadyPaired && !clearedOldPairing.current) {
      clearedOldPairing.current = true;
      socket.logout();
      CompanionSocket.clearStoredState();
      sessionStorage.removeItem('companion_desktop_id');
      sessionStorage.removeItem('companion_desktop_name');
      forceRender(n => n + 1);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Forget the URL params after we read them so a refresh doesn't re-trigger.
  useEffect(() => {
    if (qrCode || qrDesktop || qrRelay || qrTicket) {
      // Strip the params from the URL bar without re-running effects.
      setSearchParams({}, { replace: true });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => () => {
    if (timeoutRef.current) clearTimeout(timeoutRef.current);
  }, []);

  // Auto-connect on mount for non-paired users
  useEffect(() => {
    if (alreadyPaired || qrCode || qrTicket) return;
    if (socket.connected || socket.connecting) return;
    const defaultRelay = import.meta.env.VITE_AURA_RELAY_WS_URL || 'ws://localhost:8765';
    setRelayUrl(defaultRelay);
    handleConnect().catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Auto-check reachability on mount for already-paired users
  useEffect(() => {
    if (!alreadyPaired || qrCode || qrTicket) return;
    if (autoCheckRef.current) return;
    autoCheckRef.current = true;
    if (socket.connected) {
      setPhase('connected');
      return;
    }
    checkReachable();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const onFocus = () => forceRender(n => n + 1);
    window.addEventListener('focus', onFocus);
    return () => window.removeEventListener('focus', onFocus);
  }, [forceRender]);

  const handleConnect = useCallback((): Promise<void> => new Promise((resolve, reject) => {
    setPhase('connecting');
    setError('');
    socket.connect(relayUrl);
    const unsub = socket.on('welcome', () => {
      unsub();
      if (timeoutRef.current) {
        clearTimeout(timeoutRef.current);
        timeoutRef.current = undefined;
      }
      setPhase('connected');
      resolve();
    });
    timeoutRef.current = setTimeout(() => {
      unsub();
      setPhase(p => (p === 'connecting' ? 'idle' : p));
      setError('Connection timed out — check the relay URL.');
      reject(new Error('timeout'));
    }, 10000);
  }), [relayUrl]);

  const checkReachable = useCallback(async () => {
    const storedRelay = CompanionSocket.getStoredRelayUrl();
    const effectiveRelay = storedRelay || import.meta.env.VITE_AURA_RELAY_WS_URL || '';
    if (!effectiveRelay) {
      setPhase('unavailable');
      setError('No relay configured.');
      return;
    }

    const safeCtx = CompanionSocket.getStoredSafeContext();
    const storedDesktopId = safeCtx.desktop_id || '';
    if (!storedDesktopId) {
      setPhase('unavailable');
      setError('No paired desktop found.');
      return;
    }

    setPhase('checking');
    setError('');
    socket.connect(effectiveRelay);

    // Step 1: wait for welcome
    try {
      await new Promise<void>((resolve, reject) => {
        let unsub: (() => void) | null = null;
        unsub = socket.on('welcome', () => {
          unsub?.();
          resolve();
        });
        timeoutRef.current = setTimeout(() => {
          unsub?.();
          reject(new Error('Connection timed out — relay not reachable.'));
        }, 10000);
      });
    } catch (e: any) {
      setPhase('unavailable');
      setError(e?.message || 'Connection timed out — relay not reachable.');
      return;
    }

    // Step 2: verify authenticated pairing + desktop reachable
    try {
      await new Promise<void>((resolve, reject) => {
        let unsubAck: (() => void) | null = null;
        let unsubAuthErr: (() => void) | null = null;
        let unsubError: (() => void) | null = null;

        const cleanup = () => {
          if (timeoutRef.current) { clearTimeout(timeoutRef.current); timeoutRef.current = undefined; }
          unsubAck?.();
          unsubAuthErr?.();
          unsubError?.();
        };

        unsubAck = socket.on('companion.verify_ack', (data: any) => {
          // Update safe context with fresh data from desktop
          const prev = CompanionSocket.getStoredSafeContext();
          const updated: any = { ...prev };
          if (data.payload?.desktop_name) {
            updated.desktop_name = data.payload.desktop_name;
            sessionStorage.setItem('companion_desktop_name', data.payload.desktop_name);
          }
          if (data.payload?.project_id !== undefined) updated.project_id = data.payload.project_id;
          if (data.payload?.conversation_id !== undefined) updated.conversation_id = data.payload.conversation_id;
          CompanionSocket.setStoredSafeContext(updated);

          cleanup();
          resolve();
        });

        unsubAuthErr = socket.on('auth.error', () => {
          cleanup();
          reject(new Error('Stored pairing is no longer valid. Pair again.'));
        });

        unsubError = socket.on('error', (data: any) => {
          cleanup();
          reject(new Error(data.payload?.message || 'Desktop not reachable.'));
        });

        socket.send('companion.verify', {}, storedDesktopId);

        timeoutRef.current = setTimeout(() => {
          cleanup();
          reject(new Error('Verification timed out — desktop not reachable.'));
        }, 10000);
      });

      setPhase('connected');
      setError('');
    } catch (e: any) {
      setPhase('unavailable');
      setError(e?.message || 'Desktop not reachable.');
    }
  }, []);

  const handlePairAgain = useCallback(() => {
    socket.logout();
    CompanionSocket.clearStoredState();
    sessionStorage.removeItem('companion_desktop_id');
    sessionStorage.removeItem('companion_desktop_name');
    setPhase('idle');
    forceRender(n => n + 1);
  }, [forceRender]);

  const handlePair = useCallback(async () => {
    if (!pairingCode) {
      setError('Enter the 6-character code from your desktop.');
      return;
    }
    if (!desktopId) {
      setError('Missing desktop ID — scan the QR from your desktop again.');
      return;
    }
    setPhase('pairing');
    setError('');
    try {
      await socket.pair(pairingCode, relayUrl, desktopId, phoneName);
      // Persist desktop identity so Chat can route to it after pair.
      sessionStorage.setItem('companion_desktop_id', desktopId);
      if (desktopName) sessionStorage.setItem('companion_desktop_name', desktopName);
      setPhase('paired');
      const safeCtx = CompanionSocket.getStoredSafeContext();
      if (safeCtx.project_id) {
        navigate('/chat', { replace: true });
      } else {
        navigate('/projects', { replace: true });
      }
    } catch (e: any) {
      setPhase('connected');
      setError(e?.message || 'Pairing failed — generate a new code on desktop.');
    }
  }, [pairingCode, desktopId, desktopName, phoneName, relayUrl, navigate]);

  const autoPairWithTicket = useCallback(async (ticket: string, relay: string) => {
    if (autoStartedRef.current) return;
    autoStartedRef.current = true;

    try {
      setPhase('connecting');
      socket.connect(relay);

      await new Promise<void>((resolve, reject) => {
        const unsub = socket.on('welcome', () => {
          unsub();
          resolve();
        });
        setTimeout(() => { unsub(); reject(new Error('timeout')); }, 10000);
      });

      setPhase('connected');
      await new Promise(r => setTimeout(r, 200));

      setPhase('pairing');
      await new Promise<void>((resolve, reject) => {
        const unsubConfirmed = socket.on('pair.confirmed', (data: any) => {
          const t = data.payload?.token;
          if (t) {
            CompanionSocket.setStoredToken(t);
            (socket as any).deviceToken = t;

            const safeCtx: any = {};
            if (data.payload?.desktop_name) safeCtx.desktop_name = data.payload.desktop_name;
            if (data.payload?.project_id) safeCtx.project_id = data.payload.project_id;
            if (data.payload?.project_name) safeCtx.project_name = data.payload.project_name;
            if (data.payload?.conversation_id) safeCtx.conversation_id = data.payload.conversation_id;
            if (data.payload?.phone_id) safeCtx.phone_id = data.payload.phone_id;
            const ticketDesktopId = data.payload?.scoped_to || data.payload?.desktop_id || '';
            if (ticketDesktopId) {
              safeCtx.desktop_id = ticketDesktopId;
              try { sessionStorage.setItem('companion_desktop_id', ticketDesktopId); } catch {}
            }
            if (Object.keys(safeCtx).length > 0) {
              CompanionSocket.setStoredSafeContext(safeCtx);
            }

            unsubConfirmed();
            unsubError();
            resolve();
          } else {
            reject(new Error('No token in pair.confirmed'));
          }
        });
        const unsubError = socket.on('pair.error', (data: any) => {
          unsubConfirmed();
          unsubError();
          reject(new Error(data.payload?.message || 'Pairing failed'));
        });

        socket.send('pair.connect', { ticket, device_name: phoneName || 'Aura Companion' });

        setTimeout(() => {
          unsubConfirmed();
          unsubError();
          reject(new Error('Pairing timed out'));
        }, 30000);
      });

      setPhase('paired');

      const safeCtx = CompanionSocket.getStoredSafeContext();
      if (safeCtx.project_id) {
        navigate('/chat', { replace: true });
      } else {
        navigate('/projects', { replace: true });
      }
    } catch (e: any) {
      setPhase('connected');
      setError(e?.message || 'Auto-pairing failed. Try manual pairing below.');
    }
  }, [navigate, phoneName]);

  // Auto-pair: if we landed here with all URL params, run the whole flow once.
  useEffect(() => {
    if (autoStartedRef.current) return;
    if (qrCode && qrDesktop && qrRelay) {
      autoStartedRef.current = true;
      (async () => {
        try {
          await handleConnect();
          await new Promise(r => setTimeout(r, 100));  // settle
          await handlePair();
        } catch {
          // surfaced via error state
        }
      })();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Ticket-based auto-pair
  useEffect(() => {
    if (!qrTicket) return;
    if (autoStartedRef.current) return;
    const configuredRelay = import.meta.env.VITE_AURA_RELAY_WS_URL || 'ws://localhost:8765';
    setRelayUrl(configuredRelay);
    autoPairWithTicket(qrTicket, configuredRelay);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [qrTicket]);



  const pageWrap: React.CSSProperties = {
    minHeight: '100dvh',
    display: 'flex',
    flexDirection: 'column',
    padding: '2.5rem 1.25rem 2rem',
    alignItems: 'center',
  };

  const card: React.CSSProperties = {
    ...glassCard,
    width: '100%',
    maxWidth: 420,
    padding: '1.5rem 1.25rem',
    display: 'flex',
    flexDirection: 'column',
    gap: '1rem',
  };

  // Already paired (no QR params)
  if (alreadyPaired && !qrCode && !qrTicket) {
    const storedDesktopName = sessionStorage.getItem('companion_desktop_name') 
      || CompanionSocket.getStoredSafeContext().desktop_name 
      || '';
    const safeCtx = CompanionSocket.getStoredSafeContext();

    if (phase === 'checking') {
      return (
        <div style={pageWrap} className="fade-in">
          <Wordmark />
          <div style={card}>
            <div style={{ textAlign: 'center', padding: '1.5rem 0', color: tokens.fgDim }}>
              <div style={{
                width: 36, height: 36, borderRadius: '50%',
                border: `3px solid ${tokens.border}`,
                borderTopColor: tokens.accent,
                animation: 'spin 0.9s linear infinite',
                margin: '0 auto 1rem',
              }} />
              <div style={{ fontSize: '0.9rem' }}>Checking connection to your Aura desktop…</div>
              <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
            </div>
          </div>
        </div>
      );
    }

    if (phase === 'connected') {
      return (
        <div style={pageWrap} className="fade-in">
          <Wordmark />
          <div style={card}>
            <div style={{ textAlign: 'center' }}>
              <div style={{ fontSize: '1.1rem', fontWeight: 600, color: tokens.success, marginBottom: 4 }}>
                Connected
              </div>
              <div style={{ color: tokens.fgDim, fontSize: '0.9rem' }}>
                {storedDesktopName
                  ? `Connected to ${storedDesktopName}`
                  : 'Connected to your desktop'}
              </div>
            </div>
            <button
              onClick={() => {
                if (safeCtx.project_id) {
                  navigate('/chat', { replace: true });
                } else {
                  navigate('/projects', { replace: true });
                }
              }}
              style={primaryButton}
            >
              Continue
            </button>
            <button onClick={handlePairAgain} style={ghostButton}>
              Pair a different desktop
            </button>
          </div>
        </div>
      );
    }

    if (phase === 'unavailable') {
      return (
        <div style={pageWrap} className="fade-in">
          <Wordmark />
          <div style={card}>
            <div style={{ textAlign: 'center' }}>
              <div style={{ fontSize: '1.1rem', fontWeight: 600, color: tokens.danger, marginBottom: 4 }}>
                Previous desktop unavailable
              </div>
              <div style={{ color: tokens.fgDim, fontSize: '0.9rem' }}>
                {error || 'Could not reach your Aura desktop.'}
              </div>
            </div>
            <button onClick={handlePairAgain} style={primaryButton}>
              Pair again
            </button>
            <button onClick={checkReachable} style={ghostButton}>
              Retry
            </button>
          </div>
        </div>
      );
    }

    // Fallback — should not normally happen
    return (
      <div style={pageWrap} className="fade-in">
        <Wordmark />
        <div style={card}>
          <div style={{ textAlign: 'center', padding: '1.5rem 0', color: tokens.fgDim }}>
            <div style={{
              width: 36, height: 36, borderRadius: '50%',
              border: `3px solid ${tokens.border}`,
              borderTopColor: tokens.accent,
              animation: 'spin 0.9s linear infinite',
              margin: '0 auto 1rem',
            }} />
            <div style={{ fontSize: '0.9rem' }}>Checking connection to your Aura desktop…</div>
            <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
          </div>
        </div>
      </div>
    );
  }

  const isAutoPairing = (qrCode || qrTicket) && (phase === 'connecting' || phase === 'pairing');

  return (
    <div style={pageWrap} className="fade-in">
      <Wordmark />

      <div style={card}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '0.75rem' }}>
          <div>
            <div style={{ fontSize: '1.1rem', fontWeight: 600, letterSpacing: '0.01em' }}>Pair with desktop</div>
            <div style={{ color: tokens.fgDim, fontSize: '0.8rem', marginTop: 4 }}>
              Scan the QR from Aura Desktop → Settings → Companion
            </div>
          </div>
          <span style={statusPillStyle(
            phase === 'connected' || phase === 'paired' ? 'connected'
            : phase === 'connecting' || phase === 'pairing' ? 'connecting'
            : 'disconnected'
          )}>
            ● {labelFor(phase)}
          </span>
        </div>

        {error && (
          <div style={{
            padding: '0.6rem 0.85rem',
            background: 'rgba(247,118,142,0.10)',
            border: `1px solid ${tokens.danger}`,
            color: tokens.danger,
            borderRadius: 10,
            fontSize: '0.85rem',
          }}>
            {error}
          </div>
        )}

        {isAutoPairing ? (
          <div style={{ textAlign: 'center', padding: '1.5rem 0', color: tokens.fgDim }}>
            <div style={{
              width: 36, height: 36, borderRadius: '50%',
              border: `3px solid ${tokens.border}`,
              borderTopColor: tokens.accent,
              animation: 'spin 0.9s linear infinite',
              margin: '0 auto 1rem',
            }} />
            <div style={{ fontSize: '0.9rem' }}>
              {qrTicket
                ? (phase === 'connecting' ? 'Resolving pairing ticket…' : 'Pairing with desktop…')
                : (phase === 'connecting' ? 'Connecting to relay…' : 'Pairing with desktop…')}
            </div>
            <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
          </div>
        ) : (
          <>
            {phase !== 'connected' && (
              <button
                onClick={() => { handleConnect().catch(() => {}); }}
                disabled={phase === 'connecting'}
                style={{
                  ...primaryButton,
                  background: phase === 'connecting' ? tokens.borderStrong : tokens.accent,
                  color: phase === 'connecting' ? tokens.fgMuted : '#0a0f1f',
                }}
              >
                {phase === 'connecting' ? 'Connecting…' : 'Connect'}
              </button>
            )}

            {phase === 'connected' && (
              <>
                <Field label="Pairing code">
                  <input
                    value={pairingCode}
                    onChange={e => setPairingCode(e.target.value.toUpperCase().replace(/[^A-Z0-9]/g, '').slice(0, 6))}
                    placeholder="Enter code from desktop"
                    style={{
                      ...inputBase,
                      textAlign: 'center',
                      letterSpacing: '0.5rem',
                      fontSize: '1.4rem',
                      fontWeight: 600,
                      fontFamily: '"JetBrains Mono", Consolas, monospace',
                    }}
                    maxLength={6}
                    autoFocus
                  />
                </Field>

                {!qrCode && !qrTicket && (
                  <Field label="Desktop ID">
                    <input
                      value={desktopId}
                      onChange={e => setDesktopId(e.target.value)}
                      placeholder="desktop_xxxxxxxx"
                      style={{ ...inputBase, fontSize: '0.8rem', fontFamily: '"JetBrains Mono", monospace' }}
                    />
                  </Field>
                )}

                <Field label="Phone name">
                  <input
                    value={phoneName}
                    onChange={e => setPhoneName(e.target.value)}
                    placeholder="Aura Companion"
                    style={inputBase}
                  />
                </Field>

                <button onClick={handlePair} style={primaryButton}>
                  Pair
                </button>
              </>
            )}
          </>
        )}
      </div>
    </div>
  );
}

function Wordmark() {
  return (
    <div style={{ textAlign: 'center', marginBottom: '1.5rem' }}>
      <div style={{
        fontSize: '0.7rem', color: tokens.accent, letterSpacing: '0.2em',
        fontWeight: 700,
      }}>
        AURA
      </div>
      <div style={{ fontSize: '1.5rem', fontWeight: 600, letterSpacing: '0.01em', marginTop: 2 }}>
        Companion
      </div>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div style={{
        fontSize: '0.68rem', color: tokens.fgMuted,
        textTransform: 'uppercase', letterSpacing: '0.12em',
        marginBottom: 6,
      }}>
        {label}
      </div>
      {children}
    </div>
  );
}

function labelFor(p: Phase): string {
  if (p === 'idle') return 'Idle';
  if (p === 'checking') return 'Checking…';
  if (p === 'connecting') return 'Connecting';
  if (p === 'connected') return 'Online';
  if (p === 'pairing') return 'Pairing';
  if (p === 'paired') return 'Paired';
  if (p === 'unavailable') return 'Unavailable';
  return 'Offline';
}

export default LoginScreen;
