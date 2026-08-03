"""Runtime prompt and context composition."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterable

_log = logging.getLogger(__name__)

from aura.context_gearbox.models import (
    ComposedContext,
    ContextLedgerEntry,
    CustomPromptDiagnostics,
    RuntimeRole,
)
from aura.context_gearbox.sources import collect_source_text, iter_registered_sources
from aura.roles import load_bundled_role_capsule

CONTEXT_PLACEHOLDER = "{TIER1_CONTEXT}"

# Documented opt-in for full replacement. A custom prompt containing this
# marker owns the whole system prompt; every other custom prompt is an
# additive extension of Aura's canonical context and role instructions.
FULL_REPLACEMENT_MARKER = "{AURA_REPLACE_CANONICAL_PROMPT}"

CUSTOM_PROMPT_HEADER = "### Custom Instructions"

_RESPONSE_DISCIPLINE = """Response discipline:
- Lead with the answer, decision, or next action.
- Default to concise, useful replies.
- Avoid essays, tutorials, and multi-section breakdowns unless the user asks for depth.
- Normal chat should usually be 1-4 short paragraphs or up to 5 bullets.
- Give full detail when the user asks or when missing detail would make the answer unsafe or unusable."""

def _role_prompt_text(runtime_role: RuntimeRole) -> str:
    capsule = load_bundled_role_capsule(runtime_role)
    if capsule is None:
        raise RuntimeError(f"No bundled role capsule for {runtime_role.value}")
    return capsule.content


def default_role_prompt(role: RuntimeRole | str) -> str:
    runtime_role = RuntimeRole.from_value(role)
    return "\n\n".join(
        [CONTEXT_PLACEHOLDER, _RESPONSE_DISCIPLINE, _role_prompt_text(runtime_role)]
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
    active_capabilities: frozenset[str] | None = None,
) -> ComposedContext:
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
            model=model,
            task_kind=task_kind,
            target_files=normalized_target_files,
            content=content,
            active_capabilities=active_capabilities,
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
    active_capabilities: frozenset[str] | None = None,
) -> ComposedContext:
    """Compose the one canonical system prompt for *role*.

    Core context, response discipline, and the role capsule always survive.
    A custom prompt is an additive extension appended under
    ``### Custom Instructions``; text it repeats verbatim from the canonical
    blocks is dropped so nothing is injected twice.  Full replacement is only
    available by opting in with ``FULL_REPLACEMENT_MARKER``.

    ``active_capabilities`` names the extensible surfaces connected *right
    now*, so a capability pack is composed in for exactly the requests whose
    tool list actually carries those tools.
    """
    runtime_role = RuntimeRole.from_value(role)
    context = build_context_text(
        runtime_role,
        workspace_root,
        force=force,
        model=model,
        task_kind=task_kind,
        target_files=target_files,
        content=content,
        active_capabilities=active_capabilities,
    )
    custom = (custom_prompt or "").strip()
    if custom and FULL_REPLACEMENT_MARKER in custom:
        template = custom.replace(FULL_REPLACEMENT_MARKER, "").strip()
        system_prompt = template.replace(
            CONTEXT_PLACEHOLDER, context.context_text, 1
        ).strip()
    else:
        system_prompt = _compose_canonical_prompt(
            runtime_role,
            context.context_text,
            custom,
        )
    return ComposedContext(
        role=runtime_role,
        system_prompt=system_prompt,
        context_text=context.context_text,
        ledger=context.ledger,
    )


def _compose_canonical_prompt(
    runtime_role: RuntimeRole,
    context_text: str,
    custom: str,
) -> str:
    """Build canonical context + role prompt, then append custom extras."""
    blocks = [context_text, _RESPONSE_DISCIPLINE, _role_prompt_text(runtime_role)]
    parts = [block.strip() for block in blocks if block and block.strip()]
    extension = _custom_prompt_extension(custom, blocks)
    if extension:
        parts.append(f"{CUSTOM_PROMPT_HEADER}\n{extension}")
    return "\n\n".join(parts)


def _custom_prompt_extension(custom: str, canonical_blocks: list[str]) -> str:
    """Return only the part of *custom* that canonical composition lacks.

    Custom prompts are usually an edited copy of the default role prompt, so
    the placeholder and any verbatim canonical block are removed before the
    remainder is appended.
    """
    if not custom:
        return ""
    text = custom.replace(CONTEXT_PLACEHOLDER, "")
    for block in canonical_blocks:
        stripped = block.strip()
        if stripped:
            text = text.replace(stripped, "")
    lines = [line.rstrip() for line in text.splitlines()]
    collapsed: list[str] = []
    for line in lines:
        if not line and (not collapsed or not collapsed[-1]):
            continue
        collapsed.append(line)
    return "\n".join(collapsed).strip()


# Vocabulary from the retired Planner/Worker product. A custom prompt is
# usually an edited copy of an older default, so these survive long after the
# runtime stopped having two coding models.
_LEGACY_ROLE_TERMS: tuple[str, ...] = (
    "planner",
    "worker",
    "dispatch_to_worker",
    "dispatch to worker",
    "work artifact",
    "sub-agent",
    "subagent",
)

# Concepts the canonical prompt already owns, and phrasings that indicate a
# custom prompt is re-stating one. Exact-block removal cannot catch these —
# they are paraphrases, which is precisely why they need to be visible.
_CANONICAL_CONCEPT_MARKERS: dict[str, tuple[str, ...]] = {
    "read before claiming (core kernel)": (
        "read the file",
        "read files before",
        "before making claims",
        "do not describe the repository from memory",
    ),
    "scope discipline (core kernel)": (
        "stay in scope",
        "do not expand scope",
        "scope creep",
        "only what was asked",
    ),
    "response length (response discipline)": (
        "be concise",
        "keep it short",
        "avoid essays",
        "lead with the answer",
    ),
    "anti-circling (production capsule)": (
        "do not circle",
        "do not overthink",
        "act quickly",
        "stop searching",
        "one or two tool calls",
    ),
    "live todo (production capsule)": (
        "update_worker_todo",
        "checklist",
        "todo list",
    ),
    "validation selection (validation_selection_contract)": (
        "run the tests",
        "py_compile",
        "focused test",
        "validate the change",
    ),
    "receipt honesty (receipt_contract)": (
        "list changed files",
        "do not claim",
        "final receipt",
        "summarize what changed",
    ),
    "code quality (code_quality_contract)": (
        "no placeholders",
        "no stubs",
        "match local style",
        "do not invent",
    ),
}


def diagnose_custom_prompt(
    role: RuntimeRole | str,
    custom_prompt: str | None,
) -> CustomPromptDiagnostics:
    """Describe what a custom prompt adds on top of the canonical one.

    Reports size, whether it opted into full replacement, retired
    Planner/Worker vocabulary, and canonical concepts it appears to restate.
    Purely observational: composition is unchanged by anything measured here.
    """
    runtime_role = RuntimeRole.from_value(role)
    custom = (custom_prompt or "").strip()
    if not custom:
        return CustomPromptDiagnostics(
            char_count=0,
            appended_char_count=0,
            full_replacement=False,
            legacy_terms=(),
            repeated_concepts=(),
        )

    full_replacement = FULL_REPLACEMENT_MARKER in custom
    if full_replacement:
        # The custom prompt is the whole prompt, so all of it reaches the model
        # and "repeats a canonical block" is not a meaningful question.
        appended = custom.replace(FULL_REPLACEMENT_MARKER, "").strip()
        repeated: tuple[str, ...] = ()
    else:
        canonical_blocks = [
            CONTEXT_PLACEHOLDER,
            _RESPONSE_DISCIPLINE,
            _role_prompt_text(runtime_role),
        ]
        appended = _custom_prompt_extension(custom, canonical_blocks)
        repeated = _repeated_canonical_concepts(appended)

    return CustomPromptDiagnostics(
        char_count=len(custom),
        appended_char_count=len(appended),
        full_replacement=full_replacement,
        legacy_terms=_legacy_role_terms(custom),
        repeated_concepts=repeated,
    )


# Current tool names that contain retired role vocabulary. Stripped before the
# legacy scan so a prompt that correctly names today's TODO tool is not
# reported as carrying Planner/Worker language.
_CURRENT_NAMES_WITH_LEGACY_WORDS: tuple[str, ...] = (
    "update_worker_todo",
    "worker_todo",
)


def _legacy_role_terms(text: str) -> tuple[str, ...]:
    lowered = text.lower()
    for name in _CURRENT_NAMES_WITH_LEGACY_WORDS:
        lowered = lowered.replace(name, "")
    return tuple(term for term in _LEGACY_ROLE_TERMS if term in lowered)


def _repeated_canonical_concepts(appended: str) -> tuple[str, ...]:
    lowered = appended.lower()
    return tuple(
        concept
        for concept, markers in _CANONICAL_CONCEPT_MARKERS.items()
        if any(marker in lowered for marker in markers)
    )


def format_custom_prompt_diagnostics(
    diagnostics: CustomPromptDiagnostics,
    *,
    effective_prompt_chars: int | None = None,
) -> str:
    """One compact line for the settings page and the composition log.

    ASCII only: this also goes to log handlers, which on Windows may be
    encoding with the console code page rather than UTF-8.
    """
    if diagnostics.is_empty:
        line = "Custom prompt: none (using the built-in default)."
        if effective_prompt_chars is not None:
            line += f" Effective system prompt: {effective_prompt_chars:,} chars."
        return line

    parts = [f"Custom prompt: {diagnostics.char_count:,} chars"]
    if diagnostics.full_replacement:
        parts.append("replaces the canonical prompt entirely")
    else:
        parts.append(f"{diagnostics.appended_char_count:,} appended after de-duplication")
    if diagnostics.legacy_terms:
        parts.append("legacy Planner/Worker terms: " + ", ".join(diagnostics.legacy_terms))
    if diagnostics.repeated_concepts:
        parts.append("may restate: " + "; ".join(diagnostics.repeated_concepts))
    if effective_prompt_chars is not None:
        parts.append(f"effective system prompt {effective_prompt_chars:,} chars")
    return " | ".join(parts) + "."
