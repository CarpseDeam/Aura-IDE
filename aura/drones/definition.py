from __future__ import annotations

import re
from dataclasses import dataclass, field


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
    allowed_tools: tuple[str, ...]
    output_contract: str
    budget: DroneBudget = field(default_factory=DroneBudget)
    scope: str = "project"
    enabled: bool = True
    created_by: str = "user"
    created_at: str = ""
    updated_at: str = ""


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
    "run_terminal_command",
    "run_diagnostic_command",
    "get_workspace_snapshot",
)


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
        return READ_ONLY_TOOLS + WRITE_TOOLS
    return READ_ONLY_TOOLS
