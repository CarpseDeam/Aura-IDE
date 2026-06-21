# Drones

Drones are folder-backed project tools. Each Drone is a folder containing a manifest (`drone.json`) and an entrypoint program. Any language that reads JSON from stdin and writes JSON to stdout works.

Drones handle repeatable work you do not want to re-explain every time. They appear as cards in the Workbay and can be run, looped, or deleted from the UI.

## Drone kinds

A Drone's `kind` field determines how it executes.

**`command`** (default) — Aura launches the entrypoint command, sends one JSON payload on stdin, and reads one JSON result from stdout. The Drone runs as a standalone process with no Planner/Worker loop.

**`harness-lap`** — The Drone runs through Aura's Planner/Worker loop with built-in guardrails. Use this for bounded maintenance work where you want safe, verified changes with rollback. Repo Gardener is a harness-lap Drone.

## Write policies

The `write_policy` field controls what a Drone may do:

| Policy | Behavior |
|---|---|
| `read_only` | Analysis only. No file modifications. Safe to run multiple in parallel. |
| `ask_before_writes` | Per-action approval before each write. |
| `normal_diff_approval` | Changes files through the same diff-approval cycle as any Worker. |

Read-only Drones can run in parallel (up to 3). Write-capable Drones use a shared write lane and run exclusively so the approval flow is not contested.

## Workbay

The Drone Workbay is a standalone window showing saved Drone cards. From each card:

- **Run** — Start a single run.
- **Loop** — Toggle looping to repeat the Drone on a timer. Each lap is one bounded run. The Drone should be safe to re-run.
- **Delete** — Remove the Drone from the roster. This deletes the Drone folder and all its contents.
- **Status** — Each card shows live state: idle, running, completed (with summary), failed (with error), or waiting for the next loop lap (with countdown).

## Receipts

Every run produces a receipt — the Drone's stdout JSON object plus run metadata (status, elapsed time, errors). Receipts are saved to `.aura/drones/runs/` per workspace so you can review past work without re-running.

## Manifest example

A minimal `drone.json` manifest:

```json
{
  "id": "source-scout",
  "name": "Source Scout",
  "description": "Collects source candidates matching a topic.",
  "instructions": "Given a topic string, search public sources and return matching candidates.",
  "write_policy": "read_only",
  "kind": "command",
  "entrypoint": {
    "kind": "command",
    "command": ["python", "main.py"],
    "protocol": "json-stdio"
  },
  "output_contract": {
    "oneOf": [
      {
        "type": "object",
        "properties": {
          "ok": {"const": true},
          "candidates": {
            "type": "array",
            "items": {
              "type": "object",
              "properties": {
                "title": {"type": "string"},
                "source": {"type": "string"},
                "url": {"type": "string"},
                "snippet": {"type": "string"}
              }
            }
          },
          "summary": {"type": "string"}
        },
        "required": ["ok", "candidates", "summary"]
      },
      {
        "type": "object",
        "properties": {
          "ok": {"const": false},
          "error": {"type": "string"},
          "summary": {"type": "string"}
        },
        "required": ["ok", "error", "summary"]
      }
    ]
  }
}
```

Optional fields include `input_contract`, `cargo_contract`, `budget`, `scope`, `permissions`, `secrets`, and `dependencies`. Required output_contract fields are `ok` (boolean) and `summary` (string).

See `aura/drones/drone_construction.md` for the full construction spec and field reference.

## Harness-lap permissions

Harness-lap Drones declare guardrails in their `permissions` manifest field:

| Field | Type | Default | Description |
|---|---|---|---|
| `require_clean_worktree` | bool | true | Skip if working tree is dirty |
| `revert_on_failure` | bool | true | Auto-rollback on failure |
| `max_changed_files` | int | 0 (unlimited) | Cap on files a single lap may change |
| `protected_paths` | string[] | [] | Glob patterns of paths the lap must not touch |

## Construction rules

- The `drone.json` manifest must be valid JSON.
- The entrypoint `command` array must reference an executable on PATH or a relative path (starting with `./`) inside the Drone folder.
- The entrypoint program reads exactly one JSON object from stdin and writes exactly one JSON object to stdout.
- Debug output, progress, and logging go to stderr. Only the final JSON result goes to stdout.
- The output JSON must include `ok` (boolean) and `summary` (string) at minimum.
- Every failure path must return `{"ok": false, "error": "...", "summary": "..."}` — never crash with no stdout.
