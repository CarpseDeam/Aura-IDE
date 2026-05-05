# Architecture

## System Design

Aura is built on a decoupled architecture using Qt signals and slots to bridge the synchronous conversation logic with the asynchronous GUI.

### Components

- **GUI Layer (`aura/gui/`)**: Implements the user interface using PySide6.
  - `MainWindow`: Orchestrates the layout and connects the bridge to the UI components.
  - `WorkspaceTree`: Provides a tree view of the current workspace.
  - `InputPanel`: Handles user input, model selection, and thinking modes.
  - `ChatView`: Displays the conversation transcript.
  - `SettingsDialog`: Allows users to configure application defaults.
- **Bridge Layer (`aura/bridge/`)**: Manages the lifecycle of a conversation in a separate thread, providing thread-safe communication between the UI and the manager.
- **Conversation Layer (`aura/conversation/`)**: Handles history, tool execution, and persistence.
  - `ConversationManager`: Supports specialized roles for "Planner" (read-only + dispatch) and "Worker" (read/write execution).
  - `ToolRegistry`: Manages tool definitions and permissions based on the active role and Read-Only mode.
  - `Persistence`: Handles saving and loading conversation history (schema v2) to `.aura/conversations/` within the workspace, including worker dispatch records.

### Data Flow

1. User sends a message via `InputPanel`.
2. `MainWindow` triggers `ConversationBridge` to run a **Planner** turn.
3. If a code change is needed, the Planner calls `dispatch_to_worker` with a task specification.
4. `ConversationBridge` marshals the request to the GUI, which renders a `SpecCard`.
5. Upon user approval (Dispatch), the bridge spawns a **Worker** manager to execute the spec on a background thread.
6. Worker tool calls (e.g., `edit_file`) are bridged to the GUI for user approval via a diff dialog.
7. Worker results are returned to the Planner, which continues the conversation.
8. Token usage is tracked per model and role, then forwarded to `MainWindow` to update the status bar cost meter.
9. Conversations are automatically saved to disk after each turn.
