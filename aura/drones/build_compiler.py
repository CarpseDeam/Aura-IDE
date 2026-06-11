from __future__ import annotations

from dataclasses import dataclass

from aura.drones.capabilities import CapabilityRequirement


@dataclass(frozen=True)
class DroneBuildPlan:
    """Deterministic build plan compiled from a Drone Build Brief."""

    allowed_tools: tuple[str, ...]
    capability_requirements: tuple[CapabilityRequirement, ...]
    setup_notes: tuple[str, ...]
    generated_code_allowed: bool = False
    warnings: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Core Intent Map — shared source of truth for mapping human capability
# phrases to harness tool names.  Ordered by specificity (more specific
# keywords first) for performance, not logic — substring matching means
# order does not affect which tools match.
# ---------------------------------------------------------------------------
# Format: (keyword_substring, tool_tuple)
CORE_INTENT_MAP: list[tuple[str, tuple[str, ...]]] = [
    ("git status", ("git_status",)),
    ("git diff", ("git_diff",)),
    ("git log", ("git_log", "git_log_file")),
    ("git show", ("git_show",)),
    ("git branch", ("git_branch_list",)),
    ("git stash", ("git_stash_list", "git_stash_show")),
    ("git merge", ("git_status", "git_diff", "run_terminal_command")),
    ("git rebase", ("git_status", "git_diff", "run_terminal_command")),
    ("cherry pick", ("git_log", "run_terminal_command")),
    ("commit", ("git_status", "git_diff", "run_terminal_command")),
    ("push", ("git_status", "git_diff", "run_terminal_command")),
    ("stage", ("run_terminal_command",)),
    ("git add", ("run_terminal_command",)),
    ("read file", ("read_file", "read_files")),
    ("read files", ("read_files",)),
    ("file outline", ("read_file_outline",)),
    ("read an outline", ("read_file_outline",)),
    ("list directory", ("list_directory",)),
    ("list files", ("list_directory", "glob")),
    ("find files", ("glob",)),
    ("glob", ("glob",)),
    ("search code", ("grep_search", "search_codebase")),
    ("grep", ("grep_search",)),
    ("find in code", ("grep_search",)),
    ("find usages", ("find_usages",)),
    ("find references", ("find_usages",)),
    ("code search", ("search_codebase", "grep_search")),
    ("locate code", ("grep_search", "search_codebase")),
    ("write file", ("write_file",)),
    ("edit file", ("read_file", "edit_file", "patch_file")),
    ("patch file", ("patch_file",)),
    ("delete file", ("delete_file",)),
    ("modify file", ("read_file", "edit_file", "write_file")),
    ("edit symbol", ("edit_symbol", "read_file")),
    ("edit line", ("edit_line_range", "read_file")),
    ("apply edit", ("apply_edit_transaction",)),
    ("run command", ("run_terminal_command",)),
    ("terminal command", ("run_terminal_command",)),
    ("diagnostic", ("run_diagnostic_command",)),
    ("compile check", ("run_diagnostic_command",)),
    ("lint", ("run_diagnostic_command", "run_terminal_command")),
    ("workspace snapshot", ("get_workspace_snapshot",)),
    ("project info", ("get_workspace_snapshot",)),
    ("project state", ("get_workspace_snapshot",)),
    ("launch drone", ("launch_read_only_drone", "run_read_only_drone")),
    ("check drone", ("check_drone_run",)),
    ("summon drone", ("summon_drone",)),
    ("run drone", ("run_read_only_drone",)),
    ("save drone", ("save_drone_definition",)),
]


# ---------------------------------------------------------------------------
# External capability keywords — human phrases that indicate the Drone
# needs a capability NOT covered by the existing harness tool set.
# ---------------------------------------------------------------------------
_EXTERNAL_KEYWORDS: list[str] = [
    "email", "gmail", "send mail", "inbox",
    "twitter", "tweet", "post to social", "social media",
    "database", "sql", "query db", "postgres", "mysql", "mongodb",
    "browser", "chrome", "firefox", "safari", "web page", "scrape", "crawl",
    "call api", "http request", "rest api", "graphql", "external api",
    "slack", "discord", "telegram", "messaging",
    "jira", "trello", "asana", "project management",
    "s3", "aws", "cloud", "docker", "kubernetes",
    "deploy", "release",
]


# ---------------------------------------------------------------------------
# Generated-code keywords — phrases that explicitly ask for a new tool or
# integration.  Generated code is GATED and only allowed when the brief
# contains one of these keywords.
# ---------------------------------------------------------------------------
_GENERATED_CODE_KEYWORDS: list[str] = [
    "create a tool", "build a tool", "new tool", "new integration",
    "generate code", "code generation", "dynamic tool",
    "custom tool", "build a connector", "create an integration",
    "write a plugin", "create a plugin",
    "helper script", "generated code", "code generator",
    "write a tool", "build an integration",
]


# ---------------------------------------------------------------------------
# compile_drone_build_plan
# ---------------------------------------------------------------------------


def compile_drone_build_plan(
    brief_text: str,
    available_tools: frozenset[str],
    write_policy: str | None = None,
) -> DroneBuildPlan:
    """Compile a DroneBuildPlan from a plain-language brief.

    Scans the brief for:
    - Harness tool intent keywords (CORE_INTENT_MAP) → matched tool names
    - External capability keywords (_EXTERNAL_KEYWORDS) → CapabilityRequirements
    - Generated-code keywords (_GENERATED_CODE_KEYWORDS) → gated code flag
    """
    lower = brief_text.lower()

    # 1. Scan for harness intent keywords
    matched_tools: set[str] = set()
    for keyword, tools in CORE_INTENT_MAP:
        if keyword in lower:
            matched_tools.update(tools)

    # 2. Filter matched tools to those actually available
    allowed_tools = tuple(sorted(t for t in matched_tools if t in available_tools))

    # 3. Scan for external capability keywords
    seen_keywords: set[str] = set()
    capability_requirements: list[CapabilityRequirement] = []
    for kw in _EXTERNAL_KEYWORDS:
        if kw in lower and kw not in seen_keywords:
            seen_keywords.add(kw)
            capability_requirements.append(
                CapabilityRequirement(capability=kw)
            )

    # 4. Scan for generated-code keywords
    generated_code_allowed = any(kw in lower for kw in _GENERATED_CODE_KEYWORDS)

    # 5. Warning when nothing matched
    warnings: list[str] = []
    if brief_text.strip() and not allowed_tools and not capability_requirements:
        warnings.append(
            "No known harness tools matched the brief. If the brief truly needs "
            "a new tool, rephrase to include explicit language like 'create a new "
            "tool' or 'build an integration'."
        )

    return DroneBuildPlan(
        allowed_tools=allowed_tools,
        capability_requirements=tuple(capability_requirements),
        setup_notes=(),
        generated_code_allowed=generated_code_allowed,
        warnings=tuple(warnings),
    )
