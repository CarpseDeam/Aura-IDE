from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from aura.drones.capabilities import CapabilityBinding, CapabilityRequirement


@dataclass(frozen=True)
class DroneBudget:
    max_tool_rounds: int = 8
    timeout_seconds: int = 300


@dataclass(frozen=True)
class DroneDefinition:
    id: str
    name: str
    description: str
    instructions: str
    write_policy: str  # "read_only" | "ask_before_writes" | "normal_diff_approval"
    output_contract: str
    allowed_tools: tuple[str, ...] = ()
    budget: DroneBudget = field(default_factory=DroneBudget)
    scope: str = "global"
    enabled: bool = True
    created_by: str = "user"
    created_at: str = ""
    updated_at: str = ""
    capability_requirements: tuple[CapabilityRequirement, ...] = ()
    capability_bindings: tuple[CapabilityBinding, ...] = ()
    setup_steps: tuple[str, ...] = ()
    first_run_test: str = ""
    accepts: str = ""  # name of ArtifactType this drone consumes; empty = free-form goal
    produces: str = ""  # name of ArtifactType this drone emits; empty = unstructured summary
    # Route decision committed at build time — not a rule table, not capability resolution.
    # Shape: {"type": "api"|"feed"|"endpoint"|"mcp"|"browser"|"local",
    #         "targets": [...], "auth": "none"|"api_key"|"oauth"|"basic",
    #         "reason": "...", "fallback": "..."}
    route: dict[str, Any] = field(default_factory=dict)
    # Structured input and output contracts. Prefer these over the flat string
    # accepts/produces fields for new Drones. Schema is a JSON Schema fragment.
    # {"type": "<name>", "description": "...", "schema": {...}}
    input_contract: dict[str, Any] = field(default_factory=dict)
    cargo_contract: dict[str, Any] = field(default_factory=dict)
    runtime: str = ""  # Informational metadata about the preferred runtime, e.g. "python". Not used for execution.
    entrypoint: dict[str, Any] = field(default_factory=dict)  # Command entrypoint: {"kind": "command", "command": [...], "protocol": "json-stdio"}
    permissions: dict[str, Any] = field(default_factory=dict)
    secrets: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    manifest_version: str = "1"


def slugify(name: str) -> str:
    """Lowercase, replace non-alphanumeric with hyphens, collapse, strip."""
    slug = re.sub(r"[^a-zA-Z0-9]", "-", name).lower()
    slug = re.sub(r"-+", "-", slug)
    slug = slug.strip("-")
    return slug


READ_ONLY_TOOLS = (
    "read_file",
    "read_files",
    "list_directory",
    "glob",
    "grep_search",
    "read_file_outline",
    "find_usages",
    "search_codebase",
    "git_status",
    "git_diff",
    "git_log",
    "git_show",
    "git_log_file",
    "git_branch_list",
    "git_stash_list",
    "git_stash_show",
    "run_diagnostic_command",
    "get_workspace_snapshot",
)


# run_terminal_command requires shell execution capability and is only
# available to write-capable policies.
TERMINAL_TOOLS = ("run_terminal_command",)


WRITE_TOOLS = (
    "write_file",
    "delete_file",
    "edit_file",
    "edit_symbol",
    "edit_line_range",
    "patch_file",
    "apply_edit_transaction",
)


def default_tools_for_policy(write_policy: str) -> tuple[str, ...]:
    if write_policy == "read_only":
        return READ_ONLY_TOOLS
    if write_policy in ("ask_before_writes", "normal_diff_approval"):
        return READ_ONLY_TOOLS + WRITE_TOOLS + TERMINAL_TOOLS
    return READ_ONLY_TOOLS
