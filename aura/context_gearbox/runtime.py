"""Runtime prompt and context composition."""
from __future__ import annotations

from pathlib import Path

from aura.context_gearbox.models import ComposedContext, ContextLedgerEntry, RuntimeRole
from aura.context_gearbox.sources import collect_source_text, iter_context_sources

CONTEXT_PLACEHOLDER = "{TIER1_CONTEXT}"

_ROLE_PROMPTS = {
    RuntimeRole.PLANNER: """Planner role:
- Identify the user's intent and likely task lane.
- Inspect minimal repository context when needed.
- Dispatch implementation work instead of coding directly.
- Rely on deterministic router output and tool results when available.""",
    RuntimeRole.WORKER: """Worker role:
- Execute only the requested change.
- Use tools for repository reads and writes.
- Validate focused behavior when practical.
- Return a compact final result.""",
    RuntimeRole.SINGLE: """Single-agent role:
- Answer or edit within the workspace.
- Read files before claiming repository facts.
- Keep scope tight.""",
}


def default_role_prompt(role: RuntimeRole | str) -> str:
    runtime_role = RuntimeRole.from_value(role)
    return "\n\n".join([CONTEXT_PLACEHOLDER, _ROLE_PROMPTS[runtime_role]])


PLANNER_SYSTEM_PROMPT = default_role_prompt(RuntimeRole.PLANNER)
WORKER_SYSTEM_PROMPT = default_role_prompt(RuntimeRole.WORKER)
SINGLE_SYSTEM_PROMPT = default_role_prompt(RuntimeRole.SINGLE)


def build_context_text(
    role: RuntimeRole | str,
    workspace_root: Path | None,
    *,
    force: bool = False,
    model: str | None = None,
    task_kind: str | None = None,
    target_files: tuple[str, ...] | None = None,
) -> ComposedContext:
    _ = model
    runtime_role = RuntimeRole.from_value(role)
    parts: list[str] = []
    ledger: list[ContextLedgerEntry] = []
    normalized_target_files = tuple(target_files or ())
    for source in iter_context_sources(runtime_role):
        text, entry = collect_source_text(
            source,
            runtime_role,
            workspace_root,
            force=force,
            task_kind=task_kind,
            target_files=normalized_target_files,
        )
        if text:
            parts.append(text)
        ledger.append(entry)
    return ComposedContext(
        role=runtime_role,
        system_prompt="",
        context_text="\n\n".join(parts),
        ledger=tuple(ledger),
    )


def compose_system_prompt(
    role: RuntimeRole | str,
    custom_prompt: str | None,
    workspace_root: Path | None,
    *,
    force: bool = False,
    model: str | None = None,
    task_kind: str | None = None,
    target_files: tuple[str, ...] | None = None,
) -> ComposedContext:
    runtime_role = RuntimeRole.from_value(role)
    context = build_context_text(
        runtime_role,
        workspace_root,
        force=force,
        model=model,
        task_kind=task_kind,
        target_files=target_files,
    )
    custom = (custom_prompt or "").strip()
    prompt_template = custom if custom else default_role_prompt(runtime_role)
    if CONTEXT_PLACEHOLDER in prompt_template:
        system_prompt = prompt_template.replace(CONTEXT_PLACEHOLDER, context.context_text, 1)
    else:
        system_prompt = prompt_template
    return ComposedContext(
        role=runtime_role,
        system_prompt=system_prompt,
        context_text=context.context_text,
        ledger=context.ledger,
    )
