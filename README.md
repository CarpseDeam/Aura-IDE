# Aura

[![Python](https://img.shields.io/badge/python-3.10+-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)]()
[![Version](https://img.shields.io/badge/version-1.3.3-orange)]()

<img src="media/AurA.ico" alt="Aura icon" width="64" height="64" align="right">

**Desktop AI Orchestration IDE - pair programming with full workspace awareness.**

## Why Aura?

Aura is for developers who want AI coding help without looking away.
- The Planner reads your code and writes a spec.
- You review or edit the spec.
- The Worker applies changes.
- Every file write shows a diff before it touches disk.
- Backups and git commits make experiments reversible.

<p align="center">
  <img src="media/plan_and_code.gif" alt="Aura planning and coding workflow demo" width="900">
</p>

<p align="center"><em>Demo: A full Planner -> Worker cycle - spec writing, dispatch, code editing with diff approval, and auto-commit.</em></p>

**Real-world cost:** 161 million tokens processed for $11.36 USD across 6,346 API requests (DeepSeek, May 2026).
<p align="center">
  <img src="media/token-cost.png" alt="DeepSeek API usage dashboard showing 161M tokens for $11.36" width="600">
</p>

If you find Aura useful, consider starring the repo to help others discover it.

---

## Table of Contents

- [Screenshots](#screenshots)
- [Features](#features)
  - [Planner / Worker Architecture](#planner--worker-architecture)
  - [Guided First-Launch Onboarding](#guided-first-launch-onboarding)
  - [Comprehensive Tools Suite](#comprehensive-tools-suite)
  - [Diff Approval & Backups](#diff-approval--backups)
  - [Git Integration](#git-integration)
  - [Web Research](#web-research)
  - [Terminal Commands](#terminal-commands)
  - [Vision Preprocessing](#vision-preprocessing)
  - [Dynamic / Self-Extending Tools](#dynamic--self-extending-tools)
  - [Sandbox Execution](#sandbox-execution)
  - [Hardware-Tethered API Key Encryption](#hardware-tethered-api-key-encryption)
  - [AST-Based Repo Map](#ast-based-repo-map)
  - [Codebase Index (BM25 Semantic Search)](#codebase-index-bm25-semantic-search)
  - [Persistent Codebase Index](#persistent-codebase-index)
  - [Session Cost Tracking](#session-cost-tracking)
  - [Thinking Modes](#thinking-modes)
  - [Custom System Prompts](#custom-system-prompts)
  - [Separate Worker Temperature](#separate-worker-temperature)
  - [Read-Only Mode](#read-only-mode)
  - [Focused Actions (Right-Click)](#focused-actions-right-click)
  - [Auto-Dispatch & Auto-Approve](#auto-dispatch--auto-approve)
  - [Conversation Persistence](#conversation-persistence)
  - [In-App File Viewer](#in-app-file-viewer)
  - [Keyboard Shortcuts & Slash Commands](#keyboard-shortcuts--slash-commands)
  - [Project Memory (SQLite+FTS5)](#project-memory-sqlitefts5)
  - [Self-Updater](#self-updater)
  - [Cross-Platform](#cross-platform)
- [Documentation](#documentation)
- [Supported Providers](#supported-providers)
- [CLI Agent Backends](#cli-agent-backends)
- [MCP Tool Integration](#mcp-tool-integration)
- [Installation](#installation)
- [First Launch Onboarding](#first-launch-onboarding)
- [Usage](#usage)
- [Configuration](#configuration)
- [Safety Model](#safety-model)
- [Known Limitations](#known-limitations)
- [Architecture](#architecture)
- [Project Structure](#project-structure)
- [Development](#development)
- [Dependencies](#dependencies)
- [License](#license)

---

## Screenshots

<p align="center">
  <img src="media/working.png" alt="Main interface" width="49%">
  <img src="media/diff-view.png" alt="Diff approval dialog" width="49%">
</p>

*Left: Main interface with three-pane layout - workspace tree, chat view, and worker activity panel. Right: Diff approval dialog - every file change is reviewed before being applied.*

> **Note:** Screenshots may reflect an earlier version of the UI. The current interface includes a floating terminal window and a guided onboarding wizard not shown above.

---

## Features

### Planner / Worker Architecture

Aura uses a **two-agent system** inspired by pair programming:

- **Planner** - Reads targeted parts of your codebase, asks clarifying questions when needed, gives a short plan summary, and writes a precise technical specification for the Worker.
- **Worker** - Executes the specification with read/write filesystem access. It reads target files, applies edits, runs validation commands, and reports back a summary.

Both agents can use **different models** and **different reasoning depths** from the same provider. For example, use a fast/cheap model for the Planner and a more capable model for the Worker.

The Planner is tuned for speed: it keeps visible planning brief and puts the important implementation detail into the `dispatch_to_worker` spec. This architecture acts as a **spec-as-token-firewall**: the Planner's output is a structured specification, not raw code edits, so the Worker starts from a clean, unambiguous target rather than inheriting the Planner's reasoning noise. The **Spec Edit dialog** lets you modify that spec before handing it to the Worker - giving you full control over what gets implemented and how.

### Guided First-Launch Onboarding

New users are greeted with a polished **5-step wizard** on first launch (Welcome → Workspace → Safety → Provider → First Mission):

- **Welcome** — Introduces the Planner → Worker workflow and Aura's core philosophy.
- **Workspace** — Confirms or changes the project folder where Aura will operate.
- **Safety** — Explains diff approval (default), Auto-Approve, Auto-Dispatch, and git checkpoints. Safety defaults are emphasised; advanced speed features are flagged as optional.
- **Provider** — Checks API key status and points users to Settings if no key is found.
- **First Mission** — Offers three selectable safe first prompts. The chosen prompt is placed in the chat input — the user decides when to send it.

The onboarding records progress via `onboarding_checklist` and `onboarding_version` in AppSettings, allowing future extension. Existing users who have already completed onboarding are not interrupted.

### Comprehensive Tools Suite

The AI has a rich set of tools - all sandboxed to your workspace root (the AI cannot escape the project directory). Tools are grouped by category:

#### Read Tools

| Tool | Description |
|------|-------------|
| `read_file` | Read a UTF-8 text file from the workspace (capped at 200 KB) |
| `list_directory` | List files and subdirectories (hides `.git`, `__pycache__`, `.venv`, `node_modules`) |
| `glob` | Recursively find files matching a glob pattern (capped at 200 results) |
| `read_file_outline` | Read a file's structural outline - class names, function signatures, imports - via AST (Python) or heuristics (other languages) |
| `grep_search` | Search file contents with string or regex matching |
| `find_usages` | Find all usages of a symbol across the workspace using word-boundary matching - safe for refactoring |
| `search_codebase` | **BM25 semantic search** - ranks entire files by relevance to a natural-language query. Uses a local inverted index over up to 1500 files (128 KB each, 30+ extensions). Perfect for rediscovering files/functions when conversation context has been pruned. |

#### Write Tools

| Tool | Description |
|------|-------------|
| `write_file` | Create or overwrite a file. Triggers diff approval and automatic backup. |
| `edit_file` | Surgically replace code via a **Search Block** (context + target code). Uses fuzzy matching - minor whitespace, indentation, or newline differences are tolerated. Triggers diff approval and backup. |
| `edit_symbol` | **AST-based structured editing** for Python files. Replace a named function, class, or method by specifying its name - finds the symbol in the parsed AST and replaces its entire definition. No whitespace-matching issues. Supports `function`, `class`, and `method` (with `class_name`). |

#### Git Tools

| Tool | Description |
|------|-------------|
| `git_status` | Show working tree status - current branch, remote tracking info, staged/unstaged/untracked files |
| `git_diff` | Show diff of unstaged or staged changes |
| `git_log` | Show recent commit history (with optional file filter) |
| `git_show` | Show the full diff and metadata for a specific commit |
| `git_log_file` | Show commit history for a single file, following renames |
| `git_branch_list` | List all local branches with tracking information |
| `git_stash_list` | List all stashes |
| `git_stash_show` | Show the diff of a specific stash |

#### Web Tools

| Tool | Description |
|------|-------------|
| `web_search` | Search the web via [Tavily](https://tavily.com/). Returns top results with snippets. |
| `web_fetch` | Fetch and parse the content of a specific URL using BeautifulSoup |
| `run_research` | Dispatches a **background sub-agent** that autonomously searches the web (Tavily) and scrapes pages (BeautifulSoup) to produce a synthesized report. Ideal for looking up documentation, debugging unfamiliar errors, or researching libraries. |

#### Terminal

| Tool | Description |
|------|-------------|
| `run_terminal_command` | Execute shell commands in your workspace with **real-time streaming output**, cancellation support, and configurable timeout. The AI is instructed to run linters, type checkers, and test suites after making changes. |

#### Worker Tools

| Tool | Description |
|------|-------------|
| `update_todo_list` | Maintains a **live progress tracker** with `pending` -> `active` -> `done` statuses. Displayed in the Worker Activity panel for real-time visibility. |

#### Dispatch

| Tool | Description |
|------|-------------|
| `dispatch_to_worker` | **Planner-only.** Hands off a spec to the Worker for execution. Only available when Planner/Worker mode is enabled. |

#### Circuit Breaker

The conversation loop includes a **circuit breaker** that detects when the same tool call produces the identical failure output **3 or more times consecutively**. A warning is injected into the tool result, alerting the AI that it is likely in a loop - preventing infinite retry cycles.

### Diff Approval & Backups

Every `write_file`, `edit_file`, or `edit_symbol` call triggers a **diff approval dialog** before any bytes touch disk:

- **Approve** - Apply this change
- **Reject** - Skip this change
- **Approve All** - Approve this and all subsequent writes in this turn
- **Reject All** - Reject this and all further writes

Before any write, existing files are **automatically backed up** to `.aura/backups/<ISO-timestamp>/<relative-path>` in your workspace. Every backup carries an ISO-8601 timestamp, so you can always recover previous versions.

### Git Integration

If your workspace is a git repository, Aura provides deep integration:

- **Auto-commit** - After the Worker completes a set of file changes, Aura stages and commits them with an **AI-generated commit message** derived from the dispatch goal and Worker summary.
- **`/undo` slash command** - Soft-resets `HEAD~1`, reverting the last commit while keeping changes in the working directory.
- **`git_init`** - Initialises a new git repository in the workspace if one doesn't exist.
- **Snapshot / Restore** - `snapshot()` creates a lightweight checkpoint commit; `restore_to_snapshot()` returns to it. Useful for checkpointing experimental changes.
- **Full git tool access** - Both the Planner and Worker can inspect repository state before and after changes using the complete git tool suite (status, diff, log, show, branch, stash).
- **Automatic `.gitignore`** - `.aura/` is automatically added to `.gitignore` on startup.

### Web Research

The `run_research` tool dispatches a **background sub-agent** that:

1. Generates search queries from your question
2. Searches the web via Tavily
3. Fetches and parses the most relevant pages with BeautifulSoup
4. Produces a synthesised report

Perfect for looking up documentation, debugging unfamiliar error messages, or researching third-party libraries without leaving your IDE.

### Terminal Commands

`run_terminal_command` executes shell commands in your workspace directory with:

- **Real-time streaming output** - See output as it's produced, not just when the command finishes
- **Cancellation support** - Stop long-running commands mid-execution
- **Timeout** - Configurable max execution time

Terminal output appears in a **floating, non-modal terminal window** opened from the **`$` edge tab** — it no longer consumes space in the main workspace or worker panel. The window can be moved, resized, or closed independently.

Unlike a regular terminal, the AI is instructed to run **linters, type checkers, and test suites** after making changes - closing the loop between edit and validation.

### Vision Preprocessing

Paste screenshots (`Ctrl+V`) or drag-and-drop images into the chat. The input panel handles both clipboard paste and file drag-and-drop. A local [Ollama](https://ollama.com/) vision model (`llama3.2-vision`) describes images in detail so the AI can reason about visual content - error dialogs, UI glitches, diagrams, and more.

Additionally, providers/models that **natively support vision** (GPT-4o, Claude, Gemini Flash) can receive images directly, bypassing local preprocessing.

### Sandbox Execution

Aura supports three execution modes for terminal commands and dynamic tools:

| Mode | Description |
|------|-------------|
| **host** | Run directly on the host (no isolation). Default. |
| **docker** | Run inside a Docker container with resource limits: 2 GB memory, 2 CPU cores, PID limit of 200, all Linux capabilities dropped, `no-new-privileges` enabled. Dynamic tools run with a **read-only root filesystem**; terminal commands run read-write. No access to the host filesystem outside the workspace. Network is enabled for terminal commands but disabled for dynamic tools. |
| **wasm** | Reserved for future WASM runtime. |

Configurable via the Settings dialog. Aura checks Docker availability at startup and falls back gracefully if not found.

### Hardware-Tethered API Key Encryption

API keys are **never** stored in `config.json`. Instead:

1. **Environment variables** take precedence - standard `DEEPSEEK_API_KEY`, `OPENAI_API_KEY`, etc.
2. **Encrypted storage** - Keys can be stored on disk at `~/.config/Aura/keys.json` encrypted with **Fernet (symmetric encryption)** using a machine-derived key (MAC address + username). File permissions are set to `0o600`.
3. **Auto-migration** - Legacy plaintext keys are automatically migrated to encrypted form on first access.

Keys stored via the Settings dialog (gear icon) are encrypted immediately. The Settings status indicator shows green when a key is found, red when missing.

### Codebase Index (BM25 Semantic Search)

The `search_codebase` tool builds and queries a local **BM25 inverted index** over your workspace files:

- **Up to 1,500 files**, each up to 128 KB
- **30+ file extensions** covered: Python, JavaScript, TypeScript, Rust, Go, Java, C/C++, Ruby, PHP, Swift, Kotlin, Scala, YAML, JSON, TOML, Markdown, HTML, CSS, SQL, Lua, Zig, and more
- Ranks files by **semantic relevance** to a natural-language query - not keyword matching

This is especially valuable when conversation context has been pruned and the AI needs to rediscover where a particular function or class lives.

### Session Cost Tracking

The status bar displays live token usage and estimated cost:

- **Cache hit tokens** / **Cache miss tokens** / **Output tokens**
- **Estimated USD cost** using embedded pricing tables per model
- Resets per conversation session

Pricing is tracked per-model using rates in `aura/config.py` - both for built-in models and dynamically fetched ones (especially via OpenRouter, which returns real-time pricing).

### Thinking Modes

Choose reasoning depth for each agent independently:

| Mode | Description |
|------|-------------|
| **Off** | Standard response - no extended reasoning |
| **High** | Extended reasoning - the model spends more compute before responding |
| **Max** | Maximum reasoning - best for complex architectural decisions or tricky bugs |

Configure separately for **Planner**, **Worker**, and **Single** (non-Planner/Worker) modes. Works with models that support extended thinking (e.g., DeepSeek R1, Claude Sonnet).

Defaults are intentionally split for responsiveness and quality:

- **Planner Thinking** defaults to **Off** so planning and spec creation feel snappy.
- **Worker Thinking** defaults to **High** so implementation work gets more reasoning budget.

### Custom System Prompts

Configure separate system prompts via the Settings dialog:

- **System Prompt** - Used in Single mode (default conversation)
- **Planner System Prompt** - Used for the Planner agent
- **Worker System Prompt** - Used for the Worker agent

Tailor each agent's behaviour, style, constraints, and persona to your workflow.

### Separate Worker Temperature

- **Worker temperature**: defaults to `0.1` - deterministic, consistent when applying code changes
- **Planner / Single temperature**: defaults to `0.7` - creative when reasoning about architecture

Both are configurable in the Settings dialog (range 0.0-2.0).

### Read-Only Mode

Toggle the **Read-Only** button in the toolbar to strip all write tools. The AI can still read, search, and advise, but cannot modify any files. Perfect for:

- Code review sessions
- Exploring an unfamiliar codebase
- Asking questions without risk of unintended modifications

### Auto-Dispatch & Auto-Approve

Optional settings for faster workflows:

- **Auto-Dispatch** - Skips the spec review dialog; the spec is dispatched to the Worker immediately after the Planner writes it.
- **Auto-Approve** - Skips the diff approval dialog; all file writes are applied automatically.

Toggle these from the toolbar or Settings dialog. In the toolbar, a blue/bold label means the toggle is enabled; a dim label means it is disabled. Use with caution - these are best for trusted, low-risk changes.

### Conversation Persistence

- Chats are saved to `.aura/conversations/` as JSON files
- **Restore last session** on launch (configurable)
- **Open past conversations** from the toolbar
- **Start fresh** at any time via the "New Conversation" button

### Keyboard Shortcuts & Slash Commands

| Shortcut | Action |
|----------|--------|
| **Ctrl+Enter** | Send message |
| **Ctrl+V** (in editor) | Paste image from clipboard |

| Command | Description |
|---------|-------------|
| `/undo` | Soft-resets the last git commit (requires git repo). Quickly revert the AI's last change. |

### Cross-Platform

Aura runs on **Windows**, **macOS**, and **Linux** via PySide6 (Qt for Python). The same interface, the same features, everywhere.

### Focused Actions (Right-Click)

Right-click any selected code in the in-app file viewer to trigger context-aware AI actions:

| Action | Description |
|--------|-------------|
| **Ask Aura** | Ask a custom question about the selected code |
| **Explain** | Get a detailed explanation of the selected code |
| **Fix** | Fix bugs or issues in the selected region |
| **Refactor** | Restructure the selected code for clarity |
| **Simplify** | Reduce complexity while preserving behaviour |
| **Add Logging** | Insert logging statements strategically |
| **Add Type Hints** | Annotate the selection with type hints |
| **Write Tests** | Generate tests for the selected code |

Each action builds a precise prompt with the file path, line numbers, selected code, surrounding context, and action-specific instructions. Edit actions (Fix, Refactor, Simplify, Add Logging, Add Type Hints, Write Tests) instruct the AI to use the minimal tool for the job — `edit_symbol` for whole functions, `edit_file` for smaller regions. Read-only actions (Ask, Explain) prevent any file modification.

### In-App File Viewer

A tabbed code viewer with syntax highlighting (Pygments) lets you inspect files directly inside Aura:

- **Open files** by double-clicking in the workspace tree, or when the Worker streams file contents during edits
- **Text selection** with line numbers tracked — select code and right-click for focused actions
- **Worker streaming** — when the Worker reads or writes files, content appears in a live tab with typing animation
- **Syntax highlighting** for Python, JavaScript, TypeScript, CSS, JSON, YAML, Rust, Go, Java, C/C++, and more
- **Read-only mode aware** — viewer tabs respect the global read-only toggle

### AST-Based Repo Map

Every system prompt includes a structural overview of your workspace generated from AST parsing:

- **Python files** — class names, base classes, method signatures, and top-level function signatures extracted via `ast`
- **TypeScript/JavaScript files** — file paths listed (AST outline support planned)
- **Mtime-based cache** — the repo map is regenerated only when a file changes; otherwise reused across turns
- **>95% prompt cache hit rate** — because the map is deterministic and stable, provider-side prompt caching sees minimal churn
- **Capped at 300 lines** — keeps the system prompt tight while giving the model a bird's-eye view of your codebase

### Persistent Codebase Index

The BM25 codebase index persists across restarts and updates incrementally:

- **On-disk cache** — the index is saved to `.aura/codebase_index/` and restored instantly on launch
- **Change detection** — only modified files are re-indexed; unchanged files skip processing
- **Same scope** — up to 1,500 files, 128 KB each, 30+ extensions

### Project Memory (SQLite+FTS5)

Aura stores a searchable history of past tasks and saved notes in a local SQLite database with full-text search:

- **FTS5 indexing** — fast full-text search across all stored memory entries
- **Auto-record** — completed Worker dispatches are recorded as project memories
- **Manual save** — save notes, architecture decisions, or debugging insights for later retrieval
- **Searchable** — both the AI (via `search_project_memory` tool) and the user (via the Info Hub panel) can query past memories
- **Per-workspace** — each workspace has its own `.aura/memory.db` database

### Self-Updater

Aura can update itself automatically:

- **Packaged Windows Builds** — Aura checks GitHub Releases on startup. If a newer version is available, the "Update" button in the toolbar is highlighted. One click downloads the latest release ZIP and installs it via an external updater script.
- **Source Installations** — If running from a git checkout, Aura can pull the latest changes directly from the repository using a fast-forward merge.
- **Update status check** — The toolbar shows when a newer version is available.
- **Safety** — The updater never touches your workspaces, `.aura` project folders, or user configuration.

---

## Supported Providers

Aura supports five AI providers. You choose one per session via the toolbar dropdown, then select any model from that provider's catalogue. The Planner and Worker always use the same provider but can be assigned different models and thinking modes.

| Provider | Base URL | Env Var |
|----------|----------|---------|
| **DeepSeek** | `https://api.deepseek.com` | `DEEPSEEK_API_KEY` |
| **OpenAI** | `https://api.openai.com/v1` | `OPENAI_API_KEY` |
| **Google Gemini** | `https://generativelanguage.googleapis.com/v1beta/openai/` | `GEMINI_API_KEY` |
| **Anthropic** | `https://api.anthropic.com/v1` | `ANTHROPIC_API_KEY` |
| **OpenRouter** | `https://openrouter.ai/api/v1` | `OPENROUTER_API_KEY` |

### Dynamic Model Fetching

Aura can **dynamically fetch models** from provider APIs:

- **OpenRouter** - Returns the full model catalogue with real-time pricing per model. Models are automatically added to the selection dropdown with up-to-date pricing.
- **DeepSeek, OpenAI, Google** - Uses the OpenAI-compatible `/models` endpoint. Pricing for recognised models is drawn from the embedded pricing tables; unknown models default to $0.

Fetched models are **cached to disk** (`~/.config/Aura/models_cache.json`) and reloaded on startup, so you don't need to fetch every launch.

> **Tip:** Model availability and pricing change frequently. Run a fetch (via the provider dropdown menu) to refresh the catalogue. For the latest pricing, also check each provider's official documentation.

---

## CLI Agent Backends

In addition to API-based providers, Aura supports CLI-based AI tools as drop-in agent backends. These run the official CLI tools as subprocesses — authentication is handled by the CLI's own OAuth flow.

| Backend | CLI Command | Authentication | Description |
|---------|------------|----------------|-------------|
| **Gemini CLI** | `gemini` | `gemini auth login` | Google Gemini via the official Gemini CLI (npm) |
| **Claude Code** | `claude -p` | `claude auth login` | Anthropic Claude via Claude Code CLI |
| **Codex CLI** | `codex exec` | `codex login` | OpenAI Codex via the Codex CLI |

CLI backends are **pluggable** — each backend is a self-contained module under `aura/backends/`. Adding a new CLI agent requires only implementing the backend interface.

CLI backends are selected independently for the Planner and Worker via the **Agent Backends** dropdowns in the left sidebar. Authentication status is managed in the Settings dialog under **Agent Backends** — each backend shows its auth state and provides a one-click **Login** button.

> **Tip:** CLI backends are ideal when you already have the official CLI tools installed and authenticated, and want to use them without managing separate API keys in Aura.

---

## MCP Tool Integration

Aura supports the **Model Context Protocol (MCP)** for extending its tool suite with third-party tools. You can connect any MCP-compatible stdio server and its tools become available to the AI agents alongside Aura's built-in tools.

### How It Works

1. The MCP server is launched as a subprocess via a command you specify (e.g., `python -m my_mcp_server`).
2. Aura connects over stdio, initialises an MCP session, and fetches the server's tool list.
3. Each tool's schema is converted to OpenAI function-calling format and merged into the agent's available tools.
4. When the AI invokes an MCP tool, Aura forwards the call to the MCP server and returns the response.

### Supported Tool Features

- **Tool discovery** — All tools exposed by the MCP server are automatically registered.
- **Tool execution** — Arguments are forwarded; results are returned to the AI.
- **Error handling** — Unknown tools return errors; connectivity issues are surfaced gracefully.
- **Multiple servers** — Multiple MCP servers can be connected simultaneously.

MCP tools are available in all conversation modes (Planner, Worker, Single, and Read-Only).

> **Note:** MCP servers can be connected programmatically via `ToolRegistry.connect_mcp_server()`. A GUI for managing MCP server connections is planned for a future release.

---

## Installation

### Prerequisites

- **Python 3.10** or later
- An API key for at least one supported provider (see [API Key Setup](#api-key-setup))
- (Optional) [Ollama](https://ollama.com/) running locally with `llama3.2-vision` for screenshot preprocessing
- (Optional) [Git](https://git-scm.com/) for auto-commit and `/undo` support
- (Optional) [Docker](https://docker.com/) for sandboxed execution mode

### Install From Source

```bash
pip install .
```

For development:

```bash
pip install -e .[dev]
```

For packaged releases, use the installer or archive attached to the GitHub release. If you publish to PyPI later, replace this section with the final package name.

### API Key Setup

Aura never stores API keys in its config file. Keys are read from **environment variables** (precedence) or stored encrypted on disk via the Settings dialog.

**Set environment variables:**

```bash
export DEEPSEEK_API_KEY="sk-..."
export OPENAI_API_KEY="sk-..."
export ANTHROPIC_API_KEY="sk-ant-..."
export GEMINI_API_KEY="..."
export OPENROUTER_API_KEY="sk-or-..."
export TAVILY_API_KEY="..."  # Required for web_search and run_research
```

On Windows, set these via **System Properties -> Environment Variables**.

Alternatively, open the **Settings** dialog (gear icon) and enter your keys there - they will be encrypted to disk using a hardware-derived key.

### Launch

```bash
aura
```

Or:

```bash
python -m aura
```

---

## First Launch Onboarding

On first launch, Aura presents a **5-step onboarding wizard** that walks you through:

1. **Welcome** — An introduction to Aura's Planner / Worker architecture.
2. **Workspace** — Choose and confirm your project directory.
3. **Safety & Control** — Understand diff approval (on by default), Auto-Approve (off by default), Auto-Dispatch (off by default), and git checkpoints.
4. **AI Provider Setup** — Verify your API key is configured. If not, you can open Settings from the wizard to add one.
5. **Your First Mission** — Pick from three safe starter prompts. The selected prompt is placed in the chat input — you choose when to send it.

After completing the wizard, the onboarding is not shown again. You can always change any setting later via the gear icon in the toolbar.

If you skipped onboarding or want to revisit concepts:
- Add API keys via **Settings** (gear icon) or environment variables (see [API Key Setup](#api-key-setup)).
- Keep **Auto-Approve** off until you trust the workflow on that project.
- For git-backed projects, check `git status` before and after a Worker run so you can review changes cleanly.

---

## Usage

### Basic Workflow

1. Launch Aura and select your project folder as the workspace root (or it defaults to the current directory).
2. Type a question or request in the input panel - describe a bug, ask for an explanation, or request a change.
3. The **Planner** reads relevant files, asks clarifying questions if needed, then writes a short plan summary and calls `dispatch_to_worker` with a complete worker spec.
4. A **Spec Card** appears in the chat. Review it (you can edit the spec if needed), then click **Dispatch**.
5. The **Worker** runs, reads target files, and proposes edits. Each write triggers a diff dialog for your approval.
6. When the Worker finishes, it reports a summary back to the Planner, and the conversation continues.
7. **(Optional)** Auto-commit creates a git commit with an AI-generated message summarising the change.

### Keyboard Shortcuts

| Shortcut | Action |
|----------|--------|
| **Ctrl+Enter** | Send message |
| **Ctrl+V** (in editor) | Paste image from clipboard |

### Slash Commands

| Command | Description |
|---------|-------------|
| `/undo` | Soft-resets the last git commit (requires git repo). Quickly revert the AI's last change. |

### Model, Thinking & Provider Selection

Use the dropdowns in the input panel / sidebar to configure:

| Control | Description |
|---------|-------------|
| **Provider** | DeepSeek, OpenAI, Anthropic, Google Gemini, or OpenRouter |
| **Planner Model** | Model that reads code and writes specs |
| **Planner Thinking** | Reasoning depth for the Planner (Off / High / Max) |
| **Worker Model** | Model that executes file edits |
| **Worker Thinking** | Reasoning depth for the Worker (Off / High / Max) |

The Planner and Worker always use the same provider but can be assigned different models and thinking modes from that provider's catalogue.

### Attachments

- **Paste images** (`Ctrl+V`) - screenshots of errors, UI, or diagrams. Images are sent through vision preprocessing (local Ollama model or directly to vision-capable providers).
- **Drag-and-drop files** - images get base64-encoded and described by the vision model; other files are attached as path references for the AI to read.

---

## Configuration

Settings are stored at `~/.config/Aura/config.json` (or the platform-appropriate equivalent via [platformdirs](https://pypi.org/project/platformdirs/)). Open the **Settings** dialog via the toolbar gear icon to configure:

The Settings dialog is scrollable and includes provider keys, Tavily web-search keys, model selection, thinking modes, automation toggles, sandbox mode, custom prompts, and workspace information.

| Setting | Description |
|---------|-------------|
| **Provider** | Select the AI provider (DeepSeek / OpenAI / Anthropic / Google Gemini / OpenRouter) |
| **Default Model** | Model used in Single mode (non-Planner/Worker) |
| **Default Thinking** | Reasoning depth for Single mode |
| **Restore Last Conversation** | Automatically reload the previous session on launch |
| **Planner/Worker Mode** | Toggle the two-agent architecture on or off |
| **Planner Model** | Model assigned to the Planner |
| **Worker Model** | Model assigned to the Worker |
| **Planner Thinking** | Reasoning depth for the Planner. Default: Off |
| **Worker Thinking** | Reasoning depth for the Worker. Default: High |
| **Temperature** | Sampling temperature (0.0-2.0) for Single/Planner mode. Default: 0.7 |
| **Worker Temperature** | Sampling temperature (0.0-2.0) for the Worker. Default: 0.1 |
| **System Prompt** | Custom system prompt for Single mode |
| **Planner System Prompt** | Custom system prompt for the Planner agent |
| **Worker System Prompt** | Custom system prompt for the Worker agent |
| **Vision Enabled** | Toggle screenshot preprocessing via Ollama |
| **Vision Model** | Ollama model name (default: `llama3.2-vision`) |
| **Vision Endpoint** | Ollama API endpoint (default: `http://localhost:11434/v1`) |
| **Auto-Commit** | Automatically create a git commit after Worker completes changes |
| **Auto-Dispatch** | Skip the spec review dialog - dispatch to Worker immediately |
| **Auto-Approve** | Skip the diff approval dialog - apply all file writes automatically |
| **Sandbox Mode** | Execution mode: `host` (direct), `docker` (containerised), or `wasm` (reserved) |

> **Security note:** API keys are **never** written to `config.json`. They are either read from environment variables or stored encrypted in a separate `keys.json` file with `0o600` permissions.

---

## Safety Model

Aura is designed to keep AI-driven changes reviewable:

- File tools are scoped to the selected workspace root.
- Write tools require diff approval unless **Auto-Approve** is enabled.
- Existing files are backed up under `.aura/backups/` before writes.
- Conversations are saved under `.aura/conversations/`.
- API keys are stored separately from `config.json` and encrypted with a machine-derived key when saved through Settings.
- Terminal commands run in either **host** mode or **docker** mode, depending on your sandbox setting.
- **Read-Only Mode** removes write tools so Aura can inspect and explain code without changing files.

Review generated changes like you would review a teammate's pull request. Keep **Auto-Approve** off for unfamiliar repositories, large refactors, and high-risk changes.

---

## Known Limitations

- AI-generated edits still need human review.
- **Auto-Approve** can apply incorrect edits without showing the diff dialog.
- Docker sandbox mode requires Docker to be installed and running.
- Screenshot preprocessing requires local Ollama unless the selected provider/model supports native vision.
- Web search and research require a Tavily key.
- Provider model availability, pricing, and thinking-mode support vary and can change over time.
- Very large workspaces may require targeted prompts or `search_codebase` to keep planning fast.
- MCP server connections are currently managed programmatically; a GUI for browsing and connecting to MCP servers is planned.

---

## Architecture

Aura uses a decoupled architecture with Qt signals/slots bridging synchronous AI conversation logic to the async GUI:

```
┌──────────────┐     ┌──────────────┐     ┌──────────────────┐
│   GUI Layer  │ ←→  │ Bridge Layer │ ←→  │ Conversation     │
│  (PySide6)   │     │ (QThread)    │     │ Layer (sync)     │
│              │     │              │     │                  │
│ MainWindow   │     │ ConvBridge   │     │ ConvManager      │
│ ChatView     │     │ _Worker      │     │ History          │
│ InputPanel   │     │ _ApproveProxy│     │ ToolRegistry     │
│ WorkspaceTree│     │ _DispatchProxy│    │ Persistence      │
│ WorkerWindow │     │              │     │                  │
└──────────────┘     └──────────────┘     └──────────────────┘
```

- **GUI Layer** - PySide6 widgets: main window, chat transcript, input composer, workspace tree, diff dialogs, settings, and the worker activity panel.
- **Bridge Layer** - Runs the synchronous conversation loop on a background `QThread`. Proxies tool approvals and dispatch decisions back to the GUI via signals/slots so the UI never blocks.
- **Conversation Layer** - Pure Python, synchronous: manages message history, the tool-calling loop, tool execution via the `ToolRegistry`, and conversation persistence.

---

## Project Structure

```
aura/
├── __init__.py              # Package version (1.0.0)
├── __main__.py              # Entry point: `aura` or `python -m aura`
├── config.py                # Settings, provider registry, pricing, paths
├── focused_actions.py       # Right-click context menu prompt builders
├── git_ops.py               # Auto-commit, /undo, snapshot/restore, git_init
├── key_manager.py           # Hardware-tethered Fernet key encryption
├── memory_db.py             # SQLite+FTS5 project memory storage
├── paths.py                 # Cross-platform config/data directory helpers
├── prompts.py               # Default system prompt templates
├── repo_map.py              # AST-based workspace structure map
├── resources.py             # Resource path resolution (media, icons)
├── mcp_client.py            # MCP stdio client wrapper (connect, list, call)
├── models.py                # Pydantic model definitions
├── sandbox.py               # SandboxExecutor: host, docker, wasm modes
├── updater.py               # Self-updater (git fast-forward pull)
├── vision.py                # Ollama vision client for screenshot preprocessing
├── bridge/                  # Qt thread bridge
│   ├── __init__.py
│   └── qt_bridge.py         # ConversationBridge, _Worker, _ApproveProxy, _DispatchProxy
├── client/                  # AI provider client
│   ├── __init__.py
│   ├── deepseek.py          # OpenAI-compatible client (all providers)
│   └── events.py            # Streaming event types
├── codebase_index/          # BM25 semantic search index
│   ├── __init__.py
│   ├── bm25.py              # BM25Scorer: tokenizer, inverted index, scoring
│   ├── indexer.py           # CodebaseIndex: build/update/search over workspace files
│   └── tool.py              # search_codebase tool definition
├── conversation/            # Synchronous conversation logic
│   ├── __init__.py
│   ├── manager.py           # ConversationManager (tool-calling loop)
│   ├── history.py           # Message history
│   ├── dispatch.py          # WorkerDispatchRequest / WorkerDispatchResult
│   ├── persistence.py       # Save/load conversations (JSON)
│   └── tools/               # Tool implementations
│       ├── __init__.py
│       ├── registry.py      # ToolRegistry - registers all tools, handles execution
│       ├── backup.py        # Timestamped backups before writes
│       ├── dynamic.py       # Dynamic tool schema parsing and execution
│       ├── find_usages.py   # Symbol-aware search (word-boundary matching)
│       ├── fs_edit_structured.py  # edit_symbol - AST-based Python symbol replacement
│       ├── fs_read.py       # read_file, list_directory, glob, read_file_outline
│       ├── fs_write.py      # write_file, edit_file (Search Block with fuzzy matching)
│       ├── git_tools.py     # git_status, git_diff, git_log, git_show, git_log_file,
│       │                    # git_branch_list, git_stash_list, git_stash_show
│       ├── grep.py          # grep_search (regex/string search)
│       └── web.py           # web_search (Tavily), web_fetch (BeautifulSoup)
└── gui/                     # PySide6 UI components
    ├── __init__.py
    ├── main_window.py       # MainWindow, toolbar, status bar, model/thinking combos
    ├── chat_view.py         # Chat transcript with card-based rendering
    ├── code_editor_pane.py  # Tabbed in-app file viewer with syntax highlighting
    ├── input_panel.py       # Message composer, attachments, Ctrl+V paste
    ├── workspace_tree.py    # File tree browser (left pane)
    ├── onboarding_dialog.py # First-launch onboarding wizard
    ├── settings_dialog.py   # Settings editor
    ├── spec_edit_dialog.py  # Spec editor before dispatch
    ├── diff_dialog.py       # Diff approval modal (Approve/Reject/Approve All/Reject All)
    ├── theme.py             # Dark theme constants
    ├── update_dialog.py     # Self-updater dialog
    ├── aura_widget.py       # Animated "Aura" dots and GlassSwitch toggle
    ├── controllers.py       # ToolStreamController for streaming tool results
    ├── markdown_renderer.py # Markdown rendering in chat
    ├── syntax.py            # Syntax highlighting (Pygments integration)
    └── cards/               # Chat message card widgets
        ├── __init__.py
        ├── _collapsible.py
        ├── _helpers.py
        ├── _stream_label.py
        ├── assistant_card.py
        ├── code_block_card.py
        ├── code_writer_card.py
        ├── diff_card.py
        ├── error_card.py
        ├── spec_card.py
        ├── terminal_card.py
        ├── tool_call_card.py
        └── user_card.py
```

---

## Development

### Dev Install

```bash
git clone <repo-url>
cd aura
pip install -e .[dev]
```

### Smoke Tests

The `scripts/` directory contains smoke tests that exercise individual subsystems. Most require `DEEPSEEK_API_KEY` to be set.

| Script | What It Tests |
|--------|---------------|
| `smoke_client.py` | DeepSeek API client connectivity and streaming |
| `smoke_conversation.py` | Full conversation loop with tool calls |
| `smoke_gui.py` | GUI launch and basic widget initialisation |
| `smoke_history.py` | Message history management |
| `smoke_planner_worker.py` | Planner -> Worker dispatch flow |
| `smoke_tools.py` | Tool registry and individual tool execution |
| `smoke_vision.py` | Vision preprocessing with Ollama |
| `smoke_research.py` | Web research sub-agent |

Run a smoke test:

```bash
python scripts/smoke_client.py
```

### Build Options

```bash
python scripts/build_exe.py     # Build a standalone executable (PyInstaller)
python scripts/build_nuitka.py  # Build with Nuitka (faster, smaller)
```

### Release Process

To publish a Windows release, bump `aura/version.py`, create a GitHub Release tagged `vX.Y.Z`, and the release workflow will attach `Aura-Windows-x64.zip` automatically.

### Requirements

- Python 3.10+
- A DeepSeek API key for most smoke tests
- Ollama with `llama3.2-vision` for `smoke_vision.py`

---

## Dependencies

| Package | Purpose |
|---------|---------|
| [PySide6](https://pypi.org/project/PySide6/) | Qt for Python GUI framework |
| [openai](https://pypi.org/project/openai/) | AI provider client (OpenAI-compatible) |
| [beautifulsoup4](https://pypi.org/project/beautifulsoup4/) | HTML parsing for web research |
| [cryptography](https://pypi.org/project/cryptography/) | Fernet encryption for hardware-tethered key storage |
| [platformdirs](https://pypi.org/project/platformdirs/) | Cross-platform config/data directory resolution |
| [Pillow](https://pypi.org/project/Pillow/) | Image handling for pasted screenshots |
| [Pygments](https://pypi.org/project/Pygments/) | Syntax highlighting in diff dialogs and code blocks |
| [httpx](https://pypi.org/project/httpx/) | HTTP client for web research and tool execution |
| [mcp](https://pypi.org/project/mcp/) | Model Context Protocol client for extending tools via MCP servers |

---

## License

Aura is released under the [MIT License](LICENSE).

The application icon is located at [`media/AurA.ico`](media/AurA.ico).


## Documentation

- [Aura Blog](https://aura-ide.hashnode.dev/) — project updates, design deep-dives, and usage guides
- [Planner/Worker: A Better Architecture for AI Code Generation](https://aura-ide.hashnode.dev/planner-worker-a-better-architecture-for-ai-code-generation) — why the dual-agent model is more token-efficient than single-agent approaches
- [Token-Efficient Memory: How Aura Caches BM25, Repo Maps, and Long-Term Context](https://aura-ide.hashnode.dev/token-efficient-memory-how-aura-caches-bm25-repo-maps-and-long-term-context) — a deep dive into the three memory layers that keep context fresh without burning tokens