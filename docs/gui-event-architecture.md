"""
# GUI Event Architecture - Cleanup Boundary

A concise boundary note preparing Aura for slow, section-by-section GUI cleanup using **EventBus**, **lifecycle hooks**, and **projectors/controllers**. No runtime behavior changes.

---

## Pattern Layers

### 1. EventBus emits immutable facts, not widget commands

- `AuraEvent` is a frozen dataclass. Subscribers receive **facts** (something happened), not instructions (go do X).
- A subscriber must never mutate bus state or call back into the emitter.
- The bus lives in `aura/events/` - pure Python, zero Qt imports.

### 2. Lifecycle hooks enforce policy at named checkpoints

- `LifecycleHooks` (in `aura/lifecycle/`) owns `notify` (observe) and `gate` (decide) registries.
- Matchers route by `HookContext`, not by event topic.
- Gates can block/reject before a tool runs; notifiers observe after the fact.
- Hooks are infrastructure policy, not widget logic.

### 3. Projectors/controllers translate facts into stable UI projections

- A projector subscribes to EventBus topics, transforms `AuraEvent` facts into a domain-specific projection (e.g. `ActivityEntry` list, `WorkArtifactProjection`).
- A controller owns the projection and exposes a Qt-safe signal or callback for bridge delivery.
- Example: `WorkerActivityController` subscribes to `worker.*` and `work_artifact.*` topics, appends `ActivityEntry` records, and fires `set_on_change` - a single callback the bridge connects to a Qt signal.

### 4. Qt widgets/cards render projections and should not coordinate workflow state

- Widgets receive pre-digested state (strings, models, lists) and render it.
- No widget subscribes directly to the EventBus.
- No widget makes lifecycle decisions (start/stop worker, dispatch next item) - that belongs in the bridge layer.
- `ChatView` should trend toward being a card host/renderer, not the owner of tool lifecycle or workflow orchestration decisions.

### 5. MainWindow composes systems but should not own run lifecycle logic

- `MainWindow` creates subsystems, wires them together, and provides the top-level layout.
- It currently owns dozens of direct signal `.connect()` calls in `__init__` (~50+ connect calls). This signal wiring is the first safe extraction target.
- Run lifecycle (start/finish, cancel, dispatch) belongs in the bridge/backend layer, not in `MainWindow` methods like `_on_started`, `_on_finished`, `_on_tool_result`.

### 6. Backend-owned projectors may emit Qt-safe bridge signals for GUI consumption

- A projector that lives outside the GUI layer (e.g. in `aura/bridge/`) can emit via `QObject` signals when it needs to cross the Qt boundary.
- These signals are narrow, typed, and named for what they deliver (e.g. `usage_updated`, `activity_snapshot_changed`), not for what the recipient should do.

### 7. Avoid random widgets subscribing directly to broad bus events and mutating other widgets

- No widget should `bus.subscribe(ALL, ...)` and poke at other widgets' internals.
- Cross-talk must go through a named projector -> bridge signal -> widget consumer chain.

---

## First Safe Extraction: MainWindow Signal Wiring

**Target:** `MainWindow.__init__` signal wiring block (~50+ `.connect()` calls spanning lines ~130-410).

**Why it's safe:**
- Signal wiring is pure configuration - it maps sender to receiver without changing what either end does.
- Each connect call can be extracted into a named registration method or a dedicated `MainWindowSignalWiringController` without touching business logic.
- No work artifact, spec card, dispatch proxy, or chat view method bodies are affected.
- Extraction preserves the exact same signal-slot graph; only the hosting method moves.

**How:**
1. Create a `MainWindowSignalWiring` class (or similar) that receives references to all subsystems (toolbar, chat, bridge, playground, etc.) and wires them in a single `wire()` call.
2. `MainWindow.__init__` calls `wire()` instead of inline connects.
3. Subsequent cleanup can then refactor individual signal chains (e.g. toolbar -> settings -> status bar) into dedicated projectors without touching `MainWindow.__init__`.

**What is NOT first:**
- `WorkArtifact` behavior (touches dispatch proxy, artifact lifecycle)
- `SpecCard` behavior (touches approval, worker dispatch)
- `ChatView` refactoring (larger scope, riskier)
- Adding new event topics

---

## Non-goals

- Do not change runtime behavior.
- Do not touch `WorkArtifact` behavior.
- Do not touch `SpecCard` behavior.
- Do not touch `DispatchProxy` worker flow.
- Do not refactor `ChatView` yet.
- Do not add new event topics unless needed for documentation examples.

---

## Acceptance Criteria

- This doc gives future cleanup tasks a clear pattern: **EventBus fact -> projector/controller -> Qt-safe signal/projection -> widget render**.
- This doc explicitly recommends one section at a time and no mixed cleanup/feature work.
- This doc names `MainWindow` signal wiring as the first safe extraction candidate.
"""
