"""Initial scoped context source registry."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from aura.context_gearbox.models import ContextLedgerEntry, ContextSource, RuntimeRole
from aura.repo_map import generate_repo_map

CORE_KERNEL_TEXT = """Core kernel:
- Work inside the selected workspace.
- Read files before making claims about repository contents.
- Keep the response and any changes scoped to the user's request."""

PLANNER_DISPATCH_CONTRACT = """### planner_dispatch_contract
- Choose the lane: answer, ask, inspect, dispatch, or use a capability.
- Read before making repository claims.
- Dispatch only when the requested change is clear enough to execute.
- Ask one focused question when blocked.
- Do not over-plan simple work.
- Worker specs need exact goal, known files, acceptance, validation, and non-goals."""

WORKER_EXECUTION_CONTRACT = """### worker_execution_contract
- Execute only the requested change.
- Read relevant files before editing.
- Preserve existing behavior unless the request changes it.
- Avoid broad rewrites and keep diffs tight.
- Update tests only when relevant.
- Validate focused behavior and report changed files, validation, and proof."""

CODE_QUALITY_CONTRACT = """### code_quality_contract
- Keep functions and modules small where practical.
- Avoid god-file growth unless the local design leaves no better option.
- Do not duplicate logic when a clean helper exists.
- Use clear names and minimal side effects.
- Handle expected failure paths explicitly.
- Do not perform unrelated cleanup."""

VALIDATION_SELECTION_CONTRACT = """### validation_selection_contract
- Compile touched Python files when relevant.
- Run focused tests for touched behavior.
- Run broader tests only when the touched area justifies it.
- Do not spend time on irrelevant tests.
- Report skipped validation honestly and compactly."""

RECEIPT_CONTRACT = """### receipt_contract
- Final receipts list changed files, what changed, validation run, and result.
- Include risks or follow-up only when real.
- Keep the final answer compact.
- Do not ramble beyond the proof the user needs."""

_CONTRACT_TEXT: dict[str, str] = {
    "planner_dispatch_contract": PLANNER_DISPATCH_CONTRACT,
    "worker_execution_contract": WORKER_EXECUTION_CONTRACT,
    "code_quality_contract": CODE_QUALITY_CONTRACT,
    "validation_selection_contract": VALIDATION_SELECTION_CONTRACT,
    "receipt_contract": RECEIPT_CONTRACT,
}

_CODING_TASK_KINDS = {
    "new_tool_or_app",
    "bugfix",
    "gui_polish",
    "cleanup",
    "refactor",
}

GEARBOX_REBUILD_TODO = (
    "prompt source registry",
    "scoped context sources",
    "prompt source ledger",
    "deterministic current-info source gate",
    "Worker run ledger / acceptance proof ledger",
)

CONTEXT_SOURCES: tuple[ContextSource, ...] = (
    ContextSource(
        source_id="core_kernel",
        kind="kernel",
        roles=(RuntimeRole.PLANNER, RuntimeRole.WORKER, RuntimeRole.SINGLE),
        reason="baseline runtime posture",
    ),
    ContextSource(
        source_id="project_rules",
        kind="workspace_file",
        roles=(RuntimeRole.PLANNER, RuntimeRole.WORKER, RuntimeRole.SINGLE),
        reason="explicit project rules file",
    ),
    ContextSource(
        source_id="repo_map",
        kind="workspace_structure",
        roles=(RuntimeRole.PLANNER, RuntimeRole.WORKER, RuntimeRole.SINGLE),
        reason="repository structure overview",
    ),
    ContextSource(
        source_id="planner_dispatch_contract",
        kind="quality_contract",
        roles=(RuntimeRole.PLANNER,),
        reason="planner coding-harness dispatch quality contract",
    ),
    ContextSource(
        source_id="worker_execution_contract",
        kind="quality_contract",
        roles=(RuntimeRole.WORKER,),
        reason="worker coding-harness execution quality contract",
    ),
    ContextSource(
        source_id="code_quality_contract",
        kind="quality_contract",
        roles=(RuntimeRole.WORKER, RuntimeRole.SINGLE),
        reason="coding quality contract for implementation work",
    ),
    ContextSource(
        source_id="validation_selection_contract",
        kind="quality_contract",
        roles=(RuntimeRole.WORKER, RuntimeRole.SINGLE),
        reason="focused validation selection contract",
    ),
    ContextSource(
        source_id="receipt_contract",
        kind="quality_contract",
        roles=(RuntimeRole.WORKER, RuntimeRole.SINGLE),
        reason="compact final receipt contract",
    ),
)


def iter_context_sources(role: RuntimeRole) -> tuple[ContextSource, ...]:
    _ = role
    return CONTEXT_SOURCES


def collect_source_text(
    source: ContextSource,
    role: RuntimeRole,
    workspace_root: Path | None,
    *,
    force: bool = False,
    task_kind: str | None = None,
    target_files: tuple[str, ...] | None = None,
) -> tuple[str, ContextLedgerEntry]:
    try:
        text, reason = _load_source_text(
            source,
            workspace_root,
            force=force,
            role=role,
            task_kind=task_kind,
            target_files=target_files,
        )
        included = bool(text)
        return text, ContextLedgerEntry(
            source_id=source.source_id,
            kind=source.kind,
            role=role,
            reason=reason or source.reason,
            included=included,
            char_count=len(text),
        )
    except Exception as exc:
        return "", ContextLedgerEntry(
            source_id=source.source_id,
            kind=source.kind,
            role=role,
            reason=source.reason,
            included=False,
            char_count=0,
            error=f"{type(exc).__name__}: {exc}",
        )


def _load_source_text(
    source: ContextSource,
    workspace_root: Path | None,
    *,
    force: bool,
    role: RuntimeRole,
    task_kind: str | None,
    target_files: tuple[str, ...] | None,
) -> tuple[str, str]:
    if role not in source.roles:
        return "", f"not scoped to {role.value} role"
    if source.kind == "quality_contract":
        return _load_quality_contract(source, role, task_kind, target_files)
    if source.source_id == "core_kernel":
        return CORE_KERNEL_TEXT, source.reason
    if workspace_root is None:
        return "", "no workspace root"
    if source.source_id == "project_rules":
        rules_path = workspace_root / "project_rules.md"
        if not rules_path.is_file():
            return "", "project_rules.md not found"
        try:
            rules = rules_path.read_text(encoding="utf-8").strip()
        except (OSError, PermissionError) as exc:
            raise RuntimeError(f"project_rules.md unavailable: {exc}") from exc
        if not rules:
            return "", "project_rules.md is empty"
        return "### Project Rules\n" + rules, source.reason
    if source.source_id == "repo_map":
        repo_map = generate_repo_map(workspace_root, force=force)
        if not repo_map:
            return "", "repo map unavailable"
        if "No Python/TypeScript files found." in repo_map:
            return "", "no Python/TypeScript files found"
        return repo_map, source.reason
    return "", "unknown context source"


def _load_quality_contract(
    source: ContextSource,
    role: RuntimeRole,
    task_kind: str | None,
    target_files: tuple[str, ...] | None,
) -> tuple[str, str]:
    text = _CONTRACT_TEXT.get(source.source_id, "")
    if not text:
        return "", "unknown quality contract"
    if role == RuntimeRole.SINGLE and not _single_contract_applies(task_kind, target_files):
        return "", "single-mode request has no coding task shape"
    return text, source.reason


def _single_contract_applies(
    task_kind: str | None,
    target_files: tuple[str, ...] | None,
) -> bool:
    if _normalize_task_kind(task_kind) in _CODING_TASK_KINDS:
        return True
    return bool(target_files)


def _normalize_task_kind(value: Any) -> str:
    return str(value or "").strip().lower()
