from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class DroneBudget:
    timeout_seconds: int = 300


@dataclass(frozen=True)
class DroneDefinition:
    id: str
    name: str
    description: str
    instructions: str
    write_policy: str  # "read_only" | "ask_before_writes" | "normal_diff_approval"
    output_contract: dict[str, Any] = field(default_factory=dict)
    budget: DroneBudget = field(default_factory=DroneBudget)
    scope: str = "global"
    kind: str = "command"
    enabled: bool = True
    created_by: str = "user"
    created_at: str = ""
    updated_at: str = ""
    # Route decision committed at build time — not a rule table, not capability resolution.
    # Shape: {"type": "api"|"feed"|"endpoint"|"mcp"|"browser"|"local",
    #         "targets": [...], "auth": "none"|"api_key"|"oauth"|"basic",
    #         "reason": "...", "fallback": "..."}
    route: dict[str, Any] = field(default_factory=dict)
    # Structured input and output contracts.
    # Schema is a JSON Schema fragment.
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

