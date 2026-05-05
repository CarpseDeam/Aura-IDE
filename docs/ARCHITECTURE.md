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
  - `Persistence`: Handles saving and loading conversation history to `.aura/conversations/` within the workspace.
- **Client Layer (`aura/client/`)**: Communicates with the DeepSeek API.

### Data Flow

1. User sends a message via `InputPanel`.
2. `MainWindow` triggers `ConversationBridge`.
3. `ConversationBridge` starts a worker thread that uses `ConversationManager` to run the tool loop.
4. Token usage is tracked per model and forwarded to `MainWindow` to update the status bar cost meter.
5. Conversations are automatically saved to disk after each assistant turn.
