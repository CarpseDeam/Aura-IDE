# Getting Started

## Prerequisites

- Python 3.10+ (only needed for pip install — the Windows installer bundles it)
- An API key from at least one provider
- Optional: Git, Docker

## Install

**Windows installer** — Download the latest `.exe` from GitHub Releases. Per-user install, no admin rights needed. Run it, follow the prompts, Aura appears in your Start menu.

**From source:**

```bash
pip install .
# Or editable install with dev extras:
pip install -e .[dev]
```

## Provider Setup

Connect one of the supported providers below. Set your key via environment variable or the Settings → API Keys dialog (encrypted to disk).

| Provider     | Environment Variable      | Default Model         |
|--------------|---------------------------|-----------------------|
| DeepSeek     | `DEEPSEEK_API_KEY`        | deepseek-v4-flash     |
| OpenAI       | `OPENAI_API_KEY`          | gpt-5.4-mini          |
| Anthropic    | `ANTHROPIC_API_KEY`       | claude-sonnet-4-6     |
| Gemini       | `GEMINI_API_KEY`          | gemini-2.5-flash      |
| OpenRouter   | `OPENROUTER_API_KEY`      | deepseek/deepseek-v4-flash |

Example:

```bash
export DEEPSEEK_API_KEY="sk-..."
aura
```

You can also set keys through Settings → API Keys, which encrypts them to `~/.config/Aura/keys.json`.

## First Launch

Run `aura` or `python -m aura`. The onboarding wizard walks you through 5 steps:

1. **Welcome** — How Aura owns a coding turn and keeps the work visible.
2. **Workspace** — Select a project folder. Aura indexes it for search and repo mapping.
3. **Safety** — Diff approval is on by default. Auto-Approve is off.
4. **Provider** — Choose a supported BYOK provider and configure its API key.
5. **First Mission** — Choose a safe starter prompt (explain the project, suggest improvements, or write a README).

## Basic Workflow

1. Open a project folder (File → Open Workspace or drag a folder onto the window).
2. Type a request in the input panel: "Add error handling to the database module" or "Explain how the authentication flow works."
3. Aura reads the workspace and owns the root conversation. With the Agents toggle enabled, it can assemble a team or choose a runnable saved Workflow; it can also complete the task directly.
4. Each file change shows a **diff** — approve, reject, approve all, or reject all.
5. Tool activity, TODO progress, terminal output, and validation stay visible during the run.
6. When done, Aura reports a factual receipt. Changes are auto-committed with an AI-generated message.

## Create a reusable Workflow in chat

1. Ask: **“Create a reusable Workflow that implements a requested change, tests it, then reviews it.”** Aura chooses a name, inherits the current model by default, and saves the Workflow without running it. You can also start from **Ask Aura to create a Workflow** in the Agents window.
2. Check the saved graph card. **Details** shows each placement's assignment, model, and permission. Solid arrows are handoffs; dashed arrows are optional Sub-agents.
3. Refine it: **“Add a security review before the final result.”** Aura updates the same Workflow. **Undo** reverses its latest edit; chat and canvas share the same session undo history. An outdated action asks Aura to inspect the latest revision first.
4. Click **Run**, then enter this run's task, such as **“Add password reset.”** A new root turn receives that exact saved Workflow. Run it again with a different task to reuse the process.
5. **Open Workflow** opens the same saved graph on the canvas. The saved Workflow survives restarting Aura; chat previews do not.

Setup works with the Agents toggle off. The toggle enables automatic team use in ordinary chat; choosing a graph in the editor does not select it for chat. The card's explicit Run works independently of that toggle. Read Only prevents authoring changes, Plan Review remains enforced, and writable Agent work still uses isolated worktrees with explicit application.

## Keyboard Shortcuts

| Shortcut       | Action                          |
|----------------|---------------------------------|
| Ctrl+Enter     | Send (or queue during a run)    |
| Ctrl+Shift+A   | Ask about current selection     |
| Ctrl+V in input | Paste image (attached as screenshot) |

## Slash Commands

Type these in the input panel:

| Command  | Action                                                    |
|----------|-----------------------------------------------------------|
| `/undo`  | Soft-reset the last commit / restore the pre-run snapshot  |
| `/help`  | Show available commands                                   |
