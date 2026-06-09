# Development

## Architecture

Aura follows a three-layer architecture:

```
┌─────────────────────────────────────────────────┐
│                   GUI Layer                      │
│  MainWindow, ChatView, InputPanel, DiffDialog,  │
│  SettingsDialog, OnboardingDialog, DroneBay,     │
│  EdgeRails, StatusBar, TerminalWindow, etc.      │
├─────────────────────────────────────────────────┤
│                  Bridge Layer                    │
│     QtBridge — Qt signals ↔ async events         │
│     SendHandler — send/stop/undo routing          │
│     WorkerHandler — Worker lifecycle management  │
│     Controllers — UI state coordination          │
├─────────────────────────────────────────────────┤
│               Conversation Layer                 │
│  Manager — chat loop, tool dispatch              │
│  Tools — read/write/git/web/search/terminal      │
│  ToolRegistry — tool registration & execution    │
│  TaskRouter — intent classification              │
│  WorkflowState — Planner/Worker state machine    │
│  SpecQuality — spec validation                   │
│  CodebaseIndex — BM25 full-text index            │
│  RepoMap — AST structural map                    │
└─────────────────────────────────────────────────┘
```

## Project Structure

```
aura/
├── __init__.py
├── __main__.py              # Entry point (argparse, app launch)
├── ast_utils.py             # Python AST parsing utilities
├── cli_tools.py             # CLI executable resolution
├── config.py                # Paths, API keys, catalog cache
├── focused_actions.py       # Selection context & action prompts
├── git_ops.py               # Git integration (commit, diff, undo, snapshot)
├── handoff.py               # Spec handoff prompt generation
├── hooks.py                 # Hook manager for extensibility
├── key_manager.py           # Hardware-tethered Fernet key encryption
├── mcp_client.py            # MCP stdio client (tools/list, tools/call)
├── memory_db.py             # SQLite-based vector memory
├── models.py                # Model pricing and defaults
├── paths.py                 # Config/data dir resolution, safe path utils
├── project_env.py           # Project toolchain detection
├── prompts.py               # System prompt building (tier1 context, drone context)
├── python_env.py            # Python env detection, command rewriting
├── repo_map.py              # AST-based repository structural map
├── resources.py             # Resource path resolution
├── sandbox.py               # Command execution (host/docker)
├── settings.py              # AppSettings dataclass, load/save
├── startup_logging.py       # Early logging configuration
├── updater.py               # Windows self-updater (GitHub Releases)
├── version.py               # __version__ = "1.7.0"
├── vision.py                # Ollama vision client
├── windows_updater.cmd      # Windows update helper script
│
├── backends/                # Agent backend abstraction
│   ├── base.py              # AgentBackend ABC
│   └── api.py               # APIAgentBackend (REST API)
│
├── bridge/                  # Qt ↔ async bridge
│   └── qt_bridge.py
│
├── client/                  # Provider API clients
│   ├── deepseek.py
│   ├── google_cloud.py
│   ├── openai.py
│   ├── openrouter.py
│   └── ...
│
├── codebase_index/          # BM25 semantic search index
│   ├── indexer.py
│   ├── tokenizer.py
│   └── tool.py
│
├── companion/               # Mobile companion (WebSocket relay)
│   ├── auth.py
│   ├── client.py
│   ├── manager.py
│   ├── protocol.py
│   └── settings.py
│
├── conversation/            # Chat loop, tools, state machine
│   ├── dispatch.py
│   ├── history.py
│   ├── loop_detection.py
│   ├── manager.py
│   ├── persistence.py
│   ├── project_profile.py
│   ├── spec_quality.py
│   ├── task_router.py
│   ├── task_shape.py
│   ├── terminal_policy.py
│   ├── tool_limits.py
│   ├── tool_runner.py
│   ├── workflow_state.py
│   └── tools/               # Individual tool implementations
│       ├── _diagnostic_mixin.py
│       ├── _git_mixin.py
│       ├── _memory_mixin.py
│       ├── _planner_mixin.py
│       ├── _read_mixin.py
│       ├── _search_mixin.py
│       ├── _types.py
│       ├── _web_mixin.py
│       ├── _write_mixin.py
│       ├── backup.py
│       ├── catalog.py
│       ├── dynamic_registry.py
│       ├── executor.py
│       ├── find_usages.py
│       ├── fs_edit_structured.py
│       ├── fs_edit_transaction.py
│       ├── fs_handler.py
│       ├── fs_write.py
│       ├── git_handler.py
│       ├── grep.py
│       ├── mcp_registry.py
│       ├── registry.py
│       ├── schemas.py
│       └── web_handler.py
│
├── craft/                   # Quality / Humanizer
│   └── ...
│
├── drones/                  # Drone system
│   ├── definition.py
│   ├── background_runner.py
│   ├── receipt.py
│   ├── run.py
│   ├── runner.py
│   ├── store.py
│   ├── sync_runner.py
│   └── tool_scaffold.py
│
├── gui/                     # Qt6 UI components
│   ├── aura_widget.py
│   ├── chat_view.py
│   ├── checkpoint_dialog.py
│   ├── code_editor_pane.py
│   ├── controllers.py
│   ├── conv_persistence.py
│   ├── diff_dialog.py
│   ├── edge_rails.py
│   ├── info_hub_pane.py
│   ├── input_panel.py
│   ├── left_pane.py
│   ├── main_window.py
│   ├── main_window_toolbar.py
│   ├── markdown_renderer.py
│   ├── onboarding_dialog.py
│   ├── playground.py
│   ├── send_handler.py
│   ├── settings_dialog.py
│   ├── setup_dialog.py
│   ├── smooth_code_streamer.py
│   ├── spec_card_host.py
│   ├── spec_edit_dialog.py
│   ├── status_bar.py
│   ├── syntax.py
│   ├── terminal_drawer.py
│   ├── terminal_window.py
│   ├── theme.py
│   ├── update_dialog.py
│   ├── window_chrome.py
│   ├── worker_handler.py
│   ├── workspace_tree.py
│   ├── cards/               # Run card components
│   ├── drones/              # Drone UI components
│   ├── editor/              # Code editor components
│   ├── settings_pages/      # Settings page widgets
│   └── widgets/             # Shared widgets
│
├── projects/                # Project store
│   └── store.py
│
├── providers/               # Provider registry & catalog
│   ├── base.py
│   ├── catalog.py
│   ├── registry.py
│   └── ...
│
├── quality/                 # Quality / Humanizer
│   └── ...
│
└── scripts/                 # Dev & build scripts
    ├── build_nuitka.py
    ├── smoke_client.py
    ├── smoke_conversation.py
    ├── smoke_google_cloud.py
    ├── smoke_gui.py
    ├── smoke_history.py
    ├── smoke_planner_worker.py
    ├── smoke_research.py
    ├── smoke_tools.py
    └── smoke_vision.py
```

## Dev Install

```bash
git clone https://github.com/CarpseDeam/Aura-IDE
cd Aura-IDE
pip install -e .[dev]
```

## Smoke Tests

| Script                               | What It Tests                               |
|--------------------------------------|---------------------------------------------|
| `scripts/smoke_client.py`            | Provider API client creation and streaming  |
| `scripts/smoke_conversation.py`      | Conversation manager loop                   |
| `scripts/smoke_google_cloud.py`      | Google Cloud / Vertex AI integration        |
| `scripts/smoke_gui.py`              | Qt GUI components load and render           |
| `scripts/smoke_history.py`           | Conversation history persistence            |
| `scripts/smoke_planner_worker.py`    | Planner/Worker two-agent cycle              |
| `scripts/smoke_research.py`          | Web research sub-agent                      |
| `scripts/smoke_tools.py`             | All tool implementations                    |
| `scripts/smoke_vision.py`            | Vision preprocessing                        |

## Build Options

**Nuitka ZIP:**

```bash
python scripts/build_nuitka.py
```

Produces `dist/aura.zip` containing a standalone executable.

**Windows Installer:**

```bash
python scripts/build_nuitka.py --installer
```

Produces `dist/Aura-Setup-<version>.exe` — a per-user NSIS installer built from `scripts/installer/`.

## Release Process

1. Bump version in `aura/version.py` and `pyproject.toml`
2. Update `CHANGELOG.md`
3. Run smoke tests
4. Build: `python scripts/build_nuitka.py --installer`
5. Create a GitHub Release with tag `v<version>`
6. Attach the installer EXE
7. Installer naming convention: `Aura-Setup-<version>.exe` (the self-updater detects matching versions from this pattern)

## Dependencies

| Package         | Use                                      |
|-----------------|------------------------------------------|
| PySide6         | Qt6 GUI framework                        |
| openai          | OpenAI API client                        |
| google-genai    | Google Gemini API client                 |
| beautifulsoup4  | Web scraping (research agent)            |
| httpx           | Async HTTP client                        |
| cryptography    | Fernet key encryption                    |
| platformdirs    | Config/data directory resolution          |
| Pillow          | Image handling (vision)                  |
| Pygments        | Syntax highlighting                      |
| mcp             | Model Context Protocol client            |
