# Aura Companion — Detailed Implementation Spec

> **Derived from:** `COMPANION.md` (the project brief)  
> **Status:** Draft — sanity-checked against `C:\Projects\Aura-Harness2` codebase (commit `mob-test` branch)  
> **Last updated:** 2025-06-08

---

## Table of Contents

1. [Sanity Check Summary](#1-sanity-check-summary)
2. [Scope & Principles](#2-scope--principles)
3. [Architecture Overview](#3-architecture-overview)
4. [Phase 0 — Foundation & Scaffolding](#4-phase-0--foundation--scaffolding)
5. [Phase 1 — Local Developer Proof](#5-phase-1--local-developer-proof)
6. [Phase 2 — Real Desktop Chat Bridge](#6-phase-2--real-desktop-chat-bridge)
7. [Phase 3 — Project / Session / Runs / Receipts](#7-phase-3--project--session--runs--receipts)
8. [Phase 4 — Pairing, Auth & Production Polish](#8-phase-4--pairing-auth--production-polish)
9. [Desktop Integration Reference](#9-desktop-integration-reference)
10. [Relay Service Specification](#10-relay-service-specification)
11. [Mobile Web App Specification](#11-mobile-web-app-specification)
12. [Protocol Reference](#12-protocol-reference)
13. [Testing Strategy](#13-testing-strategy)
14. [Appendix: Existing Desktop APIs](#14-appendix-existing-desktop-apis)

---

## 1. Sanity Check Summary

### What `COMPANION.md` gets right

| Aspect | Verdict |
|---|---|
| **Phase ordering (1→2→3→4)** | ✅ Logical — each builds on the previous, each produces a testable increment |
| **Phase 1 as walking skeleton** | ✅ Proves relay+WS+protocol work end-to-end before touching real desktop internals |
| **Phase 2 routing into existing `ConversationManager.send`** | ✅ Correct — avoids reimplementing the conversation engine |
| **Phase 3 leveraging `ProjectStore`** | ✅ The `ProjectStore` already exists and provides `list_projects()`, `list_threads()`, etc. |
| **Protocol envelope design** | ✅ Clean JSON with `id`, `type`, `payload` — mirrors desktop's event patterns |
| **Out-of-scope list** | ✅ Well-reasoned; keeps MVP focussed |
| **Security rules** | ✅ Desktop validates all commands, Relay is passthrough only, no keys to phone |
| **Desktop does not block on Relay** | ✅ Non-negotiable for a local-first tool |

### Gaps & risks found during codebase audit

| # | Gap / Risk | Severity | Mitigation |
|---|---|---|---|
| 1 | **No Phase 0 defined** — `aura/companion/__init__.py` already says "Phase 0 foundation" but the doc doesn't define it. Stubs, relay dir, web scaffold, settings fields should all land in one foundation PR. | **High** | Added Phase 0 below. All subsequent phases reference files created in Phase 0. |
| 2 | **Desktop Companion Manager lifecycle is under-specified** — When does it start? How does it connect to MainWindow? How does connection status flow to the GUI? | **High** | Detailed in Phase 1 below: `CompanionManager` QObject owned by MainWindow, started in `__init__`, signals for status. |
| 3 | **No Companion settings page in the GUI** — COMPANION.md mentions this in a bullet but phases don't schedule it. | **Medium** | Phase 0 creates the settings page + new `AppSettings` fields. Phase 4 polishes it with device management. |
| 4 | **No project data projection for Companion** — `ProjectStore.list_projects()` returns internal `ProjectSpace` objects with file paths. The phone must not receive raw paths. A "safe" DTO is needed. | **Medium** | Phase 0/3 define `CompanionProject` / `CompanionThread` DTOs that strip internal paths. |
| 5 | **Active runs API does not exist yet as a unified query** — Worker runs live in `ConversationBridge.dispatch_records`; Drone runs live in `RunHistoryStore`. No single `list_active_runs()` endpoint exists. | **Medium** | Phase 3 creates a `get_active_run_summaries()` helper that merges both sources. |
| 6 | **Receipt API is Drone-only** — `DroneReceipt` / `RunHistoryStore` exist. Worker receipts don't have the same structured format. | **Low** | Phase 3 maps Worker dispatch records to a lightweight receipt DTO. Full Worker receipt persistence can be a future improvement. |
| 7 | **Cancel path assumption** — COMPANION.md says "if there is already a desktop-side cancel path". `ConversationBridge.request_cancel()` and `DroneRunner.cancel_run()` both exist. ✅ Confirmed. | **None** | Already works. Phase 3 wires these. |
| 8 | **Relay has no persistence model** — Session tokens, device registrations, online status need storage even in MVP. | **Medium** | Phase 1 uses in-memory dict (acceptable for dev proof). Phase 4 adds SQLite. |
| 9 | **No reconnection strategy for mobile web client** — Desktop side mentions reconnection with backoff; mobile web does not. | **Low** | Phase 1 includes basic reconnection for both sides. |
| 10 | **Command/response correlation** — Protocol has `id` on commands but no explicit response correlation pattern. Desktop events reference `desktop_id` but not `command_id`. | **Low** | Clarified in protocol spec below: desktop includes `in_response_to` on events that are direct replies. |
| 11 | **No e2e test strategy** — Phases don't define how to validate end-to-end. | **Medium** | Each phase now includes validation criteria and test commands. |

---

## 2. Scope & Principles

(Reproduced from COMPANION.md with minor clarifications.)

**Core rule:** Aura Desktop is the authority. It owns workspace, files, git, settings, providers, Drones, Worker runs, and approvals. The phone sends high-level Aura commands only.

**Aura Relay** is a lightweight backend that authenticates devices, tracks online desktop clients, and routes messages. It never touches project files or executes model calls.

**Aura Companion** is a mobile-first web app that connects to Relay. It shows online desktops, recent projects, chat sessions, run status, Drones, and receipts. It is a phone cockpit, not a squeezed desktop UI.

### Hard constraints

- No file browser, code editor, raw terminal, or remote desktop on the phone.
- No API keys or raw file contents sent to phone or Relay.
- Phone commands are high-level only; desktop validates every command.
- Desktop starts and works normally whether or not Relay is reachable.
- Write/diff approval is desktop-first for MVP (phone can see status but can't approve).

### Technology choices

| Layer | Technology | Rationale |
|---|---|---|
| **Desktop Companion module** | Python + asyncio (`websockets` or `aiohttp`) | Existing Python stack. asyncio for concurrent WS without blocking Qt main thread. |
| **Relay backend** | Python + FastAPI + `websockets` + SQLite (Phase 1: in-memory) | FastAPI has first-class WebSocket support. SQLite is zero-deploy. |
| **Mobile web app** | React + TypeScript + Vite | Existing ecosystem. Lightweight PWA-capable. |
| **WebSocket protocol** | JSON over WSS | Simple, debuggable, matches desktop event patterns. |
| **Auth (Phase 1-3)** | Dev token / hardcoded pairing code | Deliberately simple until Phase 4. |

---

## 3. Architecture Overview

```
┌──────────────────────────────┐      ┌──────────────────────┐      ┌──────────────────┐
│  Aura Desktop (Python/Qt)   │◄────►│   Aura Relay         │◄────►│  Aura Companion  │
│                              │  WS  │   (FastAPI)          │  WS  │  (React PWA)     │
│  ┌────────────────────────┐  │      │                      │      │                  │
│  │ CompanionManager       │──┘      │  ┌────────────────┐  │      │  ┌──────────────┐ │
│  │  - connects to Relay   │         │  │ SessionManager │  │      │  │ api/socket.ts│ │
│  │  - routes commands to  │         │  │ Auth           │  │      │  │ screens/*    │ │
│  │    ConversationBridge  │         │  │ Route messages │  │      │  │ components/* │ │
│  │  - forwards events to  │         │  └────────────────┘  │      │  └──────────────┘ │
│  │    Relay               │         │                      │      │                  │
│  └────────────────────────┘         └──────────────────────┘      └──────────────────┘
│  ┌────────────────────────┐
│  │ ConversationBridge     │── send() ──► Planner/Worker/Drones
│  │ ProjectStore           │── Recent projects, threads
│  │ RunHistoryStore        │── Drone receipts
│  │ dispatch_records       │── Active Worker runs
│  └────────────────────────┘
└──────────────────────────────┘
```

### Data flow (chat message)

```
Phone ──{chat.send}──► Relay ──{chat.send}──► Desktop CompanionManager
                                                    │
                                                    ▼
                                            ConversationBridge.send()
                                                    │
                                          Desktop Planner runs...
                                                    │
                                            CompanionManager ◄── events
                                                    │
Phone ◄──{chat.message.delta}── Relay ◄──{chat.message.delta}
Phone ◄──{chat.message.complete}── Relay ◄──{chat.message.complete}
```

---

## 4. Phase 0 — Foundation & Scaffolding

**Goal:** Create all structural scaffolding so Phases 1-4 can hit the ground running with no "create the file" overhead.

### Files to create

```
aura/companion/
  __init__.py          # clean import facade
  settings.py          # companion-specific settings model (used by Phase 4 settings UI)
  manager.py           # CompanionManager QObject skeleton
  protocol.py          # message serialization/deserialization, envelope types
  client.py            # WebSocket client (wraps asyncio or websockets library)
  auth.py              # token generation, storage, device identity (stub in Phase 0)

relay/
  __init__.py
  main.py              # FastAPI app stub
  auth.py              # token/session stubs
  models.py            # Pydantic models
  websocket.py         # WS endpoint stubs
  sessions.py          # session tracking stubs
  protocol.py          # shared protocol definitions (may re-export from desktop side)

companion-web/
  package.json
  tsconfig.json
  vite.config.ts
  index.html
  src/
    App.tsx
    main.tsx
    api/socket.ts       # WebSocket client stub
    screens/
      Login.tsx          # stub
      Desktops.tsx       # stub
      Projects.tsx       # stub
      Chat.tsx           # stub
      Runs.tsx           # stub
      Receipts.tsx       # stub
    components/
      StatusCard.tsx     # stub
      ChatMessage.tsx    # stub
      RunCard.tsx        # stub
```

### Desktop settings changes

Add to `AppSettings` in `aura/settings.py`:

```python
# Companion settings
companion_enabled: bool = False
companion_relay_url: str = "ws://localhost:8765"   # default dev relay
companion_display_name: str = ""                     # auto-generated from hostname
companion_device_token: str = ""                     # set during pairing (Phase 4)
```

Add `companion_enabled` and `companion_relay_url` to `AppSettings.from_dict()` deserialization.

### New settings page

Create `aura/gui/settings_pages/companion_page.py` following the `VisionPage` pattern:

- `GlassSwitch` for Enable/Disable
- Editable `QComboBox` or `QLineEdit` for Relay URL (hidden behind an "Advanced" expander in Phase 1; visible inline by Phase 4)
- Connection status indicator (colored dot: gray=disabled, yellow=connecting, green=connected, red=error)
- Desktop display name (auto-filled from hostname, editable)
- Phase 4 will add: paired devices list, revoke button

Register it in `SettingsDialog.__init__`:

```python
from aura.gui.settings_pages.companion_page import CompanionPage
self._companion_page = CompanionPage(self._settings)
# ...
(self._companion_page, "Companion"),
```

### AppSettings DTOs for Companion

Define safe DTOs that strip internal paths before sending to Relay → phone:

```python
# aura/companion/protocol.py

@dataclass
class CompanionProject:
    id: str
    name: str                    # last path component or user-set name
    updated_at: str              # ISO 8601
    thread_count: int

@dataclass
class CompanionThread:
    id: str
    title: str
    updated_at: str
    is_current: bool

@dataclass
class ActiveRunSummary:
    run_id: str
    kind: Literal["worker", "drone"]
    label: str                   # Worker: tool_call_id or goal; Drone: drone_name
    status: str                  # "running", "waiting_approval", "completed", "failed", "cancelled"
    started_at: str | None

@dataclass
class ReceiptSummary:
    run_id: str
    kind: Literal["drone", "worker"]
    label: str
    status: str
    completed_at: str
    summary: str                 # brief one-liner
```

### Validation (Phase 0)

```bash
python -m py_compile aura/companion/*.py
python -m py_compile aura/gui/settings_pages/companion_page.py
cd companion-web && npm install && npm run build -- --emptyOutDir  # builds empty shell
```

---

## 5. Phase 1 — Local Developer Proof

**Goal:** Hardcoded/dev auth. Run Relay locally. Desktop connects. Simple mobile web page connects. Phone sends `chat.send`; desktop receives and logs it; desktop sends fake response; phone displays it.

### Desktop: CompanionManager

`aura/companion/manager.py` — `CompanionManager(QObject)`:

```python
class CompanionManager(QObject):
    connection_status_changed = Signal(str)  # "disabled", "connecting", "connected", "error"
    message_received = Signal(dict)          # raw received JSON

    def __init__(self, settings: AppSettings) -> None:
        super().__init__()
        self._settings = settings
        self._ws: WebSocketClient | None = None
        self._reconnect_timer: QTimer | None = None

    def start(self) -> None:
        """Called on MainWindow init if companion_enabled."""
        if not self._settings.companion_enabled:
            self.connection_status_changed.emit("disabled")
            return
        self._connect()

    def stop(self) -> None:
        """Called on app shutdown or when toggling off."""
        self._stop_reconnect_timer()
        if self._ws:
            self._ws.close()
            self._ws = None
        self.connection_status_changed.emit("disabled")

    def send_event(self, event: dict) -> None:
        """Send an event to Relay for forwarding to phone."""
        if self._ws and self._ws.connected:
            self._ws.send(json.dumps(event))

    def _connect(self) -> None:
        """Initiate WS connection (runs in a QThread or uses asyncio)."""
        ...

    def _on_message(self, raw: str) -> None:
        """Handle an incoming message from Relay."""
        msg = json.loads(raw)
        self.message_received.emit(msg)
        # Phase 1: just log it
        # Phase 2: route to ConversationBridge
```

**Lifecycle integration in MainWindow** (`aura/gui/main_window.py`):

```python
# In MainWindow.__init__:
from aura.companion.manager import CompanionManager
self._companion = CompanionManager(load_settings())
self._companion.connection_status_changed.connect(self._on_companion_status)
self._companion.message_received.connect(self._on_companion_message)
self._companion.start()

# In MainWindow.closeEvent:
self._companion.stop()

# In MainWindow._on_open_settings → after settings saved:
self._companion.stop()
self._companion = CompanionManager(new_settings)
self._companion.start()
```

### Desktop: WebSocket client

`aura/companion/client.py` — Wraps `websockets` or `aiohttp` in a `QThread`:

```python
class CompanionWsClient(QObject):
    connected = Signal()
    disconnected = Signal()
    message_received = Signal(str)

    def __init__(self, url: str, device_token: str) -> None:
        super().__init__()
        self._url = url
        self._token = device_token
        self._ws = None

    def connect(self) -> None:
        """Run WS connect loop in thread."""
        ...

    def send(self, data: str) -> None:
        ...

    def close(self) -> None:
        ...
```

> **Note:** Because Qt's main loop is involved, we use `QThread` + `QObject.moveToThread` or `asyncio` with `qasync`. The simplest approach for MVP: a `QThread` that runs a blocking `websockets` client loop, emitting signals for received messages.

### Desktop: Protocol helpers

`aura/companion/protocol.py` — Envelope creation/parsing:

```python
def make_envelope(msg_type: str, payload: dict, *, desktop_id: str | None = None,
                  project_id: str | None = None, conversation_id: str | None = None,
                  in_response_to: str | None = None) -> dict:
    return {
        "id": f"evt_{uuid4().hex[:12]}",
        "type": msg_type,
        "desktop_id": desktop_id or "",
        "project_id": project_id or "",
        "conversation_id": conversation_id or "",
        "in_response_to": in_response_to,
        "payload": payload,
    }

def parse_command(raw: dict) -> tuple[str, dict]:
    """Validate envelope and return (type, payload)."""
    ...
```

### Desktop: Online event on connect

When `CompanionManager` connects, it sends:

```json
{
  "id": "evt_xxx",
  "type": "desktop.online",
  "desktop_id": "hostname_or_uuid",
  "project_id": "",
  "conversation_id": "",
  "payload": {
    "display_name": "Aura Desktop",
    "aura_version": "1.6.0",
    "capabilities": ["chat.send", "project.list_recent", "conversation.*"]
  }
}
```

### Relay: FastAPI app

`relay/main.py`:

```python
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from relay.sessions import SessionManager
from relay.protocol import validate_envelope

app = FastAPI(title="Aura Relay")
sessions = SessionManager()  # in-memory for Phase 1

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    # Phase 1: accept any connection (dev auth)
    # Phase 4: validate token
    device_id = await _handshake(ws)
    sessions.register(device_id, ws)
    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            # Route to target desktop
            target = msg.get("desktop_id")
            if target and sessions.is_online(target):
                await sessions.send_to(target, raw)
            else:
                await ws.send_text(json.dumps({
                    "type": "error",
                    "payload": {"message": "Desktop not online"}
                }))
    except WebSocketDisconnect:
        sessions.unregister(device_id)
```

`relay/sessions.py` — SessionManager:

```python
class SessionManager:
    def __init__(self):
        self._sessions: dict[str, WebSocket] = {}  # device_id -> ws

    def register(self, device_id: str, ws: WebSocket) -> None:
        self._sessions[device_id] = ws

    def unregister(self, device_id: str) -> None:
        self._sessions.pop(device_id, None)

    def is_online(self, device_id: str) -> bool:
        return device_id in self._sessions

    def send_to(self, device_id: str, data: str) -> bool:
        ws = self._sessions.get(device_id)
        if ws:
            asyncio.create_task(ws.send_text(data))
            return True
        return False

    def list_online(self) -> list[dict]:
        return [
            {"desktop_id": did, "display_name": "Aura Desktop"}
            for did in self._sessions
        ]
```

### Relay: Protocol

`relay/protocol.py`:

```python
def validate_envelope(msg: dict) -> bool:
    """Check required fields exist."""
    return all(k in msg for k in ("id", "type", "desktop_id", "payload"))
```

### Mobile web: socket.ts

```typescript
// companion-web/src/api/socket.ts
class CompanionSocket {
  private ws: WebSocket | null = null;
  private reconnectTimer: number | null = null;
  private listeners: Map<string, Set<(data: any) => void>> = new Map();

  connect(relayUrl: string, deviceToken: string) { ... }
  send(type: string, payload: any, desktopId?: string) { ... }
  on(type: string, handler: (data: any) => void) { ... }
  disconnect() { ... }
}
```

### Mobile web: Chat screen (Phase 1 minimal)

`Chat.tsx`:

```tsx
function ChatScreen() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");

  const sendMessage = () => {
    socket.send("chat.send", { text: input }, desktopId);
    setInput("");
  };

  // Listen for chat.message.delta and chat.message.complete
  useEffect(() => {
    socket.on("chat.message.delta", (evt) => { ... });
    socket.on("chat.message.complete", (evt) => { ... });
  }, []);

  return (
    <div className="chat">
      <MessageList messages={messages} />
      <InputBar value={input} onChange={setInput} onSend={sendMessage} />
    </div>
  );
}
```

### Desktop: Phase 1 fake response

In `CompanionManager._on_message`, for Phase 1:

```python
def _on_message(self, raw: str) -> None:
    msg = json.loads(raw)
    if msg.get("type") == "chat.send":
        text = msg.get("payload", {}).get("text", "")
        logger.info("[Companion] Received chat: %s", text)
        # Fake response
        self.send_event({
            "type": "chat.message.delta",
            "in_response_to": msg["id"],
            "payload": {"role": "assistant", "text": f"You said: {text}"}
        })
        self.send_event({
            "type": "chat.message.complete",
            "in_response_to": msg["id"],
            "payload": {"role": "assistant", "text": f"You said: {text}"}
        })
```

### Validation (Phase 1)

```bash
# Terminal 1: start Relay
cd relay && uvicorn main:app --port 8765 --reload

# Terminal 2: start Desktop with --companion flag or enable in settings
cd .. && python -m aura --companion

# Terminal 3: open companion-web dev server
cd companion-web && npm run dev

# Manual: open http://localhost:5173 in browser
# 1. See "Aura Desktop" listed as online
# 2. Select desktop → see "Recent projects" (empty, no error)
# 3. Start chat → type message → see fake response echo back
```

---

## 6. Phase 2 — Real Desktop Chat Bridge

**Goal:** Route `chat.send` from phone into Aura's real `ConversationBridge.send()` path. Stream `ContentDelta` / `Done` events back to the phone as `chat.message.delta` / `chat.message.complete`.

### Desktop: Route incoming chat.send to ConversationBridge

`CompanionManager` gains a reference to `ConversationBridge`:

```python
class CompanionManager(QObject):
    def set_bridge(self, bridge: ConversationBridge) -> None:
        self._bridge = bridge

    def _on_message(self, raw: str) -> None:
        msg = json.loads(raw)
        msg_type = msg.get("type")
        payload = msg.get("payload", {})

        if msg_type == "chat.send":
            self._handle_chat_send(msg)
        elif msg_type == "conversation.create":
            self._handle_conversation_create(msg)
        elif msg_type == "conversation.select":
            self._handle_conversation_select(msg)
        # ... etc as phases progress

    def _handle_chat_send(self, msg: dict) -> None:
        """Route a phone message into the conversation send path."""
        if not self._bridge:
            return
        # Map conversation_id: "current" → resume current, "new" → new conversation
        conv_id = msg.get("conversation_id", "current")
        if conv_id == "new":
            self._bridge.reset_history()  # start fresh
        # Hook into bridge events to forward to phone
        self._hook_bridge_events(msg["id"])
        # Send the message
        self._bridge.send(self._model, self._thinking)

    def _hook_bridge_events(self, command_id: str) -> None:
        """Temporarily wire bridge signals → Companion events."""
        # Connect to bridge._on_content_delta etc. and emit via send_event
        ...
```

**Key design choice:** Instead of modifying `ConversationBridge` to know about Companion, we attach to its existing Qt signals (`content_delta`, `done`, etc.) from `CompanionManager`. This keeps Companion as a non-invasive add-on.

Existing bridge signals (from `aura/bridge/qt_bridge.py`):

```
content_delta = Signal(str)           # text fragment
reasoning_delta = Signal(str)         # reasoning fragment
tool_call_start = Signal(int, str, str)
tool_call_args = Signal(int, str)
tool_call_end = Signal(int)
tool_result = Signal(str, str, bool, str, dict)
started = Signal()
finished = Signal(str, dict)          # finish_reason, full_message
done = Signal(str, str)               # finish_reason, full_message
api_error = Signal(int, str)
```

### Stream mapping

| Desktop signal | Companion event type | Payload |
|---|---|---|
| `started` | `planner.status` | `{"status": "running"}` |
| `content_delta(text)` | `chat.message.delta` | `{"role": "assistant", "text": text}` |
| `reasoning_delta(text)` | `planner.status` | `{"status": "reasoning", "text": text}` (optional, gated by settings) |
| `tool_call_start/args/end` | `planner.status` | `{"status": "using_tool", "tool": name}` |
| `worker.status` (via dispatch) | `worker.status` | `{"status": "running", "goal": goal}` |
| `finished(reason, msg)` | `chat.message.complete` | `{"role": "assistant", "text": full_text, "finish_reason": reason}` |
| `done(reason, msg)` | `chat.message.complete` | Same as above |
| `api_error(status, msg)` | `error` | `{"message": msg}` |

### Conversation create/select

```python
def _handle_conversation_create(self, msg: dict) -> None:
    # Create new thread in ProjectStore
    project = self._project_store.load_project(msg.get("project_id"))
    thread = self._project_store.create_thread(project, "Companion chat")
    self._bridge.reset_history()
    self._current_thread = thread
    self.send_event(make_envelope("conversation.created", {
        "conversation_id": thread.id,
        "title": thread.title,
    }))

def _handle_conversation_select(self, msg: dict) -> None:
    conv_id = msg.get("conversation_id", "current")
    if conv_id == "current" and self._project_store:
        # Load most recent thread
        project = self._project_store.load_project(msg.get("project_id"))
        threads = self._project_store.list_threads(project)
        if threads:
            thread = threads[0]
            # Load its conversation history into the bridge
            ...
```

### Validation (Phase 2)

```bash
# Terminal 1: Relay
# Terminal 2: Desktop with Companion enabled, a project open
# Terminal 3: Mobile web
# Test:
# 1. Send chat message from phone → desktop runs Planner → response streams back
# 2. Verify chat.message.delta events arrive progressively
# 3. Verify chat.message.complete contains full text
# 4. Test "new conversation" creates a new thread
# 5. Test cancel from phone (desktop.request_cancel works)
```

---

## 7. Phase 3 — Project / Session / Runs / Receipts

**Goal:** Expose recent projects from desktop, allow project selection, show active run summaries, support cancel, show recent receipts.

### Project list from desktop

`CompanionManager._handle_project_list_recent()`:

```python
def _handle_project_list_recent(self, msg: dict) -> None:
    projects = self._project_store.list_projects()
    safe_projects = [
        CompanionProject(
            id=p.id,
            name=p.name or Path(p.root_path).name,
            updated_at=p.updated_at,
            thread_count=len(self._project_store.list_threads(p)),
        ).to_dict()
        for p in projects[-20:]  # last 20
    ]
    self.send_event(make_envelope("project.list_recent", {
        "projects": safe_projects,
    }, in_response_to=msg["id"]))
```

**Event when project is selected:**

```python
def _handle_project_select(self, msg: dict) -> None:
    project_id = msg.get("project_id", "")
    project = self._project_store.load_project(project_id)
    if not project:
        self._send_error("Project not found", in_response_to=msg["id"])
        return
    self._bridge.set_workspace_root(Path(project.root_path))
    self._current_project = project
    # Send threads
    threads = self._project_store.list_threads(project)
    safe_threads = [CompanionThread(...).to_dict() for t in threads]
    self.send_event(make_envelope("project.selected", {
        "project_id": project.id,
        "name": project.name,
        "threads": safe_threads,
    }, in_response_to=msg["id"]))
```

### Active run summaries

New helper that merges Worker and Drone active runs:

```python
# aura/companion/protocol.py (or a new utils.py)
def get_active_run_summaries(bridge: ConversationBridge | None,
                              drone_runner: DroneRunner | None) -> list[dict]:
    summaries: list[dict] = []
    # Worker runs from bridge dispatch_records
    if bridge:
        for record in bridge.dispatch_records():
            summaries.append({
                "run_id": record.tool_call_id,
                "kind": "worker",
                "label": record.goal or record.tool_call_id,
                "status": record.status or "running",
                "started_at": record.started_at,
            })
    # Drone runs
    if drone_runner:
        for run in drone_runner.active_runs():
            summaries.append({
                "run_id": run.run_id,
                "kind": "drone",
                "label": run.drone_name,
                "status": run.status,
                "started_at": run.started_at,
            })
    return summaries
```

### Run cancel

```python
def _handle_run_cancel(self, msg: dict) -> None:
    run_id = msg.get("payload", {}).get("run_id", "")
    # Try worker cancel
    if self._bridge and self._bridge.user_cancelled_dispatch(run_id):
        self._send_ok(in_response_to=msg["id"])
        return
    # Try drone cancel
    if self._drone_runner:
        self._drone_runner.cancel_run(run_id)
        self._send_ok(in_response_to=msg["id"])
        return
    self._send_error("Run not found or cannot be cancelled", in_response_to=msg["id"])
```

### Receipts

```python
def _handle_receipt_list_recent(self, msg: dict) -> None:
    receipts = []
    # Drone receipts
    store = RunHistoryStore(self._workspace_root)
    for r in store.list_recent(limit=20):
        receipts.append({
            "run_id": r.run_id,
            "kind": "drone",
            "label": r.drone_name,
            "status": r.status,
            "completed_at": r.completed_at,
            "summary": r.summary[:200],
        })
    # Worker "receipts" from recent dispatch results
    if self._bridge:
        for record in self._bridge.dispatch_records():
            if record.status == "completed":
                receipts.append({...})
    self.send_event(make_envelope("receipt.list_recent", {
        "receipts": receipts,
    }, in_response_to=msg["id"]))
```

### Status forwarding

Hook into `drone_runner` signals and `bridge` dispatch signals in `CompanionManager`:

```python
# In CompanionManager.set_drone_runner:
self._drone_runner = drone_runner
drone_runner.status_changed.connect(self._on_drone_status)

def _on_drone_status(self, run_id: str, drone_name: str, status: str) -> None:
    self.send_event(make_envelope("drone.status", {
        "run_id": run_id,
        "drone_name": drone_name,
        "status": status,
    }))
```

### Mobile screens added

- **Projects screen**: list of recent projects from `project.list_recent` response
- **Project home screen**: Continue current chat / Start new chat / Active runs / Drones / Receipts
- **Runs screen**: List of active run cards with status and cancel button
- **Receipts screen**: List of recent receipts, tap to view detail

### Validation (Phase 3)

```bash
# 1. Open Desktop with recent projects → phone shows project list
# 2. Select a project from phone → phone shows threads
# 3. Send chat → run appears in Runs screen with live status
# 4. Cancel a Worker or Drone from phone → desktop cancels
# 5. After run completes → receipt appears in Receipts screen
# 6. Verify phone never sees file paths or API keys
```

---

## 8. Phase 4 — Pairing, Auth & Production Polish

**Goal:** Secure production-ready pairing flow with device tokens, revocation, and TLS.

### Pairing flow

1. Desktop generates a **pairing code** (6 alphanumeric chars, time-limited to 10 min).
2. User enters pairing code in phone web app.
3. Phone sends `auth.pair` with pairing code → Relay validates → returns a device token.
4. Phone stores token in `localStorage`.
5. Phone reconnects with token on subsequent opens.
6. Desktop can see paired devices and revoke any.

### Desktop auth module

`aura/companion/auth.py`:

```python
def generate_pairing_code() -> str:
    """Generate a 6-char time-limited pairing code."""
    import secrets, string
    code = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))
    _pairing_codes[code] = {
        "created_at": time.time(),
        "desktop_id": get_device_id(),
    }
    return code

def validate_pairing_code(code: str) -> bool:
    entry = _pairing_codes.pop(code, None)
    if not entry:
        return False
    if time.time() - entry["created_at"] > 600:  # 10 min
        return False
    return True
```

### Relay auth

`relay/auth.py`:

```python
import jwt  # or similar

SECRET = os.environ.get("AURA_RELAY_SECRET", "dev-secret-change-in-prod")

def create_device_token(desktop_id: str, device_name: str) -> str:
    return jwt.encode({
        "desktop_id": desktop_id,
        "device_name": device_name,
        "iat": int(time.time()),
    }, SECRET, algorithm="HS256")

def verify_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, SECRET, algorithms=["HS256"])
    except Exception:
        return None
```

### Relay session persistence (SQLite)

```python
# relay/models.py
import sqlite3

def init_db():
    conn = sqlite3.connect("relay.db")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS devices (
            device_id TEXT PRIMARY KEY,
            display_name TEXT,
            token_hash TEXT,
            last_seen TEXT,
            revoked INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pairing_codes (
            code TEXT PRIMARY KEY,
            desktop_id TEXT,
            created_at TEXT,
            used INTEGER DEFAULT 0
        )
    """)
```

### Desktop settings page completion

The Companion settings page now shows:

- **Enable Companion** toggle
- **Connection status** indicator (green dot + "Connected" / red dot + "Disconnected")
- **Desktop name** (editable)
- **Relay URL** (Advanced expander)
- **Paired devices list** with revoke button
- **Pairing code** (generated on demand, shown as large text with copy button and countdown)

### Mobile web: Login screen with pairing

```tsx
function LoginScreen() {
  const [pairingCode, setPairingCode] = useState("");
  const [deviceName, setDeviceName] = useState("My Phone");

  const pairDevice = async () => {
    const result = await socket.sendAndWait("auth.pair", {
      pairing_code: pairingCode,
      device_name: deviceName,
    });
    if (result.token) {
      localStorage.setItem("device_token", result.token);
      navigate("/desktops");
    }
  };

  return (
    <div className="login">
      <h1>Aura Companion</h1>
      <p>Enter the pairing code shown in Aura Desktop settings.</p>
      <PinInput value={pairingCode} onChange={setPairingCode} length={6} />
      <Input label="Device name" value={deviceName} onChange={setDeviceName} />
      <Button onClick={pairDevice}>Pair</Button>
    </div>
  );
}
```

### Production deployment

- Relay behind TLS (Caddy / nginx or FastAPI with SSLContext)
- Relay authentication via JWT or signed tokens
- Rate limiting on pairing endpoint
- `AURA_RELAY_SECRET` as environment variable
- SQLite backed up or migrated to Postgres if needed

### Validation (Phase 4)

```bash
# 1. Desktop Companion settings: generate pairing code
# 2. Phone web: enter code, receive token
# 3. Kill phone → reopen → auto-connects with stored token (no re-pair)
# 4. Desktop: revoke phone → phone gets kicked on next reconnect
# 5. Verify expired pairing codes are rejected
# 6. Verify invalid tokens are rejected
```

---

## 9. Desktop Integration Reference

### Where CompanionManager hooks into MainWindow

| Lifecycle point | Action |
|---|---|
| `MainWindow.__init__()` | Create `CompanionManager`, connect signals, call `.start()` |
| `MainWindow.closeEvent()` | Call `.stop()` |
| After settings saved | Recreate `CompanionManager` with new settings |
| When bridge is created | `companion.set_bridge(bridge)` |
| When drone runner is created | `companion.set_drone_runner(runner)` |
| When workspace root changes | `companion.set_workspace_root(path)` |

### CompanionManager signals

| Signal | Description | Connected to |
|---|---|---|
| `connection_status_changed(str)` | "disabled", "connecting", "connected", "error" | Update status bar indicator & settings page dot |
| `message_received(dict)` | Raw incoming JSON command from phone | Route handler dispatch |

### CompanionManager public methods

| Method | Description |
|---|---|
| `start()` | Begin WS connection if enabled |
| `stop()` | Close WS, cancel reconnect timer |
| `set_bridge(bridge)` | Wire up conversation bridge events |
| `set_drone_runner(runner)` | Wire up drone status events |
| `set_workspace_root(path)` | Update project store reference |
| `update_settings(settings)` | Apply new settings, reconnect if needed |
| `send_event(event_dict)` | Send event to phone via Relay |
| `generate_pairing_code()` | Return 6-char code (Phase 4) |

### AppSettings fields

| Field | Type | Default | Phase |
|---|---|---|---|
| `companion_enabled` | `bool` | `False` | 0 |
| `companion_relay_url` | `str` | `"ws://localhost:8765"` | 0 |
| `companion_display_name` | `str` | `""` (auto from hostname) | 0 |
| `companion_device_token` | `str` | `""` | 4 |

---

## 10. Relay Service Specification

### Endpoints

| Path | Method | Phase | Description |
|---|---|---|---|
| `/ws` | WebSocket | 1 | Main WS endpoint for desktop and phone |
| `/health` | GET | 1 | Health check |
| `/api/pair` | POST | 4 | Exchange pairing code for device token |
| `/api/revoke` | POST | 4 | Revoke a paired device |

### Relay → Desktop → Phone routing

```
Desktop ──WS──► Relay ──WS──► Phone
Phone   ──WS──► Relay ──WS──► Desktop
```

Relay maintains a mapping of `device_id → WebSocket`. When it receives a message, it looks up `desktop_id` in the `payload` or envelope and forwards to that device's WS. If the target is offline, it returns an error to the sender.

### Relay in-memory state (Phase 1-3)

```python
{
  "devices": {
    "desktop_abc": {
      "ws": <WebSocket>,
      "display_name": "Aura Desktop",
      "paired_phones": ["phone_xyz"],
      "last_seen": "2025-06-08T12:00:00Z",
    },
    "phone_xyz": {
      "ws": <WebSocket>,
      "display_name": "My Phone",
      "paired_desktop": "desktop_abc",
      "last_seen": "2025-06-08T12:00:00Z",
    },
  },
  "pairing_codes": {
    "ABC123": {
      "desktop_id": "desktop_abc",
      "created_at": "2025-06-08T12:00:00Z",
    }
  }
}
```

### Scale notes

- In-memory is fine for single-user/small-scale (Phase 1-3).
- Phase 4 switches to SQLite for persistence across restarts.
- If multi-tenancy is ever needed, swap to Postgres + Redis pub/sub.

---

## 11. Mobile Web App Specification

### Tech stack

- **Framework:** React 18 + TypeScript
- **Build:** Vite 5
- **Routing:** React Router v6
- **Styling:** CSS modules or Tailwind (choose one, keep consistent)
- **State:** React context + hooks (no Redux for MVP)
- **PWA:** `vite-plugin-pwa` for install prompt + offline shell

### Screen map

```
/login          → LoginScreen (Phase 4) or auto-redirect to /desktops
/desktops       → DesktopListScreen  — shows online desktops
/projects       → ProjectListScreen  — shows recent projects
/projects/:id   → ProjectHomeScreen  — thread list, start chat, runs, receipts
/chat/:thread?  → ChatScreen         — message list, input, streaming
/runs           → RunListScreen      — active run cards with cancel
/receipts       → ReceiptListScreen  — recent receipts
/receipts/:id   → ReceiptDetailScreen — full receipt content
```

### Socket event listener setup

In `App.tsx` or a `SocketProvider`:

```typescript
// On connect: send desktop.list
// Receive desktop.online/desktop.offline → update desktop list
// Receive chat.message.delta → append to active message
// Receive chat.message.complete → finalize message
// Receive planner.status → update planner status banner
// Receive worker.status → update runs list
// Receive drone.status → update runs list
// Receive receipt.ready → refresh receipts
```

### PWA requirements

- Manifest with mobile-friendly icons
- Service worker for offline shell (just the login screen and cached assets)
- No offline chat support for MVP (requires connection)

---

## 12. Protocol Reference

### Envelope

```json
{
  "id": "cmd_abc123 | evt_xyz789",
  "type": "command.or.event.name",
  "desktop_id": "desktop_uuid",
  "project_id": "project_uuid (optional)",
  "conversation_id": "thread_uuid | 'current' | 'new' (optional)",
  "in_response_to": "cmd_abc123 (only on events that are direct replies)",
  "payload": { ... }
}
```

### Command types (phone → desktop)

| Type | Payload | Phase | Description |
|---|---|---|---|
| `desktop.list` | `{}` | 1 | List online desktops |
| `project.list_recent` | `{}` | 3 | List recent projects |
| `project.select` | `{"project_id": "..."}` | 3 | Select a project |
| `conversation.list_recent` | `{"project_id": "..."}` | 2 | List threads for project |
| `conversation.create` | `{"project_id": "...", "title": "..."}` | 2 | Create new thread |
| `conversation.select` | `{"project_id": "...", "conversation_id": "..."}` | 2 | Switch to existing thread |
| `chat.send` | `{"text": "..."}` | 1 | Send chat message to Planner |
| `run.list_active` | `{}` | 3 | List active Worker/Drone runs |
| `run.cancel` | `{"run_id": "..."}` | 3 | Cancel a run |
| `receipt.list_recent` | `{}` | 3 | List recent receipts |
| `receipt.get` | `{"run_id": "..."}` | 3 | Get full receipt |
| `auth.pair` | `{"pairing_code": "...", "device_name": "..."}` | 4 | Pair phone with desktop |

### Event types (desktop → phone)

| Type | Payload | Phase | Description |
|---|---|---|---|
| `desktop.online` | `{"display_name": "...", "aura_version": "...", "capabilities": [...]}` | 1 | Desktop came online |
| `desktop.offline` | `{}` | 1 | Desktop disconnected |
| `project.selected` | `{"project_id": "...", "name": "...", "threads": [...]}` | 3 | Project selection result |
| `conversation.created` | `{"conversation_id": "...", "title": "..."}` | 2 | New thread created |
| `conversation.selected` | `{"conversation_id": "...", "title": "..."}` | 2 | Thread switched |
| `chat.message.delta` | `{"role": "assistant", "text": "..."}` | 1 | Streaming text fragment |
| `chat.message.complete` | `{"role": "assistant", "text": "...", "finish_reason": "..."}` | 1 | Message finished |
| `planner.status` | `{"status": "running"|"reasoning"|"using_tool", "tool": "..."}` | 2 | Planner phase change |
| `spec.ready` | `{"spec_summary": "..."}` | 2 | Planner produced a spec |
| `worker.status` | `{"run_id": "...", "status": "..."}` | 3 | Worker status change |
| `drone.status` | `{"run_id": "...", "drone_name": "...", "status": "..."}` | 3 | Drone status change |
| `run.finished` | `{"run_id": "...", "kind": "worker"|"drone", "status": "..."}` | 3 | Run completed |
| `run.cancelled` | `{"run_id": "..."}` | 3 | Run was cancelled |
| `receipt.ready` | `{"run_id": "...", "kind": "worker"|"drone"}` | 3 | New receipt available |
| `error` | `{"message": "...", "code": "..."}` | 1 | Error response |

### Wire format

All messages are JSON-encoded strings over WebSocket. Binary frames are not used in MVP.

---

## 13. Testing Strategy

### Per-phase validation

| Phase | Automated checks | Manual smoke tests |
|---|---|---|
| **0** | `py_compile` all new files, `companion-web` build | N/A (stubs only) |
| **1** | Relay unit tests (pytest + FastAPI TestClient), WS echo test | End-to-end: desktop connects, phone sends message, fake response received |
| **2** | CompanionManager signal routing tests (pytest with Qt Test) | End-to-end: real chat message from phone → Planner runs → response streams |
| **3** | ProjectStore integration test via Companion DTOs | Select project, see threads, see active runs, cancel run |
| **4** | Auth token flow, pairing code expiry, device revoke | Full pairing flow, kill/reconnect, revoke |

### Test files

```
tests/
  test_companion_manager.py    # CompanionManager signal routing
  test_companion_protocol.py   # Envelope creation/parsing, DTO conversion
  test_companion_client.py     # WS client connect/send/receive (mocked)

relay/
  tests/
    test_websocket.py          # WS endpoint routing
    test_auth.py               # Token generation/validation (Phase 4)

companion-web/
  src/
    __tests__/
      socket.test.ts           # WebSocket client logic
      ChatScreen.test.tsx      # Message rendering
```

### Key test scenarios

1. **Desktop starts with Companion disabled** → no WS connection, no blocking.
2. **Desktop starts with Companion enabled, Relay down** → status "error", desktop works normally.
3. **Relay comes online after desktop** → desktop reconnects automatically.
4. **Phone sends message with invalid project_id** → desktop returns error event.
5. **Phone sends message while Desktop is running a Planner turn** → message buffered or errored (desktop decides).
6. **Pairing code expires** → rejected.
7. **Token revoked** → next phone WS message returns error, phone shows "re-pair required".

---

## 14. Appendix: Existing Desktop APIs

These are the existing desktop-side APIs that CompanionManager will call.

| API | Location | Signature | Used in Phase |
|---|---|---|---|
| `ConversationBridge.send()` | `aura/bridge/qt_bridge.py` | `send(model, thinking, max_tool_rounds=None)` | 2 |
| `ConversationBridge.request_cancel()` | `aura/bridge/qt_bridge.py` | `request_cancel()` | 3 |
| `ConversationBridge.reset_history()` | `aura/bridge/qt_bridge.py` | `reset_history()` | 2 |
| `ConversationBridge.dispatch_records()` | `aura/bridge/qt_bridge.py` | `dispatch_records() -> list[WorkerDispatchRecord]` | 3 |
| `ConversationBridge.user_cancelled_dispatch()` | `aura/bridge/qt_bridge.py` | `user_cancelled_dispatch(tool_call_id) -> bool` | 3 |
| `ProjectStore.list_projects()` | `aura/projects/store.py` | `list_projects(include_archived=False) -> list[ProjectSpace]` | 3 |
| `ProjectStore.list_threads()` | `aura/projects/store.py` | `list_threads(project, include_archived=False) -> list[ProjectThread]` | 3 |
| `ProjectStore.create_thread()` | `aura/projects/store.py` | `create_thread(project, title) -> ProjectThread` | 2 |
| `ProjectStore.load_project()` | `aura/projects/store.py` | `load_project(project_id) -> ProjectSpace | None` | 3 |
| `RunHistoryStore.save_run()` | `aura/drones/store.py` | `save_run(workspace_root, receipt)` | 3 |
| `RunHistoryStore.load_run()` | `aura/drones/store.py` | `load_run(workspace_root, run_id) -> DroneReceipt | None` | 3 |
| `DroneRunner.active_runs()` | `aura/drones/runner.py` | (needs verification — is there a method?) | 3 |
| `DroneRunner.cancel_run()` | `aura/drones/runner.py` | `cancel_run(run_id)` | 3 |
| `DroneRunner.status_changed` | `aura/drones/runner.py` | Signal(str, str, str) — run_id, name, status | 3 |

### Bridge signals CompanionManager hooks into

| Signal | Signature | Event mapping |
|---|---|---|
| `content_delta` | `Signal(str)` | → `chat.message.delta` |
| `reasoning_delta` | `Signal(str)` | → `planner.status` ("reasoning") |
| `tool_call_start` | `Signal(int, str, str)` | → `planner.status` ("using_tool") |
| `tool_call_end` | `Signal(int)` | → `planner.status` ("tool_done") |
| `done` | `Signal(str, str)` | → `chat.message.complete` |
| `finished` | `Signal(str, dict)` | → `chat.message.complete` |
| `api_error` | `Signal(int, str)` | → `error` |
| `started` | `Signal()` | → `planner.status` ("running") |

---

## Roadmap Summary

```
Phase 0 (Foundation)
  ├── aura/companion/ module skeleton
  ├── relay/ skeleton
  ├── companion-web/ skeleton
  ├── AppSettings fields
  └── companion settings page (basic)

Phase 1 (Local Proof)
  ├── CompanionManager start/stop lifecycle
  ├── WS client connect/reconnect
  ├── Relay echo server
  ├── Mobile web: login → desktop list → chat → fake response
  └── desktop.online / offline events

Phase 2 (Chat Bridge)
  ├── Route chat.send → ConversationBridge.send()
  ├── Hook bridge signals → Companion events
  ├── Stream chat.message.delta / complete
  └── Conversation create/select

Phase 3 (Projects & Runs)
  ├── project.list_recent → ProjectStore → DTOs
  ├── project.select → set workspace root
  ├── Active run summaries (Worker + Drone)
  ├── run.cancel
  ├── receipt.list_recent
  └── Mobile screens for runs, receipts

Phase 4 (Auth & Polish)
  ├── Pairing code flow
  ├── JWT device tokens
  ├── SQLite persistence in Relay
  ├── Device management in desktop settings
  ├── TLS/production Relay config
  └── Mobile login screen with pairing code
```
