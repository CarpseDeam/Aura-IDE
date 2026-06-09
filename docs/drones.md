# Drones

## What Drones Are

Drones are reusable AI workers created from natural language descriptions. They are saved per project in `.aura/drones/`. Each drone has a name, description, instructions, write policy, tool access, and budget limits. When the Planner detects a match between your request and a saved drone's description, it can suggest launching it.

## Drone Bay

The Drone Bay is the UI for managing drones, accessible from the left edge rail. Here you can:

- **Create** a new drone from scratch
- **Edit** an existing drone's definition
- **Duplicate** a drone as a starting point for a new one
- **Delete** a drone
- **Launch** any drone immediately

## Drone Editor

Fields in the editor:

| Field            | Description                                                          |
|------------------|----------------------------------------------------------------------|
| Name             | Unique identifier for the drone                                      |
| Description      | What this drone does (the Planner reads this to decide when to summon it) |
| Instructions     | The system prompt the drone uses when executing                      |
| Write Policy     | Controls write behavior (see below)                                  |
| Allowed Tools    | Optional restriction of available tools                              |
| Max Tool Rounds  | Budget limit (default 8)                                             |

## Write Policies

| Policy              | Behavior                                          | Parallel-safe |
|---------------------|---------------------------------------------------|---------------|
| `read_only`         | Strips all write tools. Multiple can run simultaneously. | Yes           |
| `ask_before_writes` | Each write requires a confirmation dialog         | No            |
| `normal_diff_approval` | Standard diff approval flow (same as Worker)   | No            |

Read-only drones are parallel-safe — you can launch multiple at once. Write-capable drones queue.

## Rail Pips

Each saved drone appears as a pip in the edge rail with status indicators:

- Idle (dim)
- Running (animated)
- Completed (checkmark)
- Failed (X)

Click a pip to open the Drone Reports window.

## Drone Reports Window

Shows live output for running drones and saved receipts for completed ones. Each report includes the drone's run log, tool calls, and results.

## Save-as-Drone

After any Worker run completes, you can save it as a new drone. The Worker's instructions, tools, and behavior become the drone's definition. This lets you capture successful patterns and reuse them.

## Planner Summon

When `auto_summon_drones` is enabled (Settings → General), the Planner checks your request against saved drone descriptions. If a match exceeds the similarity threshold, it sends a `summon_drone` tool call suggesting the drone. You approve or dismiss the suggestion. The drone runs alongside or instead of the normal Worker.

## Cancellation

Any running drone can be cancelled from the Drone Reports window or the rail pip context menu.
