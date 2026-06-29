/// Pure relay URL resolution helpers.

export const HOSTED_RELAY_DEFAULT = 'wss://aura-companion-relay.carpsedema.workers.dev/ws';

const LOCAL_HOSTNAMES = new Set(['localhost', '127.0.0.1', '::1', '0.0.0.0']);

/**
 * Return true if the given WebSocket URL points to a localhost address.
 */
export function isLocalRelayUrl(url: string): boolean {
  try {
    const u = new URL(url.startsWith('ws') ? url : `ws://${url}`);
    return LOCAL_HOSTNAMES.has(u.hostname);
  } catch {
    return false;
  }
}

/**
 * Return true if the current browser origin is localhost / 127.0.0.1 / ::1 / 0.0.0.0.
 */
export function isLocalOrigin(): boolean {
  return LOCAL_HOSTNAMES.has(window.location.hostname);
}

/**
 * Resolve the effective relay URL, applying hosted-origin safety.
 *
 * Rules:
 *  1. envRelay (VITE_AURA_RELAY_WS_URL) always wins when set.
 *  2. On non-local origins, localhost qrRelay values are rejected.
 *  3. qrRelay is accepted on local origins or when it's non-local.
 *  4. Falls back to the hosted or local default based on origin.
 */
export function resolveRelayUrl(
  qrRelay: string,
  envRelay: string,
  originIsLocal: boolean,
): string {
  if (envRelay) return envRelay;
  if (!originIsLocal && qrRelay && isLocalRelayUrl(qrRelay)) {
    return HOSTED_RELAY_DEFAULT;
  }
  if (qrRelay) return qrRelay;
  return originIsLocal ? 'ws://localhost:8765' : HOSTED_RELAY_DEFAULT;
}
