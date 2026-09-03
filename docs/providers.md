# Providers

## Supported Providers

Aura supports hosted API-key providers and one configurable OpenAI-compatible local provider:

| Provider      | Label          | Base URL                            | Env Variable          | Default Model                |
|---------------|----------------|-------------------------------------|-----------------------|------------------------------|
| deepseek      | DeepSeek       | https://api.deepseek.com            | DEEPSEEK_API_KEY      | deepseek-v4-flash            |
| openai        | OpenAI         | https://api.openai.com/v1           | OPENAI_API_KEY        | gpt-5.4-mini                 |
| anthropic     | Anthropic      | https://api.anthropic.com/v1        | ANTHROPIC_API_KEY     | claude-sonnet-4-6            |
| google_cloud  | Google Gemini  | —                                   | GEMINI_API_KEY        | gemini-2.5-flash             |
| openrouter    | OpenRouter     | https://openrouter.ai/api/v1        | OPENROUTER_API_KEY    | deepseek/deepseek-v4-flash   |
| local_openai  | Local Model    | Configured in Settings              | —                     | Discovered from the server   |

## Local models

In **Settings → Models**, select **Local Model**, enter the server's OpenAI-compatible base URL, and click **Test / Discover**. Common defaults are:

- Ollama: `http://127.0.0.1:11434/v1`
- LM Studio: `http://127.0.0.1:1234/v1`
- llama.cpp: `http://127.0.0.1:8080/v1`

Aura discovers exact model IDs from `/v1/models` and sends normal tool-enabled turns through `/v1/chat/completions`. Aura does not download, launch, or configure the local server. Tool-calling support and context size are properties of the model/server; for coding work, configure a sufficiently large context window. Aura still records provider-reported token usage, while local events carry $0 provider cost. Local requests are serialized so parallel Agent branches do not compete for one local inference slot, while hosted branches can still run concurrently.

The same local models appear in each Agent's **Model target** picker. Root Aura, workflow Steps, dashed Sub-agents, and nested helpers can independently inherit Aura or mix any configured hosted and local targets.

## Dynamic Model Fetching

Aura fetches provider model lists through the Models settings page. Local discovery is explicit through **Test / Discover**. Models are cached to disk at `~/.config/Aura/models_cache.json` (or the platform-equivalent Aura configuration directory), and you can trigger a manual refresh in Settings.

For DeepSeek and Google Cloud, models are hardcoded in the catalog and supplemented by dynamic fetching. OpenRouter model pricing is fetched in real-time when available.

## Pluggable Backend Architecture

Aura's `AgentBackend` abstract class defines the streaming interface. The `APIAgentBackend` handles REST API providers through the provider registry. Custom backends can be implemented by subclassing `AgentBackend`.

`ProviderRegistry.create_client()` builds clients only for supported API-key and local OpenAI-compatible providers. Unknown or unsupported provider kinds raise rather than falling back to another provider's client.

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
