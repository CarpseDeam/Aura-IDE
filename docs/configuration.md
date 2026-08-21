# Configuration

## Config File Location

Settings are stored in `~/.config/Aura/config.json` (platform-specific via platformdirs). Edit this file directly or use the Settings dialog.

## Production Settings

Normal Aura coding runs **one continuous production model**. It receives your
original conversation and owns the whole job — repository inspection, a concise
checklist when useful, edits, terminal execution, diagnosis, repair, validation
rerun, and one factual completion receipt.

Four settings define it:

| Setting            | Type  | Default               | Description                               |
|--------------------|-------|-----------------------|-------------------------------------------|
| `provider`         | str   | `"deepseek"`          | Provider for the production model         |
| `default_model`    | str   | `"deepseek-v4-flash"` | The production model                      |
| `default_thinking` | str   | `"high"`              | Thinking mode: `"off"`, `"high"`, `"max"` |
| `temperature`      | float | `0.7`                 | Sampling temperature (0.0–2.0)            |

These are what the Models settings page and the left sidebar edit.

## Settings Table

| Setting                       | Type    | Default                  | Description                                               |
|-------------------------------|---------|--------------------------|-----------------------------------------------------------|
| `provider`                    | str     | `"deepseek"`             | Production provider                                       |
| `default_model`               | str     | `"deepseek-v4-flash"`    | Production model                                          |
| `default_thinking`            | str     | `"high"`                 | Thinking mode: `"off"`, `"high"`, `"max"`                 |
| `temperature`                 | float   | `0.7`                    | Production temperature (0.0–2.0)                          |
| `restore_last_conversation`   | bool    | `true`                   | Restore the last conversation on launch                   |
| `auto_approve`                | bool    | `false`                  | Skip diff approval for writes                             |
| `auto_summon_drones`          | bool    | `false`                  | Summon suggested drones without a confirmation card       |
| `sandbox_mode`                | str     | `"host"`                 | Execution sandbox: `"host"`, `"docker"`, `"wasm"`        |
| `max_tool_rounds`             | int     | `50`                     | Maximum tool call rounds per conversation                 |
| `tavily_api_key`              | str     | `""`                     | Tavily search API key                                     |
| `companion_enabled`           | bool    | `false`                  | Enable mobile companion (session-only, never persisted)   |
| `companion_relay_url`         | str     | `"ws://localhost:8765"`  | WebSocket relay URL                                       |
| `companion_display_name`      | str     | `""`                     | Display name for this desktop                             |
| `companion_web_url`           | str     | `"http://localhost:5173"`| Web UI URL for companion                                  |
| `first_launch_done`           | bool    | `false`                  | Whether onboarding has completed                          |

## Settings Dialog

Accessed from the gear icon in the bottom-left corner. Organized into pages:

- **General** — Auto-Approve, Auto-Summon Drones, Tavily API key, tool rounds
- **Models** — Production provider, model, thinking mode, and temperature
- **Backends** — API vs CLI backend selection
- **Sandbox** — Execution sandbox mode
- **MCP** — MCP server commands
- **Companion** — Mobile companion settings

## Sandbox Execution Modes

| Mode     | Description                                                    |
|----------|----------------------------------------------------------------|
| `host`   | Commands run directly on the host machine                      |
| `docker` | Commands run in a Docker container with security constraints   |
| `wasm`   | Reserved for future WebAssembly sandbox                        |

Docker sandbox constraints:

- 2 GB memory limit
- 2 CPU limit
- PID limit
- Dropped Linux capabilities
- `--no-new-privileges` flag
- Read-only root filesystem for dynamic tool execution

## Session Cost Tracking

The session cost tracker records:

- Tokens per model: cache hits, cache misses, output
- Token costs calculated from per-model pricing (input per million tokens, output per million tokens, cache hit discount)
- OpenRouter: real-time pricing fetched when available, fallback to local estimates
- Displayed in the status bar and expanded in the Info Hub
