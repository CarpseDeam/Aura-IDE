# Providers

> **Note:** Aura also supports **Aura Credits** as an alternative to managing API keys. See [Model Access Setup](getting-started.md#model-access-setup) in the getting-started guide.

## Supported Providers

Aura supports five API-key-based providers:

| Provider      | Label          | Base URL                            | Env Variable          | Default Model                |
|---------------|----------------|-------------------------------------|-----------------------|------------------------------|
| deepseek      | DeepSeek       | https://api.deepseek.com            | DEEPSEEK_API_KEY      | deepseek-v4-flash            |
| openai        | OpenAI         | https://api.openai.com/v1           | OPENAI_API_KEY        | gpt-5.4-mini                 |
| anthropic     | Anthropic      | https://api.anthropic.com/v1        | ANTHROPIC_API_KEY     | claude-sonnet-4-6            |
| google_cloud  | Google Gemini  | —                                   | GEMINI_API_KEY        | gemini-2.5-flash             |
| openrouter    | OpenRouter     | https://openrouter.ai/api/v1        | OPENROUTER_API_KEY    | deepseek/deepseek-v4-flash   |

## Dynamic Model Fetching

On first launch and periodically thereafter, Aura fetches the latest model lists and pricing from each provider's API. Models are cached to disk at `~/.config/Aura/catalog_cache.json`. You can trigger a manual refresh in Settings.

For DeepSeek and Google Cloud, models are hardcoded in the catalog and supplemented by dynamic fetching. OpenRouter model pricing is fetched in real-time when available.

## CLI Agent Backends

Aura supports three CLI-based agent backends as alternative execution layers. These use the tool's own OAuth flow, so you don't need to manage API keys through Aura.

| Backend      | Provider ID    | CLI Command   | Auth                        |
|--------------|----------------|---------------|-----------------------------|
| Claude Code  | claude_code    | `claude`      | OAuth via Anthropic         |
| Codex CLI    | codex          | `codex`       | OAuth via OpenAI            |
| Antigravity  | antigravity    | `antigravity` | OAuth via Antigravity       |

CLI backends are selected independently for Planner and Worker in Settings → Backends.

## Plugable Backend Architecture

Aura's `AgentBackend` abstract class defines the streaming interface. The `APIAgentBackend` handles REST API providers through the provider registry. CLI backends wrap subprocess calls to external tools. Custom backends can be implemented by subclassing `AgentBackend`.

## MCP Tool Integration

Aura connects to Model Context Protocol servers via subprocess over stdio. Multiple MCP servers can run simultaneously.

**How it works:**

1. Each MCP server is started as a subprocess with the command specified in settings.
2. The server advertises its available tools via the MCP `tools/list` endpoint.
3. Aura converts each tool definition to its internal schema and makes it available to the AI.
4. When the AI calls an MCP tool, Aura routes the call through the MCP `tools/call` endpoint.
5. Errors from MCP servers are caught and reported back to the AI.

**Configuration:**

MCP server commands are configured in Settings → MCP. Each entry is a shell command (e.g., `npx @modelcontextprotocol/server-filesystem /path`).

**GUI for MCP management is planned.** Currently configurable via settings JSON directly.
