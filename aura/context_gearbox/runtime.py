"""Runtime prompt and context composition."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterable

_log = logging.getLogger(__name__)

from aura.context_gearbox.models import ComposedContext, ContextLedgerEntry, RuntimeRole
from aura.context_gearbox.sources import collect_source_text, iter_registered_sources

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


def context_gearbox_metadata(
    entries: Iterable[ContextLedgerEntry],
    *,
    workspace_root: Path | None = None,
) -> dict[str, Any]:
    """Return the inspectable Context Gearbox payload without prompt text.

    When *workspace_root* is provided and the resulting utility dict is
    non-empty, includes a ``"utility"`` key with per-source utility data.
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
                metadata["utility"] = utility
        except Exception:
            _log.exception("Failed to derive source utility (degrading)")
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
            if u.status == "measured" and u.lift is not None:
                sign = "+" if u.lift >= 0 else ""
                parts.append(
                    f"{source_id} {sign}{u.lift:.1%} "
                    f"(loaded={u.loaded_n}, not_loaded={u.not_loaded_n})"
                )
            else:
                parts.append(
                    f"{source_id} insufficient "
                    f"(loaded={u.loaded_n}, not_loaded={u.not_loaded_n})"
                )
        if parts:
            lines.append("Utility: " + " | ".join(parts))

    return lines


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
    for source in iter_registered_sources():
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
