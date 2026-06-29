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
 * Priority:
 *  1. Non-local qrRelay always wins — trusted regardless of origin (self-hosted/custom relay).
 *  2. envRelay (VITE_AURA_RELAY_WS_URL) — baked at build time for hosted deploy.
 *  3. Local qrRelay accepted on local origins for port fallback.
 *  4. Falls back to the hosted or local default based on origin.
 */
export function resolveRelayUrl(
  qrRelay: string,
  envRelay: string,
  originIsLocal: boolean,
): string {
  // Non-local qrRelay always wins — trusted on any origin
  if (qrRelay && !isLocalRelayUrl(qrRelay)) return qrRelay;
  // Env var override
  if (envRelay) return envRelay;
  // Local origin accepts local qrRelay (supports port fallback)
  if (qrRelay && originIsLocal) return qrRelay;
  // Fallback to origin-appropriate default
  return originIsLocal ? 'ws://localhost:8765' : HOSTED_RELAY_DEFAULT;
}
