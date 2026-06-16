from __future__ import annotations

from pathlib import Path


def build_dispatch(description: str, target_folder: Path) -> dict:
    """Return a Worker dispatch dict to build a new Drone from a natural-language description.

    The target_folder is data_dir()/drones/<slug>/ — the Worker writes drone.json,
    entrypoint, etc. directly there. The drone is Ready as soon as the Worker finishes.
    """
    slug = target_folder.name
    spec = f"""Write a complete folder-backed Drone in the target directory.

The target directory is: {target_folder}

Write the following files:
- drone.json — Drone manifest following the DroneDefinition fields from aura/drones/definition.py:
  id, name, description, instructions, write_policy, output_contract, budget,
  entrypoint (dict with kind/command/protocol), route, input_contract, cargo_contract,
  permissions, secrets, dependencies, manifest_version, scope, runtime
- An entrypoint program (e.g. main.py) — a self-contained script that reads JSON from
  stdin, calls an internal run(payload) function, prints JSON to stdout.
- requirements.txt — only if dependencies beyond stdlib are needed.
- Optional README.md.

CRITICAL RULES:
- Do NOT call register_drone_folder. Write directly into the target folder.
- Do NOT install anything globally.
- Write files inside {target_folder} using absolute or workspace-relative paths.
- Use "python" as the first entrypoint command element (or "node" for JS).
- The description below is the source of truth for what to build.

- input_contract and cargo_contract each take the shape:
  {{"type": "<PascalCaseName>", "description": "...", "schema": {{<field>: "<string|number|bool|list|object|any>"}}}}
  cargo_contract.schema lists the top-level fields the drone prints to stdout.
  input_contract.schema lists the top-level fields the drone requires from its stdin.
  When the build description says this drone consumes another drone's output,
  input_contract.schema must name the fields it reads with coarse types,
  so the chain validator can match shapes structurally.
  A pure source drone may leave input_contract empty (or omit it);
  a pure sink may leave cargo_contract empty (or omit it).

DESCRIPTION:
{description}
"""
    acceptance = """Acceptance criteria:
1. py_compile the entrypoint program if it's Python.
2. Verify drone.json is valid JSON with the required fields: id, name, description,
   instructions, write_policy, output_contract, entrypoint.
3. Verify the entrypoint command points to a file that exists in the target folder.
4. drone.json includes both input_contract and cargo_contract keys. Each non-empty
   contract contains a "schema" object whose values are drawn from the coarse type set
   (string|number|bool|list|object|any).
"""
    # Use the slug as a display-ready name
    name = slug.replace("-", " ").title()
    return {
        "goal": f"Build Drone: {name}",
        "files": [str(target_folder)],
        "spec": spec,
        "acceptance": acceptance,
        "summary": f"Build new Drone '{name}' from description.",
    }


def revise_dispatch(target_folder: Path, feedback: str) -> dict:
    """Return a Worker dispatch dict to revise an existing Drone.

    The Worker reads the current drone.json and entrypoint from target_folder,
    applies the natural-language feedback, then rewrites them in place.
    The drone id stays the same; the drone stays Ready throughout.
    """
    folder_name = target_folder.name
    spec = f"""Revise the existing Drone at {target_folder}.

Read the current files (drone.json, entrypoint script), understand the current
implementation, then apply the following feedback and rewrite the files in place.

CRITICAL RULES:
- Read existing files first to understand the current drone.
- Keep the same drone id — do NOT change it.
- Rewrite drone.json and entrypoint in place (same folder).
- Do NOT call register_drone_folder.
- The drone must remain functional after revision.

FEEDBACK:
{feedback}
"""
    acceptance = """Acceptance criteria:
1. py_compile the entrypoint program if it's Python.
2. Verify drone.json is still valid JSON with the same id.
3. The feedback has been incorporated into the drone's instructions or code.
4. The entrypoint still references valid files in the drone folder.
"""
    name = folder_name.replace("-", " ").title()
    return {
        "goal": f"Revise Drone: {name}",
        "files": [str(target_folder / "drone.json"), str(target_folder)],
        "spec": spec,
        "acceptance": acceptance,
        "summary": f"Revise Drone '{name}' with provided feedback.",
    }
