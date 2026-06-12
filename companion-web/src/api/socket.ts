/// WebSocket client for Aura Relay.

type MessageHandler = (data: any) => void;

class CompanionSocket {
  private ws: WebSocket | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private listeners: Map<string, Set<MessageHandler>> = new Map();
  private relayUrl: string = '';
  private deviceToken: string = '';
  private _deviceId: string = '';
  private maxRetries = 5;
  private retryCount = 0;

  private static STORAGE_KEY_TOKEN = 'aura_companion_token';
  private static STORAGE_KEY_DEVICE_ID = 'aura_companion_device_id';
  private static STORAGE_KEY_RELAY = 'companion_relay_url';
  private static STORAGE_KEY_SAFE_CONTEXT = 'companion_safe_context';

  static getStoredToken(): string {
    return localStorage.getItem(CompanionSocket.STORAGE_KEY_TOKEN) || '';
  }

  static setStoredToken(token: string): void {
    if (token) {
      localStorage.setItem(CompanionSocket.STORAGE_KEY_TOKEN, token);
    } else {
      localStorage.removeItem(CompanionSocket.STORAGE_KEY_TOKEN);
    }
  }

  static getStoredDeviceId(): string {
    let id = localStorage.getItem(CompanionSocket.STORAGE_KEY_DEVICE_ID);
    if (!id) {
      id = 'phone_' + Math.random().toString(36).slice(2, 14);
      localStorage.setItem(CompanionSocket.STORAGE_KEY_DEVICE_ID, id);
    }
    return id;
  }

  static getStoredSafeContext(): any {
    try {
      return JSON.parse(localStorage.getItem(CompanionSocket.STORAGE_KEY_SAFE_CONTEXT) || '{}');
    } catch { return {}; }
  }

  static setStoredSafeContext(ctx: any): void {
    localStorage.setItem(CompanionSocket.STORAGE_KEY_SAFE_CONTEXT, JSON.stringify(ctx));
  }

  static getProjectId(): string {
    return CompanionSocket.getStoredSafeContext().project_id || '';
  }

  static getConversationId(): string {
    return CompanionSocket.getStoredSafeContext().conversation_id || '';
  }

  static isPaired(): boolean {
    return !!CompanionSocket.getStoredToken();
  }

  static clearStoredState(): void {
    CompanionSocket.setStoredToken('');
    CompanionSocket.setStoredSafeContext({});
    try { localStorage.removeItem(CompanionSocket.STORAGE_KEY_RELAY); } catch {}
  }

  connect(relayUrl: string, deviceToken?: string, deviceId?: string): void {
    const token = deviceToken || CompanionSocket.getStoredToken();
    const id = deviceId || CompanionSocket.getStoredDeviceId();
    // Skip if already connected to same URL and device
    if (this.connected && this.relayUrl === relayUrl && this._deviceId === id) {
      return;
    }
    this.relayUrl = relayUrl;
    this.deviceToken = token;
    this._deviceId = id;
    try { localStorage.setItem(CompanionSocket.STORAGE_KEY_RELAY, relayUrl); } catch {}
    this.maxRetries = Infinity;  // infinite retry intended — _scheduleReconnect never blocks on limit
    this.retryCount = 0;
    this._open();
  }

  get deviceId(): string {
    return this._deviceId;
  }

  get connecting(): boolean {
    return this.ws?.readyState === WebSocket.CONNECTING;
  }

  private _open(): void {
    this._cleanup();
    let base = this.relayUrl.startsWith('ws') ? this.relayUrl : `ws://${this.relayUrl}`;
    base = base.replace(/\/+$/, '');  // strip trailing slashes
    const url = base.endsWith('/ws') ? base : `${base}/ws`;
    this.ws = new WebSocket(url);
    
    this.ws.onopen = () => {
      this.retryCount = 0;
      // Send handshake
      this.sendRaw({
        type: 'hello',
        device_id: this._deviceId,
        device_type: 'phone',
        display_name: 'Aura Companion',
        token: this.deviceToken || undefined,
      });
    };

    this.ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        const msgType = msg.type || 'unknown';
        const handlers = this.listeners.get(msgType);
        if (handlers) {
          handlers.forEach((h) => h(msg));
        }
        // Also emit to wildcard listeners
        const wildcard = this.listeners.get('*');
        if (wildcard) {
          wildcard.forEach((h) => h(msg));
        }
      } catch (e) {
        console.warn('[CompanionSocket] Failed to parse message:', e);
      }
    };

    this.ws.onclose = () => {
      this._scheduleReconnect();
    };

    this.ws.onerror = () => {
      // onclose will fire after onerror
    };
  }

  private _scheduleReconnect(): void {
    if (this.maxRetries >= 0 && this.retryCount >= this.maxRetries) {
      console.warn('[CompanionSocket] Max retries reached');
      return;
    }
    this.retryCount++;
    const delay = Math.min(1000 * Math.pow(2, this.retryCount), 30000);
    console.log(`[CompanionSocket] Reconnecting in ${delay}ms (attempt ${this.retryCount})`);
    this.reconnectTimer = setTimeout(() => this._open(), delay);
  }

  private _cleanup(): void {
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    if (this.ws) {
      this.ws.onopen = null;
      this.ws.onmessage = null;
      this.ws.onclose = null;
      this.ws.onerror = null;
      this.ws.close();
      this.ws = null;
    }
  }

  send(type: string, payload: any = {}, desktopId: string = '', projectId: string = '', conversationId: string = ''): void {
    this.sendRaw({
      id: `cmd_${Math.random().toString(36).slice(2, 14)}`,
      type,
      desktop_id: desktopId,
      project_id: projectId,
      conversation_id: conversationId,
      in_response_to: '',
      payload,
    });
  }

  private sendRaw(msg: object): void {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(msg));
    } else {
      console.warn('[CompanionSocket] Cannot send — not connected');
    }
  }

  on(type: string, handler: MessageHandler): () => void {
    if (!this.listeners.has(type)) {
      this.listeners.set(type, new Set());
    }
    this.listeners.get(type)!.add(handler);
    return () => {
      this.listeners.get(type)?.delete(handler);
    };
  }

  pair(pairingCode: string, _relayUrl: string, desktopId: string, deviceName?: string): Promise<string> {
    return new Promise((resolve, reject) => {
      const unsubConfirmed = this.on('pair.confirmed', (data: any) => {
        const token = data.payload?.token;
        if (token) {
          CompanionSocket.setStoredToken(token);
          this.deviceToken = token;

          const safeCtx: any = {};
          if (data.payload?.desktop_name) safeCtx.desktop_name = data.payload.desktop_name;
          if (data.payload?.project_id) safeCtx.project_id = data.payload.project_id;
          if (data.payload?.project_name) safeCtx.project_name = data.payload.project_name;
          if (data.payload?.conversation_id) safeCtx.conversation_id = data.payload.conversation_id;
          if (data.payload?.phone_id) safeCtx.phone_id = data.payload.phone_id;
          const desktopId = data.payload?.scoped_to || data.payload?.desktop_id || '';
          if (desktopId) {
            safeCtx.desktop_id = desktopId;
            try { sessionStorage.setItem('companion_desktop_id', desktopId); } catch {}
          }
          if (Object.keys(safeCtx).length > 0) {
            CompanionSocket.setStoredSafeContext(safeCtx);
          }

          unsubConfirmed();
          unsubError();
          resolve(token);
        } else {
          reject(new Error('No token in pair.confirmed'));
        }
      });

      const unsubError = this.on('pair.error', (data: any) => {
        unsubConfirmed();
        unsubError();
        reject(new Error(data.payload?.message || 'Pairing failed'));
      });

      // User must be connected before calling pair().
      this.send('pair.connect', {
        code: pairingCode,
        device_name: deviceName || 'Aura Companion',
      }, desktopId);

      setTimeout(() => {
        unsubConfirmed();
        unsubError();
        reject(new Error('Pairing timed out'));
      }, 30000);
    });
  }

  logout(): void {
    CompanionSocket.setStoredToken('');
    this.disconnect();
  }

  disconnect(): void {
    this.maxRetries = 0; // prevent reconnect
    this._cleanup();
  }

  get connected(): boolean {
    return this.ws?.readyState === WebSocket.OPEN;
  }
}

// Singleton instance
export const socket = new CompanionSocket();
export default CompanionSocket;
