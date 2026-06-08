import { useState, useRef, useEffect, useCallback } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import CompanionSocket, { socket } from '../api/socket';
import { tokens, glassCard, primaryButton, ghostButton, inputBase, statusPillStyle } from '../ui/theme';

type Phase = 'idle' | 'connecting' | 'connected' | 'pairing' | 'paired' | 'error';

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
      const token = await new Promise<string>((resolve, reject) => {
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
            if (Object.keys(safeCtx).length > 0) {
              CompanionSocket.setStoredSafeContext(safeCtx);
            }

            unsubConfirmed();
            unsubError();
            resolve(t);
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
    if (qrCode && qrDesktop && qrRelay && !alreadyPaired) {
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
    if (alreadyPaired) return;
    const host = window.location.hostname;
    const derivedRelay = `ws://${host}:8765`;
    setRelayUrl(derivedRelay);
    autoPairWithTicket(qrTicket, derivedRelay);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [qrTicket]);

  const handleReconnect = () => {
    setPhase('connecting');
    socket.connect(relayUrl);
    const unsub = socket.on('welcome', () => {
      unsub();
      navigate('/chat', { replace: true });
    });
  };

  const handleClearPairing = () => {
    socket.logout();
    CompanionSocket.setStoredToken('');
    sessionStorage.removeItem('companion_desktop_id');
    sessionStorage.removeItem('companion_desktop_name');
    forceRender(n => n + 1);
  };

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

  // Already paired
  if (alreadyPaired && !qrCode && !qrTicket) {
    return (
      <div style={pageWrap} className="fade-in">
        <Wordmark />
        <div style={card}>
          <div style={{ textAlign: 'center', color: tokens.fgDim, fontSize: '0.9rem' }}>
            Already paired with a desktop.
          </div>
          <button onClick={handleReconnect} style={primaryButton}>Reconnect</button>
          <button onClick={handleClearPairing} style={ghostButton}>Pair a different desktop</button>
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
            <div style={{ fontSize: '1.1rem', fontWeight: 600, letterSpacing: '0.01em' }}>Pair your phone</div>
            <div style={{ color: tokens.fgDim, fontSize: '0.8rem', marginTop: 4 }}>
              {desktopName ? `with ${desktopName}` : 'Connect to a running Aura desktop'}
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
            <Field label="Relay URL">
              <input
                value={relayUrl}
                onChange={e => setRelayUrl(e.target.value)}
                placeholder="ws://192.168.1.x:8765"
                style={inputBase}
                disabled={phase === 'connecting' || phase === 'connected' || phase === 'pairing'}
              />
            </Field>

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
                {phase === 'connecting' ? 'Connecting…' : 'Connect to Relay'}
              </button>
            )}

            {phase === 'connected' && (
              <>
                <Field label="Pairing code">
                  <input
                    value={pairingCode}
                    onChange={e => setPairingCode(e.target.value.toUpperCase().replace(/[^A-Z0-9]/g, '').slice(0, 6))}
                    placeholder="ABC234"
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

                <Field label="Desktop ID (from QR)">
                  <input
                    value={desktopId}
                    onChange={e => setDesktopId(e.target.value)}
                    placeholder="desktop_xxxxxxxx"
                    style={{ ...inputBase, fontSize: '0.8rem', fontFamily: '"JetBrains Mono", monospace' }}
                  />
                </Field>

                <Field label="This phone's name">
                  <input
                    value={phoneName}
                    onChange={e => setPhoneName(e.target.value)}
                    placeholder="My Phone"
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

      <div style={{ marginTop: '1.25rem', color: tokens.fgMuted, fontSize: '0.72rem', textAlign: 'center' }}>
        Tip: scan the QR shown in Aura → Settings → Companion.
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
  if (p === 'connecting') return 'Connecting';
  if (p === 'connected') return 'Online';
  if (p === 'pairing') return 'Pairing';
  if (p === 'paired') return 'Paired';
  return 'Offline';
}

export default LoginScreen;
