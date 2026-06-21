# Aura

[![Python](https://img.shields.io/badge/python-3.10+-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)]()
[![Version](https://img.shields.io/badge/version-1.8.01-orange)]()

<p>
  <a href="https://www.producthunt.com/products/aura-ide?embed=true&utm_source=badge-featured&utm_medium=badge&utm_campaign=badge-aura-ide" target="_blank" rel="noopener noreferrer">
    <img alt="Aura IDE - Open source AI coding harness you control | Product Hunt" width="150" height="54" src="https://api.producthunt.com/widgets/embed-image/v1/featured.svg?post_id=1162818&theme=light&t=1780584703498">
  </a>
</p>

**Aura is a local-first AI coding workspace that turns prompts into reviewed, validated code changes.**

Aura gives AI a real workbench — not just a chat box. It runs a Planner/Worker loop: the Planner reads your repo and writes a structured technical spec; the Worker executes the spec through controlled file tools, proposes every change as a diff for your approval, runs validation, and commits the result. The architecture is model-agnostic: use DeepSeek, OpenAI, Anthropic, Gemini, or OpenRouter through your own keys, or use Aura Credits for hosted models without key management. Reusable Drones, Workbay automation, AST repo maps, BM25 codebase search, git integration, and a mobile companion ship with the desktop app.

## See it in action

<p align="center">
  <img src="media/plan_and_code.gif" alt="Aura planning and coding workflow" width="900">
</p>
<p align="center"><em>Plan, dispatch, edit, review diffs, validate, and commit from one desktop workflow.</em></p>

## What Aura does

The workflow is a tight loop you stay in control of:

1. **Prompt** — Describe the change you want, in your own words.
2. **Planner spec** — The Planner reads your workspace (AST repo map, BM25 index, dependency graph) and writes a structured technical spec. You review and edit it before anything runs.
3. **Dispatch** — When the spec looks right, you dispatch it.
4. **Worker edits** — The Worker reads the spec and makes the changes through controlled file tools. It can read, write, edit, and search your codebase.
5. **Diff approval** — Every proposed write shows as a diff. You approve, reject, approve all, or reject all before anything touches disk.
6. **Validation** — The Worker runs validation after every change. If it fails, the Worker inspects the error and attempts a fix. If recovery fails, the change is aborted cleanly. No broken state left behind.
7. **Commit** — Approved changes are committed with an AI-generated message. You get a receipt showing every tool call, token cost, and file changed.

The user reviews the plan before execution and reviews diffs before writes. Nothing is automatic unless you choose automation.

## Why Aura is different

Most AI coding tools are chat interfaces that try to edit files directly with no intermediate reasoning layer. Aura separates concerns:

**Planner and Worker are separate agents.** The Planner reads your codebase and produces a focused technical spec — not raw code. The Worker starts from that clean spec instead of inheriting the Planner's full reasoning trace. This means you can mix a cheap Planner with an expensive Worker, or use different providers per role.

**Aura is a harness around models, not a model itself.** Swap DeepSeek for Anthropic mid-session, change thinking depth per agent, or switch providers entirely — the workflow stays the same. The architecture, not the model, produces the consistency.

**Repo-aware by default.** Every Planner system prompt includes an AST-derived structural map of your workspace. BM25 full-text search covers 30+ file extensions across up to 1,500 files. A dependency graph traces import relationships. The AI doesn't guess your project layout — it reads it.

**Verification is part of the loop.** Run-and-watch lets the Worker start a process (dev server, compiler, test watcher), observe its output over a window of seconds, and classify the outcome. Validation isn't bolted on — it's a tool the Worker uses like any other.

## Drones and Workbay

Drones are reusable workers you build from natural language. Describe the workflow once — "stage all changes, commit with a descriptive message, push to the current branch" — and it becomes a saved Drone in your workspace. Run it with one click from the main UI. Every Drone shows up as a card in your workspace.

**Read-only Drones** run in parallel for safe background investigation — trace a bug, scout test coverage, map unused exports. No write access, no risk.

**Write-capable Drones** follow the same controlled path as any Worker: spec → edits → diff approval → validation → commit. You review every change before it lands.

**Workbay** is a visual canvas where you chain Drones into automations. Drag a Git Push Drone, a lint-fix Drone, and a test-runner Drone into a sequence. Run the chain with one click. Mission Control shows you the status of every active run.

Examples you can build in a few minutes:
- **Git Push Drone** — stage all, commit with a descriptive message, push.
- **File Size Mapper** — scan the workspace for large files, report top offenders.
- **Repo Gardener** — passive maintenance pass: remove deterministic debris, apply bounded refactors.

<p align="center">
  <img src="media/drone-workbay.png" alt="Drones and Workbay" width="900">
</p>
<p align="center"><em>Drones in Workbay — reusable agents you can run, chain, and automate.</em></p>

## Model access

Aura supports two model paths. Pick the one that fits you.

**Aura Credits** — hosted Aura models without managing provider keys. Open Settings → Aura → Buy Credits, check purchase, then select Aura as your Planner or Worker provider. No credit card needed to start.

**Bring Your Own Keys** — connect DeepSeek, OpenAI, Anthropic, Gemini, or OpenRouter directly. Set your API key in Settings; it's encrypted to disk with a machine-derived key. Environment variables also work (DEEPSEEK_API_KEY, OPENAI_API_KEY, etc.).

Both paths support the full Planner/Worker architecture. Mix them — use your own key for one role and Aura Credits for the other.

## Safety and control

Aura is designed around reviewable changes, not blind writes.

- **Diff approval on every write** — every write_file, edit_file, or edit_symbol shows a diff before touching disk. Approve, reject, approve all, or reject all.
- **Automatic backups** — existing files are backed up to `.aura/backups/` before any edit.
- **Read-only mode** — prevents all writes. Safe for exploration and investigation.
- **Validation and recovery** — every change is validated. The Worker retries on failure and aborts cleanly if recovery fails.
- **Git safety net** — snapshot/restore for experimental checkpoints, `/undo` to soft-reset the last commit, auto-generated commit messages.
- **Encrypted API keys** — stored with a hardware-derived key, not plaintext.

## Quick start

**Windows:** Download the latest installer from [Releases](https://github.com/CarpseDeam/Aura-IDE/releases). Per-user install, no admin rights needed. In-app updates handled automatically.

**From source (all platforms):**
```bash
git clone https://github.com/CarpseDeam/Aura-IDE.git
cd Aura-IDE
pip install .
aura
```

**First run:**
1. Open a workspace (File → Open Workspace).
2. Choose your model path:
   - **Aura Credits:** Settings → Aura → Buy Credits. Select Aura as Planner or Worker provider.
   - **BYOK:** Settings → API Keys. Add your key for DeepSeek, OpenAI, Anthropic, Gemini, or OpenRouter.
3. Ask for something small — "fix a typo in README.md" or "add a docstring to this function."
4. Review the Planner's spec, then click dispatch.
5. Approve or reject each diff the Worker proposes.
6. Watch validation run. Review the receipt.

## Advanced capabilities

- **AST repo map** — structural workspace map built from Python AST parsing. Every Planner system prompt includes it.
- **Dependency graph** — import-tree traversal for blast radius analysis. Know what breaks before you change it.
- **BM25 codebase search** — full-text semantic search across 30+ file extensions and up to 1,500 files. Powers the `search_codebase` tool.
- **Run-and-watch verification** — the Worker can start a process (dev server, compiler, watcher), observe output over a configurable window, and classify the result.
- **Git integration** — status, diff, commit, undo, snapshot/restore, automatic `.gitignore` setup.
- **Mobile companion** — relay server lets you chat with your Planner from your phone. Dispatch specs remotely, watch the desktop stream execution live. No separate app needed — works through your browser.
- **Web research** — built-in sub-agent using Tavily + BeautifulSoup for live web lookups during planning.
- **MCP tool integration** — connect custom stdio MCP servers. Tools are converted to OpenAI-compatible function schemas automatically.
- **Self-updater** — Windows builds check for updates and install in-place. Git-based updates for source installs.

## Built with Aura

Aura wrote most of itself. During May 2026 it processed **1.1 billion DeepSeek tokens** across nearly **30,000 API requests** while building its own codebase.

<p align="center">
  <img src="media/aura-may.png" alt="Token usage for May 2026" width="600">
</p>

The harness produces the quality, not the model. Swap models, swap providers, change thinking depth — the workflow stays the same and the output stays consistent.

---

[Full documentation](docs/README.md) — getting-started guide, tool reference, provider config, and more.

[Aura blog](https://aura-ide.hashnode.dev/) — project updates, design deep-dives, usage guides

<p>
  <a href="https://www.producthunt.com/products/aura-ide?embed=true&utm_source=badge-featured&utm_medium=badge&utm_campaign=badge-aura-ide" target="_blank" rel="noopener noreferrer">
    <img alt="Aura IDE - Open source AI coding harness you control | Product Hunt" width="150" height="54" src="https://api.producthunt.com/widgets/embed-image/v1/featured.svg?post_id=1162818&theme=light&t=1780584703498">
  </a>
  <a href="https://buymeacoffee.com/snowballkori" target="_blank" rel="noopener noreferrer">
    <img alt="Buy me a coffee" src="https://img.shields.io/badge/Buy%20me%20a%20coffee-support%20Aura-yellow?logo=buymeacoffee" height="54">
  </a>
</p>

MIT License — see [LICENSE](LICENSE).
