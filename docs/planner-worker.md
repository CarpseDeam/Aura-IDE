# Planner/Worker Architecture

## Two-Agent Architecture

Aura uses two separate AI agents that can run on different models, providers, and thinking depths:

**Planner** — Reads your project (AST repo map, BM25 index, file contents). Writes a structured technical spec describing what to change and how. Does not write code.

**Worker** — Receives the spec and executes it with full filesystem access. Writes files, runs validation, recovers on failure. Every write goes through diff approval.

## Spec-as-Token-Firewall

The spec is the boundary between planning and execution. The Planner emits a structured document — not raw code — and the Worker starts from that clean target. This prevents:

- Planner reasoning noise from bleeding into the implementation
- Hallucinated context from the planning phase contaminating the execution
- The Worker inheriting the Planner's dead ends or incorrect assumptions

The spec is human-readable and editable. You can modify it before dispatch.

## Spec Edit Dialog

Before dispatching, click "Edit Spec" to open the Spec Edit dialog. This lets you:

- Rewrite any part of the spec
- Add constraints or non-goals
- Change file paths or implementation approach
- Remove parts you don't want implemented

Your edits become the Worker's instructions.

## Model Mixing

Planner and Worker can use different models — even from different providers. For example:

- **Planner:** DeepSeek V4 Flash (fast, cheap, sufficient for analysis)
- **Worker:** DeepSeek V4 Pro (deeper reasoning for implementation)

Configure these independently in Settings → Models.

## Thinking Modes

Three levels, configurable independently for Planner and Worker:

| Mode | Effect                                        | Use Case                             |
|------|-----------------------------------------------|--------------------------------------|
| Off  | Standard generation, no extended thinking      | Fast planning (Planner default)      |
| High | Extended reasoning tokens, better quality      | Worker implementation (Worker default) |
| Max  | Maximum reasoning depth, highest cost          | Complex refactors, bug diagnostics   |

**Defaults:** Planner = Off (speed), Worker = High (quality). The rationale: planning benefits from fast iteration, while execution needs thorough reasoning.

## Temperature

Planner and Worker have separate temperature settings:

- **Planner:** 0.7 — more creative exploration of approaches
- **Worker:** 0.1 — deterministic, precise implementation

## Auto-Dispatch

When enabled (Settings → General), specs are sent directly to the Worker without requiring a manual Dispatch click. The spec card still appears — you can review it — but dispatch is automatic after a short delay. Off by default.
