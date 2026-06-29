# Configuration

## Config File Location

Settings are stored in `~/.config/Aura/config.json` (platform-specific via platformdirs). Edit this file directly or use the Settings dialog.

## Settings Table

| Setting                       | Type    | Default                  | Description                                               |
|-------------------------------|---------|--------------------------|-----------------------------------------------------------|
| `provider`                    | str     | `"deepseek"`             | Default provider                                          |
| `planner_provider`            | str     | `"deepseek"`             | Provider for the Planner agent                            |
| `worker_provider`             | str     | `"deepseek"`             | Provider for the Worker agent                             |
| `planner_backend`             | str     | `"default_api"`          | Backend for Planner (`"default_api"` or a CLI backend ID) |
| `worker_backend`              | str     | `"default_api"`          | Backend for Worker                                        |
| `default_model`               | str     | `"deepseek-v4-flash"`    | Default model for single-mode                             |
| `default_planner_model`       | str     | `"deepseek-v4-flash"`    | Planner model                                             |
| `default_worker_model`        | str     | `"deepseek-v4-pro"`      | Worker model                                              |
| `default_thinking`            | str     | `"high"`                 | Thinking mode: `"off"`, `"high"`, `"max"`                 |
| `default_planner_thinking`    | str     | `"off"`                  | Planner thinking mode                                     |
| `default_worker_thinking`     | str     | `"high"`                 | Worker thinking mode                                      |
| `temperature`                 | float   | `0.7`                    | Planner temperature (0.0–2.0)                             |
| `worker_temperature`          | float   | `0.1`                    | Worker temperature                                        |
| `system_prompt`               | str     | `""`                     | Custom system prompt for single mode                      |
| `planner_system_prompt`       | str     | `""`                     | Custom system prompt for Planner                          |
| `worker_system_prompt`        | str     | `""`                     | Custom system prompt for Worker                           |
| `planner_worker_mode`         | bool    | `true`                   | Enable Planner/Worker two-agent mode                      |
| `show_planner_reasoning`      | bool    | `false`                  | Show Planner's reasoning in the UI                        |
| `restore_last_conversation`   | bool    | `true`                   | Restore the last conversation on launch                   |
| `auto_commit_enabled`         | bool    | `true`                   | Auto-commit after Worker cycles                           |
| `auto_dispatch`               | bool    | `false`                  | Skip manual dispatch confirmation                         |
| `auto_approve`                | bool    | `false`                  | Skip diff approval for writes                             |
| `auto_summon_drones`          | bool    | `false`                  | Allow Planner to suggest drones                           |
| `sandbox_mode`                | str     | `"host"`                 | Execution sandbox: `"host"`, `"docker"`, `"wasm"`        |
| `max_tool_rounds`             | int     | `50`                     | Maximum tool call rounds per conversation                 |
| `tavily_api_key`              | str     | `""`                     | Tavily search API key                                     |
| `companion_enabled`           | bool    | `false`                  | Enable mobile companion (session-only, never persisted)   |
| `companion_relay_url`         | str     | `"ws://localhost:8765"`  | WebSocket relay URL                                       |
| `companion_display_name`      | str     | `""`                     | Display name for this desktop                             |
| `companion_web_url`           | str     | `"http://localhost:5173"`| Web UI URL for companion                                  |
| `humanizer_enabled`           | bool    | `true`                   | Enable Humanizer quality checks                           |
| `humanizer_gate_enabled`      | bool    | `false`                  | Block writes that fail Humanizer checks                   |
| `humanizer_gate_min_severity` | str     | `"high"`                 | Minimum severity to block: `"critical"`, `"high"`, `"medium"`, `"low"` |
| `first_launch_done`           | bool    | `false`                  | Whether onboarding has completed                          |

## Settings Dialog

Accessed from the gear icon in the bottom-left corner. Organized into pages:

- **General** — Auto-Dispatch, Auto-Approve, Auto-Summon Drones, Tavily API key, tool rounds
- **Models** — Provider selection, model selection, thinking mode, temperature per agent
- **Backends** — API vs CLI backend selection per agent
- **System Prompts** — Custom prompts for Single, Planner, and Worker modes
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

- **Single** — Used for non-Planner/Worker mode conversations
- **Planner** — Prepended to the Planner's system prompt
- **Worker** — Prepended to the Worker's system prompt

Each supports `{tier1_context}` and `{private_worker_style}` template variables.

## Session Cost Tracking

The session cost tracker records:

- Tokens per model: cache hits, cache misses, output
- Token costs calculated from per-model pricing (input per million tokens, output per million tokens, cache hit discount)
- OpenRouter: real-time pricing fetched when available, fallback to local estimates
- Displayed in the status bar and expanded in the Info Hub
