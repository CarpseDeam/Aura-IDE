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

1. **Welcome** — What Aura's single production agent does.
2. **Workspace** — Select a project folder. Aura indexes it for search and repo mapping.
3. **Safety** — Diff approval is on by default. Auto-Approve is off.
4. **Provider** — Choose a supported BYOK provider and configure its API key.
5. **First Mission** — Choose a safe starter prompt (explain the project, suggest improvements, or write a README).

## Basic Workflow

1. Open a project folder (File → Open Workspace or drag a folder onto the window).
2. Type a request in the input panel: "Add error handling to the database module" or "Explain how the authentication flow works."
3. The production agent reads the workspace and owns the task end to end.
4. Each file change shows a **diff** — approve, reject, approve all, or reject all.
5. Tool activity, TODO progress, terminal output, and validation stay visible during the run.
6. When done, Aura reports a factual receipt. Changes are auto-committed with an AI-generated message.

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
