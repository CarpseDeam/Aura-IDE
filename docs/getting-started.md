# Getting Started

## Prerequisites

- Python 3.10+ (only needed for pip install — the Windows installer bundles it)
- An API key from at least one provider (DeepSeek recommended for cheapest trial)
- Optional: Git, Docker, Ollama (for vision)

## Install

**Windows installer** — Download the latest `.exe` from GitHub Releases. Per-user install, no admin rights needed. Run it, follow the prompts, Aura appears in your Start menu.

**From source:**

```bash
pip install .
# Or editable install with dev extras:
pip install -e .[dev]
```

## API Key Setup

Aura supports multiple providers. Set your key via environment variable or the Settings dialog (encrypted to disk).

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

You can also set keys through Settings → Provider, which encrypts them to `~/.config/Aura/keys.json`.

## First Launch

Run `aura` or `python -m aura`. The onboarding wizard walks you through 5 steps:

1. **Welcome** — What Aura is and how Planner/Worker work.
2. **Workspace** — Select a project folder. Aura indexes it for search and repo mapping.
3. **Safety** — Diff approval is on by default. Auto-Approve and Auto-Dispatch are off.
4. **Provider** — Set your API key or confirm it's already configured.
5. **First Mission** — Choose a safe starter prompt (explain the project, suggest improvements, or write a README).

## Basic Workflow

1. Open a project folder (File → Open Workspace or drag a folder onto the window).
2. Type a request in the input panel: "Add error handling to the database module" or "Explain how the authentication flow works."
3. The **Planner** reads your code and writes a technical spec. You see it in the spec card.
4. Review the spec. Edit it if needed using the Spec Edit dialog.
5. Click **Dispatch** or press **Ctrl+Enter**. The **Worker** executes the spec.
6. Each file change shows a **diff** — approve, reject, approve all, or reject all.
7. When done, the Worker reports back with a receipt. Changes are auto-committed with an AI-generated message.

## Keyboard Shortcuts

| Shortcut       | Action                          |
|----------------|---------------------------------|
| Ctrl+Enter     | Send / Dispatch                 |
| Ctrl+Shift+A   | Ask about current selection     |
| Ctrl+V in input | Paste image (attached as screenshot) |

## Slash Commands

Type these in the input panel:

| Command  | Action                                                    |
|----------|-----------------------------------------------------------|
| `/undo`  | Soft-reset the last commit / restore pre-worker snapshot   |
| `/help`  | Show available commands                                   |
