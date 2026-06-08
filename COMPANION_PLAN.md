# Aura Companion — Phased Implementation Plan

Derived from [COMPANION.md](./COMPANION.md). This plan breaks the Companion feature into 5 concrete phases mapped to the existing Aura codebase.

---

## Phase 0: Foundation (Desktop-side Companion module + protocol)

**Goal:** Create the `aura/companion/` module structure, the shared protocol envelope, and the `AppSettings` plumbing — without any networking yet.

### Files to create

| Path | Purpose |
|---|---|
| `aura/companion/__init__.py` | Module init |
| `aura/companion/protocol.py` | JSON envelope schemas, command/event type enums, serialization |
| `aura/companion/manager.py` | `CompanionManager` — lifecycle, state machine (disabled/connecting/connected/error), reconnect backoff stub |
| `aura/companion/client.py` | `CompanionRelayClient` — WebSocket connect/send/receive — **stub class** that just logs in Phase 0 |
| `aura/companion/auth.py` | Device identity, token storage, pairing state |

### Files to modify

| Path | Change |
|---|---|
| `aura/settings.py` | Add `AppSettings` fields: `companion_enabled: bool = False`, `companion_relay_url: str = "ws://localhost:8765"`, `companion_device_name: str = ""` (auto-populated with hostname), `companion_device_id: str = ""` (auto-generated UUID). Wire into `from_dict()`. |
| `aura/gui/settings_pages/companion_page.py` (new) | New settings tab: enable/disable toggle, relay URL field, device name, connection status indicator |
| `aura/gui/settings_dialog.py` | Import and register `CompanionPage` as a new tab |
| `aura/__main__.py` | On startup, if `companion_enabled`, instantiate `CompanionManager` and start background connection |

### Acceptance

- Settings save/load `companion_enabled` and `companion_relay_url`
- Companion settings tab visible in Settings dialog
- Toggle shows "disabled" state; device name auto-fills with hostname
- `python -m py_compile aura/companion/*.py` passes

---

## Phase 1: Local Relay + Proof of Connectivity

**Goal:** A working local developer proof: Relay backend runs locally, desktop connects, a simple web page connects, and a `chat.send` message flows end-to-end with a fake response.

### Files to create

| Path | Purpose |
|---|---|
| `relay/main.py` | FastAPI/Starlette app entry + WebSocket endpoint |
| `relay/auth.py` | Dev-mode hardcoded auth (skip in Phase 1) |
| `relay/models.py` | Pydantic models for envelope, commands, events |
| `relay/websocket.py` | WebSocket handler: manage connections, route messages by `desktop_id` |
| `relay/sessions.py` | In-memory session store: track connected desktops → paired phone sessions |
| `relay/protocol.py` | Message routing logic |
| `relay/requirements.txt` | `fastapi`, `websockets`, `uvicorn`, `pydantic` |
| `companion-web/` (initial skeleton) | Simple static HTML/JS page that connects to Relay, shows connected desktop, has a text input to send a chat message and display a response |

### Files to modify

| Path | Change |
|---|---|
| `aura/companion/client.py` | Implement real WebSocket connection (using `websockets` lib or `asyncio`), `send_command()`, `on_event` dispatch, ping/pong keepalive, reconnect backoff |
| `aura/companion/manager.py` | Wire `CompanionManager` to hook into Aura startup/shutdown, hold ref to `CompanionRelayClient`, expose event callbacks |
| `aura/companion/protocol.py` | Finalize envelope schema + command/event type definitions |
| `aura/__main__.py` | Start Companion connection in background thread after Qt init |
| `pyproject.toml` | Add `websockets` (or `httpx`-based WS) dependency |

### Acceptance

- `uvicorn relay.main:app` starts on `ws://localhost:8765`
- Desktop Aura connects on startup (if companion_enabled)
- Open `companion-web/index.html` → connects to Relay → sees "Desktop Online"
- Type text in phone web → sends `chat.send` → Desktop logs it
- Desktop sends back a hardcoded fake response → phone displays it

---

## Phase 2: Real Desktop Chat Bridge

**Goal:** Route phone `chat.send` commands into the real `ConversationManager.send()` pipeline and stream Planner/assistant responses back through the Relay to the phone.

### Files to modify

| Path | Change |
|---|---|
| `aura/companion/client.py` | Add `stream_response()` — receives events from `ConversationManager` callback and forwards them as Relay events |
| `aura/companion/manager.py` | Hold a reference to the active `ConversationManager`, hook into its `send()` event callback, map events (`ContentDelta`, `ToolResult`, `Done`, etc.) to Relay event types |
| `aura/companion/protocol.py` | Map `chat.message.delta`, `chat.message.complete`, `planner.status`, `error` event payloads |
| `aura/conversation/manager.py` or bridge | Expose a method to accept a `chat.send` from Companion — find or create thread, append user message, call `send()`, relay events |
| `companion-web/src/` | Build out a real chat UI: message list (user + assistant), streaming text input, send button, status banner |

### Acceptance

- Phone sends "Plan the README cleanup" → Desktop receives it → Planner runs → streamed text appears on phone
- New conversation and continue-existing-conversation both work
- Planner reasoning / tool calls are not shown on phone (just the assistant text output)
- If desktop is idle / no active conversation, starting a chat creates one

---

## Phase 3: Projects, Runs & Receipts

**Goal:** Expose recent projects from `ProjectStore`, allow project selection, show active Worker/Drone run summaries, show receipts.

### Files to modify

| Path | Change |
|---|---|
| `aura/companion/manager.py` | Add query handlers: `project.list_recent`, `project.select`, `conversation.list_recent`, `run.list_active`, `run.cancel`, `receipt.list_recent`, `receipt.get` |
| `aura/companion/client.py` | Route command types to the appropriate manager methods |
| `aura/companion/protocol.py` | Add all command/event types from the spec |
| `companion-web/src/screens/` | Build: `Projects.tsx`, `Runs.tsx`, `Receipts.tsx`, `Desktops.tsx` with cards, status indicators, cancel button |

### Key integration points

- `ProjectStore.list_projects()` — already exists
- `ConversationManager.send()` already has cancel via `cancel_event` — wire `run.cancel` to that
- `ProjectStore.list_threads()` — already exists
- Receipt/write summary data would need a new accessor — likely reading from conversation history or a receipts store

### Acceptance

- Phone sees list of recent Aura projects
- Selecting a project shows: Continue chat / Start new chat / Active runs / Receipts
- Active run card shows status and cancel button
- Cancelling a run via phone stops the active generation
- Receipts are visible after a run completes

---

## Phase 4: Auth, Pairing & Production Polish

**Goal:** Real device pairing, production-ready auth, TLS, Companion settings UI complete, device revocation.

### Files to create/modify

| Path | Change |
|---|---|
| `relay/auth.py` | Real auth: sign-up/sign-in, device registration, token issuance (JWT or similar), device revocation |
| `relay/models.py` | Add user/device database models (SQLite via SQLAlchemy or similar) |
| `relay/main.py` | Add REST endpoints for signup, login, list devices, revoke device |
| `aura/companion/auth.py` | Implement pairing flow: generate device key pair, register with Relay, store token |
| `aura/companion/manager.py` | Add device management: list paired devices, revoke |
| `aura/gui/settings_pages/companion_page.py` | Show paired devices list, revoke button, connection status with error details, QR code or pairing code display |
| `companion-web/src/screens/Login.tsx` | Login/signup screen, pairing flow |
| `companion-web/src/` | Production UI polish: error states, loading spinners, reconnection banner |
| TLS config for Relay | `uvicorn` with SSL cert, environment-based config |

### Acceptance

- Phone can sign in
- Phone sees only its paired desktops
- Desktop can revoke a paired phone
- If Relay is unreachable, desktop still launches and works normally
- All traffic is encrypted in production config

---

## Phase Dependency Graph

```
Phase 0 (Module skeleton + settings)
    ↓
Phase 1 (Local Relay + proof of connectivity)
    ↓
Phase 2 (Real chat bridge)
    ↓
Phase 3 (Projects, runs, receipts)
    ↓
Phase 4 (Auth, pairing, production)
```

Phases 0–1 can be done in parallel with the Relay backend. Phase 2 is the biggest integration challenge — it requires understanding how `ConversationManager.send()` events map to the Companion protocol. Phase 3 builds naturally on top. Phase 4 is the security layer.

---

## Key Risk Notes

- **ConversationManager threading:** `ConversationManager` runs on a worker thread (not the main Qt thread). The Companion event bridge will need thread-safe event forwarding.
- **Event mapping:** Not all `Event` types from ConversationManager map 1:1 to Companion protocol events. Need a careful translation layer in `manager.py`.
- **Relay statefulness:** The Relay must maintain in-memory session mappings (desktop_id ↔ phone_session). If Relay restarts, all desktops reconnect with backoff.
- **No file contents on Relay:** Per security rules, the Relay never receives file contents or API keys. Project list sends only project_id + name, not the full root path.
