# Aura

[![Python](https://img.shields.io/badge/python-3.10+-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)]()
[![Version](https://img.shields.io/badge/version-1.7.10-orange)]()

<p>
  <a href="https://www.producthunt.com/products/aura-ide?embed=true&utm_source=badge-featured&utm_medium=badge&utm_campaign=badge-aura-ide" target="_blank" rel="noopener noreferrer">
    <img alt="Aura IDE - Open source AI coding harness you control | Product Hunt" width="150" height="54" src="https://api.producthunt.com/widgets/embed-image/v1/featured.svg?post_id=1162818&theme=light&t=1780584703498">
  </a>
</p>

**The AI workflow IDE where the model is the fuel and the harness is the engine.**

Aura is a native desktop IDE that runs your prompt through a real engineering loop — repo analysis, spec writing, safe execution, diff approval, and validation recovery — before anything touches your project. It works across DeepSeek, OpenAI, Anthropic, Gemini, and OpenRouter. You bring the API key. You keep control of every change.

<p align="center">
  <img src="media/plan_and_code.gif" alt="Aura planning and coding workflow demo" width="900">
</p>
<p align="center"><em>A full Planner → Worker cycle: spec writing, dispatch, code editing with diff approval, and auto-commit.</em></p>

<p align="center">
  <img src="media/phone-home.jpg" alt="Aura mobile companion" width="300">
</p>
<p align="center"><em>Your Planner, from your phone. Chat, dispatch, watch it stream live on desktop.</em></p>

---

Here's what Aura actually does. You type a request... fix a bug, add a feature, refactor a module. The **Planner** reads your code, understands the project structure, and writes a technical spec. You see the spec. You can edit it. When you're satisfied, you dispatch it. The **Worker** executes the spec with read and write filesystem access, proposes every change as a diff for your approval, runs validation, and recovers if something breaks. Every write is backed up. Every batch of changes gets an AI-generated commit message. The whole cycle produces a receipt you can review.

What makes it different is the architecture. The Planner and Worker are two separate models that can run on different providers with different thinking depths. The Planner's output is a structured spec — not raw code — so the Worker starts from a clean target instead of inheriting the Planner's reasoning noise. Combined with a deterministic AST repo map and stable memory layers, this produces 90%+ prompt cache hit rates — not luck, architecture. That's why a full month of heavy development cost just $35.18 — most of those tokens never needed recomputing.

Aura wrote most of itself. During May 2026 it processed **1.1 billion DeepSeek tokens** across nearly **30,000 API requests** while building its own codebase.

<p align="center">
  <img src="media/aura-may.png" alt="Token usage for May 2026" width="600">
</p>

The harness produces the quality, not the model. Swap models, swap providers, change thinking depth — the workflow stays the same and the output stays consistent.

---

**What you get.**

**Planner/Worker architecture** — Two specialized agents. One plans, one executes. The spec is a token firewall between them. You review every dispatch before it runs.

**Drones** — Reusable AI workers you create from natural language descriptions and save per project. Each drone has a write policy: read-only (parallel-safe), ask-before-writes, or normal diff approval. Save any useful Worker run as a new drone. The Planner can summon saved drones when it detects a match.

**Mobile companion** — A relay server lets you chat with your Planner from your phone. Dispatch specs remotely, watch the desktop stream the execution live. No separate mobile app needed — it works through your browser.

**Multi-provider model agnosticism** — DeepSeek, OpenAI, Anthropic, Gemini, OpenRouter. Swap models and providers per session or per agent without changing anything else. Mix a cheap Planner with an expensive Worker. The architecture abstracts the model.

**Diff approval on every write** — Every `write_file`, `edit_file`, or `edit_symbol` shows you a diff before touching disk. Approve, reject, approve all, or reject all. Existing files are backed up to `.aura/backups/` automatically.

**AST repo map and BM25 codebase search** — Every system prompt includes a structural map of your workspace built from Python AST parsing. The BM25 full-text index over 1,500 files gives the AI semantic search — not keyword grep — across 30+ file extensions.

**Git integration** — Auto-commit with AI-generated messages, `/undo` to soft-reset the last commit, snapshot/restore for experimental checkpoints, automatic `.gitignore` setup.

**Web research sub-agent** — A background agent that searches the web (Tavily), scrapes pages (BeautifulSoup), and returns a synthesized report. For documentation lookups, debugging unfamiliar errors, or researching libraries without leaving the IDE.

**MCP tool integration** — Connect any Model Context Protocol stdio server. Its tools become available to the AI alongside Aura's built-in tools. Multiple servers supported simultaneously.

**Windows installer with self-updater** — Per-user install, no admin rights needed. In-app updates check GitHub Releases and download the latest installer. One click to update.

---

**Quick start.**

```bash
pip install .
# or download the Windows installer from github.com/CarpseDeam/Aura-IDE/releases

export DEEPSEEK_API_KEY="sk-..."
# or set it in the Settings dialog — encrypted to disk

aura
```

That's it. Five lines. You're running.

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
