/**
 * Aura Companion Relay — Cloudflare Worker + Durable Object
 *
 * Single WebSocket endpoint: /ws
 * All relay state lives in one RelayDO instance (global singleton via "global" stub).
 *
 * Desktop auth: desktop hello must include `secret` matching DESKTOP_SECRET env var.
 * Phones auth: via JWT token issued by the desktop after pairing (verified with RELAY_SECRET).
 */

export interface Env {
  RELAY: DurableObjectNamespace;
  /** Shared secret desktops must send in hello.secret to be trusted. */
  DESKTOP_SECRET: string;
  /** HMAC secret for signing/verifying phone JWT tokens. */
  RELAY_SECRET: string;
}

// ---------------------------------------------------------------------------
// Worker entry point — routes /ws to the singleton Durable Object
// ---------------------------------------------------------------------------

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);

    if (url.pathname === "/ws") {
      const upgradeHeader = request.headers.get("Upgrade");
      if (!upgradeHeader || upgradeHeader.toLowerCase() !== "websocket") {
        return new Response("Expected WebSocket upgrade", { status: 426 });
      }
      const id = env.RELAY.idFromName("global");
      const stub = env.RELAY.get(id);
      return stub.fetch(request);
    }

    if (url.pathname === "/health") {
      const id = env.RELAY.idFromName("global");
      const stub = env.RELAY.get(id);
      return stub.fetch(request);
    }

    return new Response("Not found", { status: 404 });
  },
} satisfies ExportedHandler<Env>;

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface DeviceSession {
  ws: WebSocket;
  deviceType: "desktop" | "phone";
  displayName: string;
  authenticated: boolean;
  /** Role: "desktop" | "phone" */
  role: string;
  /** For phones: which desktop they are paired with */
  pairedDesktop: string | null;
  lastSeen: string;
}

interface Ticket {
  desktopId: string;
  code: string;
  desktopName: string;
  projectId: string;
  conversationId: string;
  expiresAt: number; // ms epoch
}

interface PairAttempt {
  ticket: string;
  desktopId: string;
  phoneId: string;
  phoneName: string;
  originalMsgId: string;
  createdAt: number; // ms epoch
  expiresAt: number; // ms epoch
}

interface JwtPayload {
  role: string;
  desktop_id: string;
  device_name: string;
  exp: number;
}

// ---------------------------------------------------------------------------
// JWT — HMAC-SHA256, no external dependency
// ---------------------------------------------------------------------------

function b64urlDecode(s: string): Uint8Array {
  const padded = s.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(s.length / 4) * 4, "=");
  const bin = atob(padded);
  return Uint8Array.from(bin, (c) => c.charCodeAt(0));
}

function b64urlEncode(data: Uint8Array): string {
  return btoa(String.fromCharCode(...data))
    .replace(/=/g, "")
    .replace(/\+/g, "-")
    .replace(/\//g, "_");
}

async function hmacKey(secret: string): Promise<CryptoKey> {
  return crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign", "verify"],
  );
}

async function verifyJwt(token: string, secret: string): Promise<JwtPayload | null> {
  const parts = token.split(".");
  if (parts.length !== 3) return null;
  const [header, body, sigB64] = parts;
  const data = `${header}.${body}`;
  const key = await hmacKey(secret);
  const sig = b64urlDecode(sigB64).buffer as ArrayBuffer;
  const valid = await crypto.subtle.verify("HMAC", key, sig, new TextEncoder().encode(data) as unknown as ArrayBuffer);
  if (!valid) return null;
  try {
    const payload = JSON.parse(new TextDecoder().decode(b64urlDecode(body))) as JwtPayload;
    if (payload.exp * 1000 < Date.now()) return null;
    return payload;
  } catch {
    return null;
  }
}

async function signJwt(payload: JwtPayload, secret: string): Promise<string> {
  const header = b64urlEncode(new TextEncoder().encode(JSON.stringify({ alg: "HS256", typ: "JWT" })));
  const body = b64urlEncode(new TextEncoder().encode(JSON.stringify(payload)));
  const data = `${header}.${body}`;
  const key = await hmacKey(secret);
  const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(data));
  const sigB64 = b64urlEncode(new Uint8Array(sig));
  return `${data}.${sigB64}`;
}

// ---------------------------------------------------------------------------
// Message helpers
// ---------------------------------------------------------------------------

function randomId(): string {
  return `evt_${crypto.randomUUID().replace(/-/g, "").slice(0, 12)}`;
}

function envelope(
  type: string,
  payload: unknown,
  opts: { desktopId?: string; projectId?: string; conversationId?: string; inResponseTo?: string } = {},
): string {
  return JSON.stringify({
    id: randomId(),
    type,
    desktop_id: opts.desktopId ?? "",
    project_id: opts.projectId ?? "",
    conversation_id: opts.conversationId ?? "",
    in_response_to: opts.inResponseTo ?? "",
    payload,
  });
}

function errorMsg(message: string, inResponseTo?: string): string {
  return envelope("error", { message }, { inResponseTo });
}

function authErrorMsg(inResponseTo?: string): string {
  return envelope(
    "auth.error",
    { message: "Device not paired. Send pair.connect to authenticate." },
    { inResponseTo },
  );
}

function validateEnvelope(msg: unknown): msg is Record<string, unknown> {
  if (typeof msg !== "object" || msg === null) return false;
  const m = msg as Record<string, unknown>;
  return typeof m["type"] === "string" && m["type"].length > 0;
}

// ---------------------------------------------------------------------------
// RelayDO — Durable Object holding all live relay state
// ---------------------------------------------------------------------------

export class RelayDO implements DurableObject {
  private readonly sessions = new Map<string, DeviceSession>();
  /** ticket string → Ticket */
  private readonly tickets = new Map<string, Ticket>();
  /** phone_id → desktop_id */
  private readonly pairedPhones = new Map<string, string>();
  /** original_msg_id (phone's pair.connect msg id) → PairAttempt */
  private readonly pairAttempts = new Map<string, PairAttempt>();

  private env!: Env;

  constructor(_state: DurableObjectState, env: Env) {
    this.env = env;
  }

  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);

    if (url.pathname === "/health") {
      const desktops = [...this.sessions.values()].filter((s) => s.deviceType === "desktop").length;
      const phones = [...this.sessions.values()].filter((s) => s.deviceType === "phone").length;
      return Response.json({ status: "ok", online_desktops: desktops, online_phones: phones });
    }

    if (url.pathname !== "/ws") {
      return new Response("Not found", { status: 404 });
    }

    const pair = new WebSocketPair();
    const [client, server] = Object.values(pair);
    this.handleSession(server as WebSocket);
    return new Response(null, { status: 101, webSocket: client as WebSocket });
  }

  private handleSession(ws: WebSocket): void {
    ws.accept();

    let deviceId = "";

    ws.addEventListener("message", async (evt: MessageEvent) => {
      const raw = typeof evt.data === "string" ? evt.data : new TextDecoder().decode(evt.data as ArrayBuffer);
      let msg: unknown;
      try {
        msg = JSON.parse(raw);
      } catch {
        ws.send(errorMsg("Invalid JSON"));
        return;
      }

      if (!validateEnvelope(msg)) {
        ws.send(errorMsg("Invalid envelope"));
        return;
      }

      // First message must be hello
      if (!deviceId) {
        await this.handleHello(ws, msg, raw).then((id) => {
          deviceId = id;
        });
        return;
      }

      const session = this.sessions.get(deviceId);
      if (!session) return;

      const msgType = String(msg["type"]);
      const msgId = typeof msg["id"] === "string" ? msg["id"] : undefined;

      // Auth gate
      const AUTH_SKIP = new Set([
        "hello", "welcome", "error", "system.online_list",
        "pair.connect", "pair.cancel", "pair.verify",
        "pair.confirmed", "pair.error",
      ]);
      if (!AUTH_SKIP.has(msgType) && !session.authenticated) {
        ws.send(authErrorMsg(msgId));
        return;
      }

      await this.dispatch(deviceId, session, ws, msg, msgType, msgId, raw);
    });

    ws.addEventListener("close", () => {
      if (deviceId) {
        this.sessions.delete(deviceId);
        this.broadcastOnline();
      }
    });

    ws.addEventListener("error", () => {
      if (deviceId) {
        this.sessions.delete(deviceId);
        this.broadcastOnline();
      }
    });
  }

  private async handleHello(
    ws: WebSocket,
    msg: Record<string, unknown>,
    _raw: string,
  ): Promise<string> {
    if (msg["type"] !== "hello") {
      ws.send(errorMsg("Expected hello"));
      ws.close();
      return "";
    }

    const deviceId = String(msg["device_id"] ?? "");
    const deviceType = String(msg["device_type"] ?? "phone");
    const displayName = String(msg["display_name"] ?? deviceId);

    if (!deviceId) {
      ws.send(errorMsg("Missing device_id"));
      ws.close();
      return "";
    }

    const session: DeviceSession = {
      ws,
      deviceType: deviceType === "desktop" ? "desktop" : "phone",
      displayName: displayName || deviceId,
      authenticated: false,
      role: "",
      pairedDesktop: null,
      lastSeen: new Date().toISOString(),
    };

    // Desktop auth: must present the shared secret
    if (deviceType === "desktop") {
      const secret = String(msg["secret"] ?? "");
      const expected = this.env.DESKTOP_SECRET ?? "";
      if (expected && secret !== expected) {
        ws.send(errorMsg("Desktop authentication failed"));
        ws.close();
        return "";
      }
      session.authenticated = true;
      session.role = "desktop";
    }

    // Phone auth: optional token in hello for pre-authenticated reconnects
    if (deviceType !== "desktop") {
      const token = String(msg["token"] ?? "");
      if (token) {
        const payload = await verifyJwt(token, this.env.RELAY_SECRET ?? "");
        if (payload) {
          session.authenticated = true;
          session.role = payload.role;
          const savedDesktop = payload.desktop_id;
          session.pairedDesktop = savedDesktop || null;
          if (savedDesktop) {
            this.pairedPhones.set(deviceId, savedDesktop);
          }
        }
      }
    }

    this.sessions.set(deviceId, session);

    ws.send(
      JSON.stringify({
        type: "welcome",
        payload: { device_id: deviceId, online_count: this.sessions.size },
      }),
    );

    this.broadcastOnline();
    return deviceId;
  }

  private async dispatch(
    deviceId: string,
    session: DeviceSession,
    ws: WebSocket,
    msg: Record<string, unknown>,
    msgType: string,
    msgId: string | undefined,
    raw: string,
  ): Promise<void> {
    switch (msgType) {
      case "ticket.register":
        this.handleTicketRegister(deviceId, session, ws, msg, msgId);
        break;
      case "pair.connect":
        await this.handlePairConnect(deviceId, ws, msg, msgId);
        break;
      case "pair.confirmed":
        await this.handlePairConfirmed(deviceId, ws, msg, msgId, raw);
        break;
      case "pair.paired_devices":
        this.handlePairPairedDevices(deviceId, ws, msgId);
        break;
      case "desktop.list":
        this.handleDesktopList(ws, msgId);
        break;
      default:
        await this.handleRoute(deviceId, session, ws, msg, msgId, raw);
        break;
    }
  }

  // -------------------------------------------------------------------------
  // ticket.register — desktop stores a short-lived single-use ticket
  // -------------------------------------------------------------------------

  private handleTicketRegister(
    deviceId: string,
    session: DeviceSession,
    ws: WebSocket,
    msg: Record<string, unknown>,
    msgId: string | undefined,
  ): void {
    if (session.role !== "desktop") {
      ws.send(errorMsg("Only desktops may register tickets", msgId));
      return;
    }

    const payload = (msg["payload"] ?? {}) as Record<string, unknown>;
    const ticket = String(payload["ticket"] ?? "");
    const code = String(payload["code"] ?? "");
    const desktopName = String(payload["desktop_name"] ?? "");
    const projectId = String(payload["project_id"] ?? "");
    const conversationId = String(payload["conversation_id"] ?? "");

    if (!ticket) {
      ws.send(errorMsg("Missing ticket", msgId));
      return;
    }

    // Purge expired tickets first (keep memory tidy)
    this.purgeExpiredTickets();

    this.tickets.set(ticket, {
      desktopId: deviceId,
      code,
      desktopName,
      projectId,
      conversationId,
      expiresAt: Date.now() + 300_000, // 5 min
    });

    ws.send(
      envelope("ticket.registered", { ticket }, { desktopId: deviceId, inResponseTo: msgId }),
    );
  }

  // -------------------------------------------------------------------------
  // pair.connect — phone presents a ticket; relay resolves it and forwards
  //                pair.verify to the correct desktop
  // -------------------------------------------------------------------------

  private async handlePairConnect(
    deviceId: string,
    ws: WebSocket,
    msg: Record<string, unknown>,
    msgId: string | undefined,
  ): Promise<void> {
    const payload = (msg["payload"] ?? {}) as Record<string, unknown>;
    const phoneName = String(payload["device_name"] ?? "Phone");
    const ticket = String(payload["ticket"] ?? "");

    let targetDesktopId = "";
    let pairingCode = "";

    if (ticket) {
      const data = this.consumeTicket(ticket);
      if (!data) {
        ws.send(
          envelope("pair.error", { message: "Invalid or expired ticket" }, { inResponseTo: msgId }),
        );
        return;
      }
      targetDesktopId = data.desktopId;
      pairingCode = data.code;
    } else {
      // Fallback: manual code + explicit desktop_id in envelope
      pairingCode = String(payload["code"] ?? "");
      targetDesktopId = String(msg["desktop_id"] ?? "");
    }

    if (!pairingCode || !targetDesktopId) {
      ws.send(
        envelope("pair.error", { message: "Missing pairing code or desktop_id" }, { inResponseTo: msgId }),
      );
      return;
    }

    const desktopSession = this.sessions.get(targetDesktopId);
    if (!desktopSession) {
      ws.send(
        envelope("pair.error", { message: "Desktop not online" }, { inResponseTo: msgId }),
      );
      return;
    }

    // Store pairing attempt bound to this desktop session
    const attempt: PairAttempt = {
      ticket: ticket || "",
      desktopId: targetDesktopId,
      phoneId: deviceId,
      phoneName,
      originalMsgId: msgId ?? "",
      createdAt: Date.now(),
      expiresAt: Date.now() + 300_000, // 5 min
    };
    this.pairAttempts.set(msgId ?? "", attempt);
    this.purgeExpiredPairAttempts();

    const verifyMsg = envelope(
      "pair.verify",
      {
        code: pairingCode,
        phone_id: deviceId,
        device_name: phoneName,
        original_msg_id: msgId ?? "",
      },
      { desktopId: targetDesktopId },
    );
    desktopSession.ws.send(verifyMsg);
  }

  // -------------------------------------------------------------------------
  // pair.confirmed — desktop confirms pairing; relay records it and forwards
  //                  the confirmation (with scoped_to) to the phone
  // -------------------------------------------------------------------------

  private async handlePairConfirmed(
    deviceId: string,
    ws: WebSocket,
    msg: Record<string, unknown>,
    _msgId: string | undefined,
    raw: string,
  ): Promise<void> {
    const payload = (msg["payload"] ?? {}) as Record<string, unknown>;
    const phoneId = String(payload["phone_id"] ?? "");
    const phoneName = String(payload["device_name"] ?? "Phone");

    // Look up the pairing attempt
    const lookupKey =
      typeof msg["in_response_to"] === "string" && (msg["in_response_to"] as string).length > 0
        ? (msg["in_response_to"] as string)
        : typeof payload["original_msg_id"] === "string"
          ? (payload["original_msg_id"] as string)
          : "";
    const attempt = lookupKey ? this.pairAttempts.get(lookupKey) : undefined;

    if (!attempt) {
      ws.send(
        envelope("pair.error", { message: "No matching pairing attempt" }, { inResponseTo: msg["id"] as string }),
      );
      return;
    }
    if (attempt.desktopId !== deviceId) {
      ws.send(
        envelope("pair.error", { message: "Desktop connection does not own this pairing attempt" }, { inResponseTo: msg["id"] as string }),
      );
      return;
    }
    if (Date.now() > attempt.expiresAt) {
      this.pairAttempts.delete(lookupKey);
      ws.send(
        envelope("pair.error", { message: "Pairing attempt expired" }, { inResponseTo: msg["id"] as string }),
      );
      return;
    }

    // Remove the attempt now that it is confirmed
    this.pairAttempts.delete(lookupKey);

    if (!phoneId) {
      ws.send(
        envelope("pair.error", { message: "Missing phone_id" }, { inResponseTo: msg["id"] as string }),
      );
      return;
    }

    // Create a relay-issued connect token for the phone
    const relayToken = await signJwt(
      {
        role: "phone",
        desktop_id: deviceId,
        device_name: phoneName,
        exp: Math.floor(Date.now() / 1000) + 86400 * 30, // 30 days
      },
      this.env.RELAY_SECRET ?? "",
    );

    // Mark phone session as authenticated
    const phoneSession = this.sessions.get(phoneId);
    if (phoneSession) {
      phoneSession.authenticated = true;
      phoneSession.role = "phone";
      phoneSession.displayName = phoneName;
    }

    // Record pairing
    this.pairedPhones.set(phoneId, deviceId);
    if (phoneSession) {
      phoneSession.pairedDesktop = deviceId;
    }

    // Forward to phone with relay-issued token + scoped_to
    const confirmed = JSON.parse(raw) as Record<string, unknown>;
    const confirmedPayload = confirmed["payload"] as Record<string, unknown>;
    confirmedPayload["token"] = relayToken;
    confirmedPayload["scoped_to"] = deviceId;
    const phoneConn = this.sessions.get(phoneId);
    if (phoneConn) {
      phoneConn.ws.send(JSON.stringify(confirmed));
    }
  }

  // -------------------------------------------------------------------------
  // pair.paired_devices — desktop queries its paired phones
  // -------------------------------------------------------------------------

  private handlePairPairedDevices(
    deviceId: string,
    ws: WebSocket,
    msgId: string | undefined,
  ): void {
    const phoneIds: string[] = [];
    for (const [pid, did] of this.pairedPhones) {
      if (did === deviceId) phoneIds.push(pid);
    }
    ws.send(
      envelope("pair.paired_devices", { phone_ids: phoneIds }, { desktopId: deviceId, inResponseTo: msgId }),
    );
  }

  // -------------------------------------------------------------------------
  // desktop.list — phone requests online desktops
  // -------------------------------------------------------------------------

  private handleDesktopList(ws: WebSocket, msgId: string | undefined): void {
    const devices = this.listOnline();
    ws.send(envelope("system.online_list", { devices }, { inResponseTo: msgId }));
  }

  // -------------------------------------------------------------------------
  // Generic routing — route msg to the desktop_id in the envelope
  // -------------------------------------------------------------------------

  private async handleRoute(
    deviceId: string,
    session: DeviceSession,
    ws: WebSocket,
    msg: Record<string, unknown>,
    msgId: string | undefined,
    raw: string,
  ): Promise<void> {
    const target = String(msg["desktop_id"] ?? "");
    if (!target) {
      ws.send(errorMsg("Missing desktop_id", msgId));
      return;
    }

    // Security: paired phones can only route to their own desktop
    if (session.role === "phone" && session.pairedDesktop !== target) {
      ws.send(errorMsg("Phone is not paired with target desktop", msgId));
      return;
    }

    const desktopSession = this.sessions.get(target);
    if (!desktopSession) {
      ws.send(errorMsg("Desktop not online", msgId));
      return;
    }

    // Stamp sender before forwarding
    const forwarded = JSON.parse(raw) as Record<string, unknown>;
    forwarded["sender_device_id"] = deviceId;
    desktopSession.ws.send(JSON.stringify(forwarded));
  }

  // -------------------------------------------------------------------------
  // Helpers
  // -------------------------------------------------------------------------

  private listOnline(): Array<{ device_id: string; display_name: string; device_type: string; last_seen: string }> {
    const result = [];
    for (const [id, s] of this.sessions) {
      result.push({
        device_id: id,
        display_name: s.displayName,
        device_type: s.deviceType,
        last_seen: s.lastSeen,
      });
    }
    return result;
  }

  private broadcastOnline(): void {
    const devices = this.listOnline();
    const msg = JSON.stringify({
      id: "evt_sys_online",
      type: "system.online_list",
      desktop_id: "",
      project_id: "",
      conversation_id: "",
      in_response_to: "",
      payload: { devices },
    });
    for (const s of this.sessions.values()) {
      try {
        s.ws.send(msg);
      } catch {
        // session closing — ignore
      }
    }
  }

  /** Consume a ticket (single-use, removes it). Returns null if missing/expired. */
  private consumeTicket(ticket: string): Ticket | null {
    const data = this.tickets.get(ticket);
    if (!data) return null;
    this.tickets.delete(ticket); // single-use: always remove
    if (Date.now() > data.expiresAt) return null;
    return data;
  }

  private purgeExpiredTickets(): void {
    const now = Date.now();
    for (const [k, v] of this.tickets) {
      if (now > v.expiresAt) this.tickets.delete(k);
    }
  }

  private purgeExpiredPairAttempts(): void {
    const now = Date.now();
    for (const [k, v] of this.pairAttempts) {
      if (now > v.expiresAt) this.pairAttempts.delete(k);
    }
  }
}
