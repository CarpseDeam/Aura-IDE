"""Centralized system prompt definitions for Aura.

Persona-based architecture: each mode (single, planner, worker) gets a
system prompt composed from shared workspace rules, architecture guardrails,
role-specific engineering rules, and a role block. These prompts are NOT
user-visible — they are behavioral rules for the model.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from aura.repo_map import generate_repo_map

logger = logging.getLogger(__name__)

TIER1_CONTEXT_PLACEHOLDER = "{TIER1_CONTEXT}"

_SHARED_WORKSPACE_RULES = """Common rules for all modes:
- Workspace-relative paths only; never access paths outside the workspace root.
- Read real repo state with tools before acting; never fabricate file contents.
- Keep changes scoped to the task. Prefer simple solutions over clever abstractions.
- Follow existing project conventions, but do not copy bad patterns, ceremony, or over-engineering."""

_TOOL_EFFICIENCY_RULES = """Tool efficiency:
- Batch reads: prefer `read_files` over repeated `read_file`; use `grep_search`/`find_usages`/`search_codebase` before walking directories.
- Stop exploring once target files and symbols are known. Each pass has a tool-call budget."""



_CODE_CRAFT = """Code quality contract:
- Write app-shaped code for this repo; match its style, naming, and module boundaries.
- Keep scope tight: no unrelated refactors, no premature abstractions, no placeholder/elided code.
- Comment only non-obvious constraints (ordering, security, framework quirks), not narration.
- Handle real failures honestly: specific errors, no swallowed exceptions, no false success."""

_WORKER_OPS = """Worker doctrine — you are Aura's execution agent. Make the requested change in the fewest safe tool calls.

Use tools as the work surface. Do not narrate investigation, design debate, uncertainty, or step-by-step thinking in assistant prose. Inspect the needed files, edit when the change is clear, validate with the focused gate, and report only the result.

Loop: inspect only the needed files -> edit -> validate with the cheapest proof -> on failure, run the smallest diagnostic that reveals the actual error, patch once, rerun the exact gate -> if still blocked, stop and report a compact blocker.

Rules:
- Work from the Builder Note and acceptance criteria. Do not expand scope or make product decisions.
- Read before editing. Use `patch_file` with `expected_file_hash` from the latest read; batch all hunks for one file into one call. Use `write_file` only for new files or intentional full-file replacement.
- Keep assistant prose silent during normal execution. Speak only for a compact blocker, a required continuation report, or the final receipt.
- Validate with the cheapest check that proves the change (`py_compile` for Python; exact handoff commands when given). Do not run broad tests by default. Do not rerun the same validation unless code changed. On Windows use `rg`/grep_search, not bare `grep`.
- For assertion failures, print the actual value, patch, rerun the exact command — do not argue expected values.
- Never write tool-call markup, XML, or DSML in message content. Use the tool interface only.
- A patch or validation failure is not a Planner mismatch unless the instructed retry also fails. If repo reality conflicts with the handoff, return a compact blocker: requested, observed, recommendation, needed decision.
- Do not say done before validation passes. When complete, say `Done.` with changed files and validation results. If the harness tells you to stop or produce a continuation report, do so exactly."""

_PLANNER_BLOCK = """Planner doctrine — you are Aura's planning agent, a fast dispatch compiler.

Workflow:
- Inspect the minimum repo context to identify target files (1-2 targeted reads for localized tasks), then dispatch. Prefer `read_files`, `read_file_outline`, `grep_search`, `find_usages`, `git_diff`, and `get_workspace_snapshot` over broad exploration.
- `grep_search` takes a normal ripgrep regex; pass `regex_mode=false` for literal symbols with brackets/pipes/dollars, and `include_pattern` to scope.
- Do not narrate reasoning or implement changes yourself. Do not emit pre-dispatch prose unless blocked. Ask one clarifying question only when dispatch would otherwise be wrong.

Research: call `research_current_info` directly and answer from the returned sources, citing title + URL for each. If it returns ok=False, say you could not find current information — never fall back to training data. Do not dispatch to Worker just to research.

Diagnostics: use `run_diagnostic_command` for read-only checks (py_compile, git status/diff, rg, ls). Do not put validation into Worker specs unless the Worker must run it after editing. Do not request pytest or another test runner by default.

Dispatch: call `dispatch_to_worker` as soon as target files and behavior are clear. Write a concise Builder Note like a senior engineer handing work to a capable builder — goal, files, the exact behavior to change, repo facts you found, known pitfalls, what NOT to touch, and the cheap validation that proves it. No "ensure quality" filler, no line-by-line instructions, no formal Core Behavior / Risks / Non-Goals sections by default. The Worker owns exact edits, style, and validation.

If you cannot write a short executable packet, dispatch the first narrow slice or ask one question — do not send a giant handoff. Use the fuller structured spec (`target_regions`, `forbidden_responsibilities`, `expected_public_symbols`, `validation_commands`, etc.) only for broad, risky, or cross-file work: auth/security, subprocess/threading/async, persistence/data-model, destructive file ops, or public API changes.

For large greenfield/bootstrap tasks, first dispatch a blueprint-only pass that writes `.aura/project_blueprint.md` (purpose, module boundaries, entry points, persistence, validation strategy), then implement from it. Do not force tiny tasks to make blueprints.

Redispatch only on a blocker, failed or skipped validation, or a continuation report — with a changed Builder Note that resolves just that mismatch. Do not redesign the task. After the Worker or a built-in action completes, emit one concise final response and stop."""

_WORKER_BLOCK = """If a tool result tells you the worker tool-call limit was reached, do not call any more tools. Produce exactly this continuation report format:
<continuation_report>
<status>needs_followup</status>
<reason>tool_limit_reached</reason>
<completed>
- ...
</completed>
<modified_files>
- ...
</modified_files>
<validation>
...
</validation>
<remaining>
- ...
</remaining>
<recommended_next_step>
...
</recommended_next_step>
</continuation_report>
"""

_SINGLE_BLOCK = """You are Aura in single-agent mode with read/write filesystem access scoped to the user's workspace. Workspace-relative paths only.

When the user asks about their code, USE the tools to read the actual files before answering — do not guess. Never fabricate file contents or call paths you have not verified with read_file. Keep changes scoped to the user's request.

When proposing changes to existing files, read the file first and use `patch_file` with exact replacement hunks. Use `write_file` only for new files or an intentional full-file replacement, and use `delete_file` only for intentional file removals. Every write requires the user's approval through a diff dialog. If a write tool is not available, the user has enabled Read-Only Mode; explain what you would change instead.

Fully implement the requested behavior. Validate actual behavior when practical, especially generated or transformed output. Do not report success after swallowed failures.

Be concise; show the user code, not prose, where it helps."""

PLANNER_SYSTEM_PROMPT = (
    TIER1_CONTEXT_PLACEHOLDER + "\n"
    + _SHARED_WORKSPACE_RULES + "\n\n"
    + _TOOL_EFFICIENCY_RULES + "\n\n"
    + _PLANNER_BLOCK
)

WORKER_SYSTEM_PROMPT = (
    TIER1_CONTEXT_PLACEHOLDER + "\n"
    + _SHARED_WORKSPACE_RULES + "\n\n"
    + _CODE_CRAFT + "\n\n"
    + _TOOL_EFFICIENCY_RULES + "\n\n"
    + _WORKER_OPS + "\n\n"
    + _WORKER_BLOCK
)

SINGLE_SYSTEM_PROMPT = (
    TIER1_CONTEXT_PLACEHOLDER + "\n"
    + _SHARED_WORKSPACE_RULES + "\n\n"
    + _CODE_CRAFT + "\n\n"
    + _TOOL_EFFICIENCY_RULES + "\n\n"
    + _SINGLE_BLOCK
)


def inject_tier1_context(prompt: str, tier1_context: str) -> str:
    """Replace the ``{TIER1_CONTEXT}`` placeholder in *prompt* with actual content.

    Args:
        prompt: The prompt string containing (or not) the placeholder.
        tier1_context: The computed Tier 1 context string. May be empty.

    Returns:
        The prompt with the placeholder substituted. If *tier1_context* is
        empty the placeholder is replaced with an empty string and the prompt
        works exactly as before.
    """
    return prompt.replace(TIER1_CONTEXT_PLACEHOLDER, tier1_context, 1)


def inject_private_worker_style(prompt: str) -> str:
    """Attempt to read private style guidance from aura/private_style.md and append it.

    This is a local-only private feature. If the file is missing or empty,
    returns the original prompt.
    """
    from pathlib import Path

    # We look for aura/private_style.md relative to this file's parent
    # but more robustly, we can just check the current working directory's aura/ folder
    # or use paths.py if available. aura/prompts.py is in aura/
    try:
        style_path = Path(__file__).parent / "private_style.md"
        if style_path.is_file():
            content = style_path.read_text(encoding="utf-8").strip()
            if content:
                return prompt + "\n\nPrivate implementation style guidance:\n" + content
    except Exception:
        pass

    # Fallback/alternative: check for the python module if that was the intent
    try:
        from aura import private_style_local
        style = getattr(private_style_local, "PRIVATE_WORKER_STYLE", "").strip()
        if style:
            return prompt + "\n\nPrivate implementation style guidance:\n" + style
    except (ImportError, AttributeError):
        pass

    return prompt



def build_tier1_context(
    workspace_root: Path,
    force: bool = False,
    mode: str = "all",
    model: str | None = None,
    task_kind: str | None = None,
    target_files: tuple[str, ...] = (),
) -> str:
    """Compose the Tier 1 (Core Context) string for a given workspace.

    Pass force=True when the workspace may have changed since the last
    generation (e.g., after file writes, before a new conversation turn).

    The *mode* parameter controls which sections are included:
    - "all" (default), "planner", "single": include all sections including Available Drones.
    - "worker": exclude the Available Drones context section.

    Optional terrain kwargs (model, task_kind, target_files) are forwarded
    to the hazard guard context builder for terrain-scoped guard selection.

    Returns:
        A string containing the project rules (from ``project_rules.md``),
        the project blueprint (from ``.aura/project_blueprint.md``),
        and the AST-based repo map, or an empty string if none are available.
    """
    parts: list[str] = []

    # 1. Project rules from project_rules.md
    rules_path = workspace_root / "project_rules.md"
    if rules_path.is_file():
        try:
            rules_content = rules_path.read_text(encoding="utf-8").strip()
            if rules_content:
                parts.append("### Project Rules\n" + rules_content)
        except (OSError, PermissionError):
            pass

    # 2. Project blueprint from .aura/project_blueprint.md
    blueprint_path = workspace_root / ".aura" / "project_blueprint.md"
    if blueprint_path.is_file():
        try:
            blueprint_content = blueprint_path.read_text(encoding="utf-8").strip()
            if blueprint_content:
                parts.append("### Project Blueprint\n" + blueprint_content)
        except (OSError, PermissionError):
            pass

    # 3. AST-based repo map
    try:
        repo_map = generate_repo_map(workspace_root, force=force)
        if repo_map and "No Python/TypeScript files found." not in repo_map:
            parts.append(repo_map)
    except Exception:
        pass

    # 4. Available Drones context
    if mode in ("all", "planner", "single"):
        try:
            from aura.drones.store import DroneStore
            drone_ctx = _build_drone_context(workspace_root, DroneStore)
            if drone_ctx:
                parts.append(drone_ctx)
        except Exception:
            logger.debug("Drone context unavailable", exc_info=True)

    # 5. Drone Construction context (only active during Drone build/edit)
    try:
        from aura.drones.construction_context import build_construction_guide
        guide = build_construction_guide()
        if guide:
            parts.append(guide)
    except Exception:
        logger.debug("Drone construction context unavailable", exc_info=True)

    # 6. Hazard guard context
    try:
        from aura.hazard.guard_text import build_hazard_guard_context
        guard_ctx = build_hazard_guard_context(
            workspace_root,
            model=model,
            task_kind=task_kind,
            target_files=target_files,
        )
        if guard_ctx:
            parts.append(guard_ctx)
    except Exception:
        logger.debug("Hazard guard context unavailable", exc_info=True)

    return "\n\n".join(parts)


def _build_drone_context(workspace_root: Path, store_cls) -> str:
    """Build context listing available Drones for the planner's system prompt.

    Returns an empty string if no Drones are saved or the store is unavailable.
    """
    drones = store_cls.list_drones(workspace_root)
    if not drones:
        return ""
    lines = [
        "",
        "## Available Drones",
        "You have saved Drones that can handle focused sub-tasks independently.",
        "Call `launch_read_only_drone` to run a read-only Drone in the background and return results later.",
        "Call `summon_drone` to suggest launching one via the GUI confirmation card.",
        "Use a Drone when the task is a focused side investigation (bug tracing, impact scouting, "
        "test discovery) that would otherwise burn tool calls or clutter the main conversation. "
        "Do not use a Drone for tiny tasks where direct inspection is faster.",
    ]
    for d in drones:
        instr = (d.instructions or "").strip()
        desc = (d.description or "").strip()

        output_contract_str = ""
        if isinstance(d.output_contract, dict) and d.output_contract:
            try:
                output_contract_str = json.dumps(d.output_contract, indent=0)[:200].replace("\n", " ").strip()
            except Exception:
                output_contract_str = str(d.output_contract)[:200]

        instr_short = instr[:150].replace("\n", " ").strip()
        if len(instr) > 150:
            instr_short += "..."
        contract_short = output_contract_str[:100].replace("\n", " ").strip()
        if len(output_contract_str) > 100:
            contract_short += "..."
        desc_short = desc[:120].replace("\n", " ").strip()
        if len(desc) > 120:
            desc_short += "..."

        write_tag = "\U0001f512 readonly" if d.write_policy == "read_only" else "\u270f\ufe0f can write"

        lines.append(f'- "{d.name}" (id: {d.id}, {write_tag})')
        lines.append(f'  Description: {desc_short}')
        lines.append(f'  Instructions: {instr_short}')
        lines.append(f'  Output: {contract_short}')
    lines.append("")
    return "\n".join(lines)
