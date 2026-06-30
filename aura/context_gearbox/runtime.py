"""Runtime prompt and context composition."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterable

_log = logging.getLogger(__name__)

from aura.context_gearbox.models import ComposedContext, ContextLedgerEntry, RuntimeRole
from aura.context_gearbox.sources import collect_source_text, iter_registered_sources

CONTEXT_PLACEHOLDER = "{TIER1_CONTEXT}"

_RESPONSE_DISCIPLINE = """Response discipline:
- Lead with the answer, decision, or next action.
- Default to concise, useful replies.
- Avoid essays, tutorials, and multi-section breakdowns unless the user asks for depth.
- Normal chat should usually be 1-4 short paragraphs or up to 5 bullets.
- Coding/workflow replies should emphasize target, decision, next step, and validation.
- Give full detail when the user asks or when missing detail would make the answer unsafe or unusable."""

_ROLE_PROMPTS = {
    RuntimeRole.PLANNER: """Planner role:
- Choose the lane quickly: answer, ask one focused question, inspect minimally, or dispatch.
- For code changes, default to dispatch_to_worker once the objective, target seam/files, constraints, and acceptance are known.
- Inspect only the minimal repository context needed to name that capsule; do not keep researching after the capsule is actionable.
- Own intent, target seam, allowed files, constraints, non-goals, validation expectations, and the Worker capsule only.
- Create a Worker task capsule and call dispatch_to_worker; that tool call is the Planner's deliverable for implementation.
- When implementation is needed, dispatch instead of presenting a plan for the user to execute.
- If the user accepts a previously proposed actionable phase, bind that acceptance to the most recent actionable phase and dispatch it promptly.
- Acceptance phrases include "do phase 1", "start phase 1", "yes do that", "go", "run it", and "let's do it".
- Planner must not say "I will start extracting/editing/refactoring" in planner mode; dispatch the Worker instead.
- Planner must not write code, sketch patches, plan hunks, inspect exact edit ranges, or do exact implementation/edit reasoning.
- Worker owns implementation reasoning, exact edits, validation execution, and final code-quality decisions.
- For large-file helper extraction or refactors, dispatch only the first concrete slice: one helper family, one new module, one source-file wireback, and one validation command.
- Preserve structured contract fields for the slice when knowable: expected_public_symbols, expected_dataclass_fields, forbidden_calls, forbidden_public_methods, and non_goals.
- Dispatch implementation work instead of coding directly.
- Rely on deterministic router output and tool results when available.""",
    RuntimeRole.WORKER: """Worker role:
- Execute only the requested change.
- Use tools for repository reads and writes; read narrowly around the target seam.
- Once enough facts are known, make the smallest safe edit instead of restating the plan.
- Do not keep broad-orienting or comparing approaches when an edit is possible.
- Validate focused behavior after writes when practical.""",
    RuntimeRole.SINGLE: """Single-agent role:
- Answer or edit within the workspace.
- Read files before claiming repository facts.
- Keep scope tight.""",
}


def default_role_prompt(role: RuntimeRole | str) -> str:
    runtime_role = RuntimeRole.from_value(role)
    return "\n\n".join(
        [CONTEXT_PLACEHOLDER, _RESPONSE_DISCIPLINE, _ROLE_PROMPTS[runtime_role]]
    )


PLANNER_SYSTEM_PROMPT = default_role_prompt(RuntimeRole.PLANNER)
WORKER_SYSTEM_PROMPT = default_role_prompt(RuntimeRole.WORKER)
SINGLE_SYSTEM_PROMPT = default_role_prompt(RuntimeRole.SINGLE)


def serialize_context_ledger(
    entries: Iterable[ContextLedgerEntry],
) -> list[dict[str, Any]]:
    """Return deterministic plain data for runtime context ledger entries."""
    serialized: list[dict[str, Any]] = []
    for entry in entries:
        item: dict[str, Any] = {
            "source_id": entry.source_id,
            "kind": entry.kind,
            "role": entry.role.value,
            "included": bool(entry.included),
            "reason": entry.reason,
            "char_count": int(entry.char_count),
        }
        if entry.error:
            item["error"] = entry.error
        serialized.append(item)
    return serialized


def summarize_context_ledger(
    ledger: Iterable[ContextLedgerEntry] | Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Return compact loaded/skipped counts for a serialized or raw ledger."""
    entries: list[dict[str, Any]] = []
    for entry in ledger:
        if isinstance(entry, ContextLedgerEntry):
            entries.append(serialize_context_ledger((entry,))[0])
        else:
            entries.append(dict(entry))

    loaded = [str(entry["source_id"]) for entry in entries if entry.get("included")]
    skipped = [
        {
            "source_id": str(entry.get("source_id", "")),
            "reason": str(entry.get("reason", "")),
        }
        for entry in entries
        if not entry.get("included")
    ]
    loaded_count = len(loaded)
    skipped_count = len(skipped)
    return {
        "loaded_count": loaded_count,
        "skipped_count": skipped_count,
        "loaded": loaded,
        "skipped": skipped,
        "display": f"Context: {loaded_count} loaded, {skipped_count} skipped",
    }


def _serialize_source_utility(utility: Any) -> dict[str, Any]:
    """Return JSON-safe plain data for a SourceUtility-like value."""
    lift = getattr(utility, "lift")
    return {
        "source_id": str(getattr(utility, "source_id")),
        "task_kind": str(getattr(utility, "task_kind")),
        "loaded_n": int(getattr(utility, "loaded_n")),
        "not_loaded_n": int(getattr(utility, "not_loaded_n")),
        "lift": None if lift is None else float(lift),
        "status": str(getattr(utility, "status")),
    }


def _source_utility_field(utility: Any, field: str) -> Any:
    if isinstance(utility, dict):
        return utility.get(field)
    return getattr(utility, field, None)


def context_gearbox_metadata(
    entries: Iterable[ContextLedgerEntry],
    *,
    workspace_root: Path | None = None,
    task_kind: str | None = None,
) -> dict[str, Any]:
    """Return the inspectable Context Gearbox payload without prompt text.

    When *workspace_root* is provided, may include inspect-only ``"utility"``
    and ``"eviction"`` keys with per-source utility and dry-run eviction data.
    """
    ledger = serialize_context_ledger(entries)
    metadata: dict[str, Any] = {
        "summary": summarize_context_ledger(ledger),
        "ledger": ledger,
    }
    if workspace_root is not None:
        try:
            from aura.skills.utility import derive_source_utility

            utility = derive_source_utility(workspace_root)
            if utility:
                metadata["utility"] = {
                    str(source_id): _serialize_source_utility(source_utility)
                    for source_id, source_utility in utility.items()
                }
        except Exception:
            _log.exception("Failed to derive source utility (degrading)")
    if workspace_root is not None:
        try:
            from aura.skills.eviction import (
                compute_eviction_verdicts,
                summarize_eviction_report,
            )

            verdicts = compute_eviction_verdicts(workspace_root, task_kind=task_kind)
            if verdicts:
                metadata["eviction"] = summarize_eviction_report(verdicts)
        except Exception:
            _log.exception("Failed to derive skill eviction report (degrading)")
    return metadata


def format_context_gearbox_display(metadata: dict[str, Any]) -> list[str]:
    """Format compact Context Gearbox lines for log/detail surfaces."""
    summary = metadata.get("summary") if isinstance(metadata, dict) else {}
    if not isinstance(summary, dict):
        return []

    display = str(summary.get("display") or "").strip()
    lines = [display] if display else []

    loaded = summary.get("loaded")
    if isinstance(loaded, list) and loaded:
        lines.append("Loaded: " + ", ".join(str(item) for item in loaded))

    skipped = summary.get("skipped")
    if isinstance(skipped, list) and skipped:
        formatted: list[str] = []
        for item in skipped:
            if not isinstance(item, dict):
                continue
            source_id = str(item.get("source_id") or "").strip()
            reason = str(item.get("reason") or "").strip()
            if not source_id:
                continue
            formatted.append(f"{source_id} ({reason})" if reason else source_id)
        if formatted:
            lines.append("Skipped: " + ", ".join(formatted))

    # Utility display (inspect-only metadata)
    utility = metadata.get("utility") if isinstance(metadata, dict) else None
    if isinstance(utility, dict) and utility:
        parts: list[str] = []
        for source_id in sorted(utility):
            u = utility[source_id]
            status = _source_utility_field(u, "status")
            lift = _source_utility_field(u, "lift")
            loaded_n = _source_utility_field(u, "loaded_n")
            not_loaded_n = _source_utility_field(u, "not_loaded_n")
            if status == "measured" and lift is not None:
                numeric_lift = float(lift)
                sign = "+" if numeric_lift >= 0 else ""
                parts.append(
                    f"{source_id} {sign}{numeric_lift:.1%} "
                    f"(loaded={loaded_n}, not_loaded={not_loaded_n})"
                )
            else:
                parts.append(
                    f"{source_id} insufficient "
                    f"(loaded={loaded_n}, not_loaded={not_loaded_n})"
                )
        if parts:
            lines.append("Utility: " + " | ".join(parts))

    eviction = metadata.get("eviction") if isinstance(metadata, dict) else None
    if isinstance(eviction, dict):
        would_evict_count = int(eviction.get("would_evict_count") or 0)
        would_evict = eviction.get("would_evict")
        if would_evict_count > 0 and isinstance(would_evict, list):
            parts: list[str] = []
            for item in would_evict:
                if not isinstance(item, dict):
                    continue
                skill_id = str(item.get("skill_id") or "").strip()
                task_kind = str(item.get("task_kind") or "").strip()
                reason = str(item.get("reason") or "").strip()
                if not skill_id:
                    continue
                parts.append(f"{skill_id} terrain={task_kind} reason={reason}")
            if parts:
                lines.append(
                    f"Eviction (dry-run): would withhold {would_evict_count}: "
                    + " | ".join(parts)
                )

    return lines


def build_context_text(
    role: RuntimeRole | str,
    workspace_root: Path | None,
    *,
    force: bool = False,
    model: str | None = None,
    task_kind: str | None = None,
    target_files: tuple[str, ...] | None = None,
    content: str | None = None,
) -> ComposedContext:
    _ = model
    runtime_role = RuntimeRole.from_value(role)
    parts: list[str] = []
    ledger: list[ContextLedgerEntry] = []
    normalized_target_files = tuple(target_files or ())
    for source in iter_registered_sources():
        text, entry, extra_entries = collect_source_text(
            source,
            runtime_role,
            workspace_root,
            force=force,
            task_kind=task_kind,
            target_files=normalized_target_files,
            content=content,
        )
        if text:
            parts.append(text)
        ledger.append(entry)
        ledger.extend(extra_entries)
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
    content: str | None = None,
) -> ComposedContext:
    runtime_role = RuntimeRole.from_value(role)
    context = build_context_text(
        runtime_role,
        workspace_root,
        force=force,
        model=model,
        task_kind=task_kind,
        target_files=target_files,
        content=content,
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
