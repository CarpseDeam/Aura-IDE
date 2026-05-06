# Aura — Specification (Phase 1 + Phase 2 plan)

Aura is a Windows desktop chat app for troubleshooting code with the DeepSeek V4 LLM.
The app is a daily driver focused on safety (every write is gated), groundedness
(the model uses tools, not guesses), and a polished dark UI in the spirit of
Cursor's chat panel and Zed's agent mode.

This document captures the Phase-1 architecture so Phase 2 can extend it without
breaking contracts.

---

## 1. Stack

| | |
|---|---|
| Language | Python 3.10+ (developed on 3.13) |
| GUI | PySide6 (Qt 6.6+) |
| API client | `openai>=1.40` against the DeepSeek base URL |
| Settings/data | `platformdirs` (per-user config + data) |
| Images | Pillow |
| Validation | pydantic 2 (currently lightweight; settings live in `config.py`) |
| Install | `pip install -e .` (editable) |
| Run | `python -m aura` |

DeepSeek API key is read from `DEEPSEEK_API_KEY`. On Windows it is typically a
User-scope environment variable; the app fails fast if unset.

---

## 2. Top-level directory layout

```
aura/
  __main__.py              # entry point — wires app, theme, MainWindow
  config.py                # settings, paths, model registry, pricing constants
  client/
    events.py              # streaming event dataclasses
    deepseek.py            # DeepSeekClient.stream(...) -> Iterator[Event]
  conversation/
    history.py             # History with for_api() — the replay-rule trap
    manager.py             # ConversationManager — model->tool->model loop
    tools/
      registry.py          # ToolRegistry — workspace jail, tool defs, dispatch
      fs_read.py           # read_file, list_directory, glob
      fs_write.py          # propose_write, propose_edit (no FS mutation here)
      backup.py            # timestamped pre-write backups
  gui/
    theme.py               # dark palette + global stylesheet
    main_window.py         # QMainWindow, three-pane splitter, toolbar
    chat_view.py           # transcript with all card types
    input_panel.py         # composer (text/drag/paste/picker/send/stop)
    diff_dialog.py         # modal diff approval dialog
  bridge/
    qt_bridge.py           # ConversationBridge — QThread + blocking approval
scripts/
  smoke_client.py          # streaming with/without thinking
  smoke_tools.py           # all five tools + jail + read-only
  smoke_history.py         # for_api() replay-rule unit test
  smoke_conversation.py    # full tool loop, multi-turn (replay-rule live)
  smoke_vision.py          # PNG-via-content-array verification
  smoke_gui.py             # launch + auto-quit smoke
SPEC.md
pyproject.toml
```

---

## 3. DeepSeek client (`aura/client/`)

### 3.1 Verified facts

- Endpoint: `https://api.deepseek.com`. Use `OpenAI(api_key=..., base_url=...)`.
- Models: **`deepseek-v4-flash`** (default), **`deepseek-v4-pro`**.
  Legacy `deepseek-chat` / `deepseek-reasoner` are **not used** (deprecated 2026-07-24).
- Thinking mode:
  - on: `extra_body={"thinking": {"type": "enabled"}}`,
    `reasoning_effort` is `"high"` or `"max"`. Do **not** pass
    temperature / top_p / penalties — the API silently ignores them.
  - off: `extra_body={"thinking": {"type": "disabled"}}`. Temperature is allowed (we use 0.7).
- Streaming: `stream=True`, `stream_options={"include_usage": True}` for the final usage chunk.
- Streaming chunks expose `delta.reasoning_content` (CoT) **or** `delta.content` (final answer).
  Accumulate them in **separate buffers**.
- Tool calls stream as fragments keyed by `index`. Multiple calls can stream in parallel.

### 3.2 Event types (`client/events.py`)

All events are dataclasses (cross-thread safe via Qt signals):

| Event | Fields |
|---|---|
| `ReasoningDelta` | `text` |
| `ContentDelta` | `text` |
| `ToolCallStart` | `index`, `id`, `name` |
| `ToolCallArgsDelta` | `index`, `args_chunk` |
| `ToolCallEnd` | `index` |
| `Usage` | `prompt_tokens`, `completion_tokens`, `cache_hit_tokens`, `cache_miss_tokens` |
| `Done` | `finish_reason`, `full_message` (complete assistant dict ready for history) |
| `ApiError` | `status_code` (or None), `message` |
| `ToolResult` | (manager-emitted) `tool_call_id`, `name`, `ok`, `result`, `extras` |

`Done.full_message` shape:
```python
{
    "role": "assistant",
    "content": str | None,
    "reasoning_content": str | None,
    "tool_calls": [...]   # only if the model emitted any
}
```

### 3.3 `DeepSeekClient.stream(...)`

Signature:
```python
def stream(
    self,
    messages: list[dict],
    tools: list[dict] | None,
    model: ModelId,
    thinking: ThinkingMode,                # "off" | "high" | "max"
    cancel_event: threading.Event | None,
) -> Iterator[Event]: ...
```

Contracts:
- **Never raises.** Network/API failures are converted to `ApiError` events.
- Detects usage chunk regardless of whether server emits it standalone or with the final choice.
- Builds a complete assistant dict server-side (reproduces the streamed message
  exactly, in OpenAI tool-call format), so history append is trivial.
- Honors `cancel_event` — if set, the iterator returns cleanly with whatever
  has accumulated so far. The manager handles partial-message persistence.

---

## 4. History — the multi-turn replay trap (`conversation/history.py`)

### 4.1 The rule

> If an assistant turn contained `tool_calls`, its `reasoning_content` **MUST** be
> passed back to the API in subsequent requests, or the API returns 400:
> `"The reasoning_content in the thinking mode must be passed back to the API."`
>
> If the assistant turn did **not** contain `tool_calls`, `reasoning_content` is ignored
> by the API; we strip it for cleanliness.

This is the trap that has open bugs in OpenCode and Hermes. Aura solves it in **one place**:
`History.for_api()`.

### 4.2 API

```python
class History:
    system_prompt: str | None
    messages: list[dict]   # OpenAI-format messages, plus reasoning_content on assistants

    set_system(prompt)
    append_user_text(text)
    append_user_multimodal(parts)            # for image+text turns
    append_assistant(full_message)           # ALWAYS stores reasoning_content
    append_tool_result(tool_call_id, content_str)
    truncate_after(index)                    # used on cancel/rewind
    for_api() -> list[dict]                  # the only place that strips reasoning_content
```

`for_api()` walks `messages`. For assistant entries:
- If `tool_calls` is present → keep `reasoning_content`.
- Else → drop `reasoning_content`.

Storage always retains `reasoning_content` so the GUI can re-show it and so a later
tool-call turn could (in principle) reference earlier reasoning if the contract changed.

Verified by `scripts/smoke_history.py`.

---

## 5. Tools (`conversation/tools/`)

### 5.1 Registry

`ToolRegistry(workspace_root: Path, read_only: bool=False)`:

- `tool_defs() -> list[dict]` — OpenAI tool schema. **Returns read tools only when `read_only`** —
  the model literally cannot call writes; safety floor #4.
- `set_workspace_root(path)`
- `set_read_only(value)`
- `execute(name, args, approval_cb, reject_all=False) -> ToolExecResult`

### 5.2 Workspace jail

`_resolve_in_root(path)` rules:
- Path is required and non-empty.
- Reject any segment equal to `..` (even before resolution).
- Resolve the path. If absolute outside the root, reject.
- After resolve, must be `is_relative_to(workspace_root)`.

Verified by `scripts/smoke_tools.py` ("rejects ..", "rejects abs outside root").

### 5.3 Tool catalog

| Name | Args | Behavior |
|---|---|---|
| `read_file` | `path` | UTF-8 read; capped at 200 KB; appends a truncation marker if hit. |
| `list_directory` | `path` | Returns separate `directories` (suffixed with `/`) and `files` lists. Skips dotfiles, `__pycache__`, `.venv`, `.git`, `node_modules`, `.import` dir, and `*.import` files. |
| `glob` | `pattern` | `Path(root).rglob(pattern)`, capped at 200 matches, same skip rules, file results only. |
| `write_file` | `path, content` | Calls `approval_cb`. On approve → backup existing file → write. |
| `edit_file` | `path, old_str, new_str` | Locate `old_str` (must match exactly once — otherwise descriptive error). Build proposed content. Calls `approval_cb`. Backup + write. |

All tools return JSON-stringified objects; the registry wraps the raw dicts.

### 5.4 Backups

Path: `<workspace>/.aura/backups/<UTC-ISO-timestamp>/<relpath>`.
Created **before** an approved write overwrites an existing file (write_file overwrite,
edit_file). Never auto-cleaned (Phase 2 may add a janitor).

### 5.5 Approval contract

```python
@dataclass
class ApprovalRequest:
    tool_name: str          # "write_file" | "edit_file"
    rel_path: str
    old_content: str
    new_content: str
    is_new_file: bool

@dataclass
class ApprovalDecision:
    action: Literal["approve", "reject", "reject_all"]
    note: str = ""

ApprovalCallback = Callable[[ApprovalRequest], ApprovalDecision]
```

`reject_all` short-circuits the rest of the writes in the current user turn — the manager
remembers it across rounds within one `send()` call.

---

## 6. ConversationManager (`conversation/manager.py`)

```python
class ConversationManager:
    def __init__(self, client: DeepSeekClient, history: History, tool_registry: ToolRegistry): ...

    def send(
        self,
        on_event: Callable[[Event], None],
        approval_cb: ApprovalCallback,
        cancel_event: threading.Event,
        model: ModelId,
        thinking: ThinkingMode,
    ) -> None: ...
```

The caller appends the user's message before invoking `send()`.

Loop (max `MAX_TOOL_ROUNDS = 10`):
1. Stream from client with `history.for_api()` and `tool_registry.tool_defs()`.
2. Forward every event to `on_event` (and to GUI via the bridge).
3. On `Done`, append the full assistant message to history.
4. If no `tool_calls`, return.
5. For each `tool_call`: dispatch via the registry (calling `approval_cb` for writes),
   append the result to history, emit `ToolResult` event.
6. Loop.

Cancellation: `cancel_event` is checked between rounds and inside the client. On
mid-stream cancel, whatever has accumulated is appended as a content-only assistant
message (any partial `tool_calls` are dropped) so the conversation is still well-formed.

Hitting `MAX_TOOL_ROUNDS` emits an `ApiError` and stops.

Verified by `scripts/smoke_conversation.py` (two-turn, second turn forces tool call —
exercises the replay rule live and observes 0 errors).

---

## 7. GUI (`aura/gui/`)

### 7.1 MainWindow

QMainWindow + 1-row toolbar + horizontal QSplitter (3-pane layout).

```
+-------------------------------------------------------------+
| [New Conversation] [Read-Only] [READ-ONLY badge]   [name]   |
+----------------+---------------------------------------------+
|  Workspace     |                                             |
|  <root>        |     ChatView (scrollable cards)             |
|  [Change Root] |                                             |
|                |                                             |
|  Phase 2 hint  |---------------------------------------------+
|                |     InputPanel (chips, editor, controls)    |
+----------------+---------------------------------------------+
```

- **Toolbar**: New Conversation, Read-Only Mode toggle (with lock icon when on,
  warm-yellow `READ-ONLY` badge), spacer, workspace name.
- **Left pane (Phase 1 stub)**: workspace label + Change Root button + a hint that
  workspace tree / search / history land in Phase 2.
- **Center**: `ChatView`.
- **Bottom**: `InputPanel`.

### 7.2 ChatView (`chat_view.py`)

Vertical stack of cards inside a scroll area. The view auto-scrolls to bottom on
any append. Public API used by the bridge:

```python
chat.add_user(text, image_b64s)
chat.begin_assistant()                       # opens a new AssistantCard
chat.append_reasoning(text)                  # streamed CoT
chat.append_content(text)                    # streamed answer
chat.add_tool_call(tool_call_id, name)
chat.append_tool_args(tool_call_id, fragment)
chat.set_tool_result(tool_call_id, ok, result_text)
chat.add_diff_card(tool_call_id, rel_path, old, new, decision, is_new_file)
chat.add_error(title, message)
chat.assistant_done()                        # finalize markdown render
chat.reset()                                 # New Conversation
```

Card types:
- **UserCard** — right-leaning style, shows base64 image thumbnails above text.
- **AssistantCard** — composite:
  - Collapsible "Thinking…" section (italic muted text). Collapses automatically once content streaming begins.
  - Streamed content label (renders as Markdown via `Qt.TextFormat.MarkdownText` once `assistant_done()` fires; Phase 1 deliberately skips Pygments syntax highlighting).
  - Inline ToolCallCards in stream order.
  - Inline DiffCards in stream order (after the user decides on a write).
- **ToolCallCard** — collapsible header `> name("path")  (running|done|failed)`,
  body shows pretty-printed args + result. Auto-expands on failure.
- **DiffCard** — read-only unified diff with red/green hunk styling, header colored
  by decision (green Applied, red Rejected).
- **ErrorCard** — red-tinted card surfacing API/tool/render errors verbatim.

### 7.3 InputPanel (`input_panel.py`)

- Auto-grow `QTextEdit` (1 line min, 8 lines max).
- Drag-drop: image files become attachments; non-image files insert
  `[user attached: <relpath>]` text refs that get appended to the prompt.
- Clipboard paste (Ctrl+V): if the clipboard holds a `QImage`, attach it
  as `pasted.png`; otherwise plain-text paste.
- Attachment chips above the editor (with thumbnail + filename + close).
- Model picker (Flash/Pro), Thinking toggle (Off/High/Max).
- Send (`Ctrl+Enter`) / Stop (visible during streaming).

`SendPayload(text, attachments[Attachment])` is emitted on send.

### 7.4 DiffApprovalDialog (`diff_dialog.py`)

Modal QDialog opened by the bridge on the GUI thread when a write is proposed:
- 900x640 default, monospaced unified diff with red/green hunk highlighting.
- Buttons: Apply (default, green) / Reject / Reject all in this turn.
- Returns an `ApprovalDecision`.

---

## 8. Qt bridge (`bridge/qt_bridge.py`)

The `ConversationManager` is synchronous; the GUI must not block. The bridge owns:

- A persistent `History`, `ToolRegistry`, `DeepSeekClient`, and `ConversationManager`.
- A per-send `QThread` running a `_Worker` that calls `manager.send(...)`.
- An `_ApprovalProxy` that marshals approval requests onto the GUI thread via
  `QMetaObject.invokeMethod(... BlockingQueuedConnection)`. The worker thread
  blocks until `DiffApprovalDialog.exec()` returns on the main thread.

Public Qt facade (signals fired on the GUI thread):

```python
class ConversationBridge(QObject):
    started, finished
    reasoningDelta(str), contentDelta(str)
    toolCallStart(str, str)        # tool_call_id, name
    toolCallArgs(str, str)
    toolCallEnd(str)
    toolResult(str, str, bool, str, dict)
    diffDecided(str, str, str, str, str, bool)  # tool_call_id, decision, rel_path, old, new, is_new_file
    streamDone(str, dict)
    apiError(int, str)
    usageEmitted(int, int, int, int)
```

The bridge translates the streaming `index` keys into stable `tool_call_id`s
before forwarding events to the GUI, so views work in terms of IDs only.

Stop button → `bridge.request_cancel()` sets the cancel event. The worker thread
exits cleanly between rounds; the GUI re-enables input.

---

## 9. Read-Only Mode (safety-critical)

`MainWindow._on_read_only_toggled(checked)` calls
`bridge.set_read_only(checked)` which sets the registry flag. The next API
call's `tools=[...]` payload contains **only** read tools. The model has no
ability to call write tools because they aren't in the schema it sees.

Even if the model were to fabricate a tool name, registry dispatch refuses
(`smoke_tools.py` "read-only: write_file dispatch refused").

The toolbar shows a lock-prefixed action label and a warm-yellow `READ-ONLY` badge.

---

## 10. Image input (vision) — Phase 1 status

**Verified result (2026-05-04):** `deepseek-v4-pro` and `deepseek-v4-flash` both
return HTTP 400 on the OpenAI multimodal content-array format. Verbatim error:

```
Failed to deserialize the JSON body into the target type: messages[0]:
unknown variant `image_url`, expected `text` ... invalid_request_error
```

Decision: keep the image-attach UI enabled. When the user attaches and sends an
image, the API call fails and the error renders verbatim in an `ErrorCard`.
We do **not** silently strip images.

Phase 2 will route image-bearing turns to a vision-capable model (e.g.
`deepseek-vl2`) or proxy through a separate vision client.

---

## 11. Pricing (display only, Phase 2 surfaces it)

`config.MODELS` holds per-model `input_per_m_usd`, `output_per_m_usd`,
`cache_hit_per_m_usd`. The `Usage` event already carries the inputs Phase 2
needs for the status-bar cost meter:

```python
prompt_tokens, completion_tokens, cache_hit_tokens, cache_miss_tokens
```

---

## 12. System prompt

Set once in `MainWindow.__init__`. Lives at `aura.gui.main_window.SYSTEM_PROMPT`.
Tells the model: (a) it has filesystem tools scoped to the workspace; (b) use
them — do not guess; (c) prefer `edit_file` over `write_file`; (d) every write
needs user approval; (e) explain — do not pretend — in Read-Only mode.

---

## 13. Smoke tests

| Script | What it verifies |
|---|---|
| `smoke_client.py` | Streaming with thinking off (no reasoning) and high (reasoning + content). |
| `smoke_tools.py` | All five tools, jail rejection (`..` and abs paths), edit_file 0/1/2-match cases, backup creation, read-only mode strips write tools and dispatch refuses. |
| `smoke_history.py` | `for_api()` keeps `reasoning_content` on tool-call assistants and strips it on plain ones. |
| `smoke_conversation.py` | Full tool loop, two turns. The second triggers another tool call after a tool-call assistant exists in history (replay-rule live test). |
| `smoke_vision.py` | Sends a Pillow-generated PNG as an `image_url` to Pro and Flash. Documents the 400 result. |
| `smoke_gui.py` | Constructs `MainWindow`, shows it, auto-quits — sanity check that nothing imports/initializes wrong. |

All scripts force UTF-8 stdout (Windows cp1252 will otherwise crash on emoji output).

---

## 14. Phase 2 plan (deferred)

These are **not** in Phase 1 — keep diffs focused.

1. **Workspace tree pane** — replace the left-pane stub with a filtered file tree
   (respect existing `SKIP_DIRS` / `SKIP_FILE_SUFFIXES`). Drag-from-tree to compose
   message. Click-to-open in chat for context.
2. **Conversation persistence** — JSON history files in `data_dir()/conversations/`,
   sidebar of past chats in the left pane below the workspace section.
3. **Status-bar cost meter** — total tokens + USD this session, using `Usage` events.
4. **Syntax highlighting in code blocks** — Pygments → `QTextCharFormat` runs on
   final assistant content. Phase 1 uses Markdown-as-text via `QTextFormat.MarkdownText`.
5. **Settings dialog** — model defaults, thinking defaults, custom system prompt,
   font size, max tool rounds.
6. **Vision routing** — when an image is attached and current model can't process it,
   proxy to a vision-capable model and stitch results.
7. **Backup janitor** — auto-prune `<workspace>/.aura/backups/` older than N days.

Phase-2 changes that should NOT break Phase-1 contracts:
- `History.for_api()` algorithm (anchor of the replay-rule trap).
- `Event` shape (consumers across bridge + GUI assume the dataclasses).
- `ApprovalRequest` / `ApprovalDecision` shape.
- `ToolRegistry.tool_defs()` semantics (read_only swaps the schema, not just hides UI).
