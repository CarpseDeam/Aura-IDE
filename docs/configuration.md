# Configuration

## Config File Location

Settings are stored in `~/.config/Aura/config.json` (platform-specific via platformdirs). Edit this file directly or use the Settings dialog.

## Production Settings

Normal Aura coding runs **one continuous production model**. It receives your
original conversation and owns the whole job — repository inspection, the live
TODO, edits, terminal execution, diagnosis, repair, validation rerun, and one
factual completion receipt. There is no Planner-to-Worker handoff in the normal
product.

Four settings define it:

| Setting            | Type  | Default               | Description                               |
|--------------------|-------|-----------------------|-------------------------------------------|
| `provider`         | str   | `"deepseek"`          | Provider for the production model         |
| `default_model`    | str   | `"deepseek-v4-flash"` | The production model                      |
| `default_thinking` | str   | `"high"`              | Thinking mode: `"off"`, `"high"`, `"max"` |
| `temperature`      | float | `0.7`                 | Sampling temperature (0.0–2.0)            |

These are what the Models settings page and the left sidebar edit.

### Migrating from older configurations

Old configs that only carried Planner values still load. On startup Aura prefers
valid generic production values; when they are absent it migrates the Planner
provider, model, and thinking mode into the production settings. Legacy fields
are preserved in `config.json` and are never destroyed on load. `planner_worker_mode`
is read but not acted on — startup always enters production single-agent mode.

## Settings Table

| Setting                       | Type    | Default                  | Description                                               |
|-------------------------------|---------|--------------------------|-----------------------------------------------------------|
| `provider`                    | str     | `"deepseek"`             | Production provider                                       |
| `default_model`               | str     | `"deepseek-v4-flash"`    | Production model                                          |
| `default_thinking`            | str     | `"high"`                 | Thinking mode: `"off"`, `"high"`, `"max"`                 |
| `temperature`                 | float   | `0.7`                    | Production temperature (0.0–2.0)                          |
| `system_prompt`               | str     | `""`                     | Custom system prompt for the production model             |
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

### Legacy fields

These remain in `config.json` for backward compatibility. They are loaded and
preserved, but the normal product does not use them: `planner_provider`,
`worker_provider`, `planner_backend`, `worker_backend`, `default_planner_model`,
`default_worker_model`, `default_planner_thinking`, `default_worker_thinking`,
`worker_temperature`, `planner_system_prompt`, `worker_system_prompt`,
`planner_worker_mode`, `show_planner_reasoning`, `auto_dispatch`,
`auto_commit_enabled`.

## Settings Dialog

Accessed from the gear icon in the bottom-left corner. Organized into pages:

- **General** — Auto-Approve, Auto-Summon Drones, Tavily API key, tool rounds
- **Models** — Production provider, model, thinking mode, and temperature
- **Backends** — API vs CLI backend selection
- **System Prompts** — Custom prompt for the production model
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

## Custom System Prompts

- **Single** — The production model's prompt. This is the one normal coding uses.
- **Planner** / **Worker** — Legacy role prompts, retained for backward
  compatibility with older configurations. Not used by the normal product.

Each supports `{tier1_context}` and `{private_worker_style}` template variables.

## Session Cost Tracking

The session cost tracker records:

- Tokens per model: cache hits, cache misses, output
- Token costs calculated from per-model pricing (input per million tokens, output per million tokens, cache hit discount)
- OpenRouter: real-time pricing fetched when available, fallback to local estimates
- Displayed in the status bar and expanded in the Info Hub
