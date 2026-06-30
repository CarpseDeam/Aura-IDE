"""Initial scoped context source registry."""
from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from aura.context_gearbox.models import ContextLedgerEntry, ContextSource, RuntimeRole
from aura.repo_map import generate_repo_map
from aura.skills.text import build_skill_context_with_ids

CORE_KERNEL_TEXT = """Core kernel:
- Work inside the selected workspace.
- Read files before making claims about repository contents.
- Keep the response and any changes scoped to the user's request."""

PLANNER_DISPATCH_CONTRACT = """### planner_dispatch_contract
- Choose the lane: answer, ask, inspect, dispatch, or use a capability.
- Read before making repository claims.
- For code changes, dispatch once the requested change is clear enough to execute.
- Clear enough means goal, target seam/files, constraints/non-goals, and acceptance are known; exact implementation details are Worker-owned.
- Do not spend another turn narrating, comparing approaches, or expanding a plan after the Worker capsule is actionable.
- If the user accepts a previously proposed actionable phase, bind the acceptance to the most recent actionable phase and call dispatch_to_worker.
- Treat "do phase 1", "start phase 1", "yes do that", "go", "run it", and "let's do it" as acceptance when a recent actionable phase exists.
- Do not say you will start extracting, editing, or refactoring in Planner mode; the Planner dispatches and the Worker implements.
- Planner owns the Worker capsule only. Worker owns exact reads, edits, validation, and final proof.
- Ask one focused question when blocked.
- Do not over-plan simple work.
- Once goal, files/seam, constraints/non-goals, and acceptance are known, dispatch; do not keep inspecting.
- If an accepted phase lacks target files or acceptance, Planner may do at most one narrow read/search to complete the capsule.
- Large-file helper extraction/refactor dispatches must be sliced before Worker starts.
- Do not dispatch broad refactor capsules like "extract 880 lines into 3 modules"; dispatch the first concrete slice only.
- A first slice means one helper family, one new module, one source-file wireback, and one validation command. Later slices are follow-up after the first slice lands.
- Acceptance should prove the first slice only.
- Worker specs need exact goal, known files, acceptance, validation, and non-goals.
- Fill structured contract fields when knowable: expected_public_symbols, expected_dataclass_fields, forbidden_calls, forbidden_public_methods, and non_goals.
- If those fields are known, call dispatch_to_worker instead of explaining the plan in chat."""

WEB_RESEARCH_RULES = """### web_research_rules
- Use run_read_only_drone with drone_id "web-research" for latest/current facts, external docs/API examples, pricing, versions/releases/changelogs, schedules, current people/roles, error lookup, URLs, and external references.
- Do not use web research for local repo, file, workspace, git, or ordinary coding tasks unless the user explicitly needs current external facts.
- Pure research answers must use the sourced research result and must not dispatch Worker.
- For hybrid research-plus-code, Planner should research first and dispatch Worker only after findings create a concrete code objective with target files and acceptance.
- Keep chat output compact and sourced; keep raw metadata in tool logs."""

WORKER_EXECUTION_CONTRACT = """### worker_execution_contract
- Execute only the requested change.
- Read relevant files before editing, but prefer targeted reads around the named seam over broad orientation.
- Once the target and local facts are clear, edit. Do not keep restating plans, comparing approaches, or rebuilding the full picture.
- Make the smallest safe change that satisfies the task; preserve existing behavior unless the request changes it.
- Avoid broad rewrites and keep diffs tight.
- Update tests only when relevant.
- Validate focused behavior after writes when practical.
- Report changed files, validation, and proof compactly."""

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

GUI_RULES = """### gui_rules
- Preserve existing signal wiring and data flow.
- Use existing theme tokens, styles, and components before adding new UI paths.
- Keep widget and layout edits narrow; avoid broad rewrites.
- Validate UI-adjacent changes with focused tests or selfcheck when practical.
- Do not invent parallel UI paths when an existing seam exists."""

DRONE_RULES = """### drone_rules
- Keep the run loop owned by the card or harness.
- Keep runs bounded and preserve structured receipts.
- Do not create a duplicate canonical drone folder.
- Preserve run history and existing receipt behavior.
- Do not mix drone runtime changes with unrelated UI polish."""

PROVIDER_RULES = """### provider_rules
- Never leak keys, tokens, or provider secrets.
- Preserve provider selection and settings behavior.
- Keep BYOK, Aura Credits, and OpenRouter paths distinct.
- Surface provider errors cleanly without hiding useful diagnostics.
- Do not hardcode pricing or model lists unless explicitly required."""

BUILD_PIPELINE_RULES = """### build_pipeline_rules
- Do not break the Nuitka or installer flow.
- Keep build scripts Windows-safe.
- Avoid unrelated dependency churn.
- Validate compile assumptions before packaging assumptions.
- Preserve bundled resource handling."""

_CONTRACT_TEXT: dict[str, str] = {
    "planner_dispatch_contract": PLANNER_DISPATCH_CONTRACT,
    "worker_execution_contract": WORKER_EXECUTION_CONTRACT,
    "code_quality_contract": CODE_QUALITY_CONTRACT,
    "validation_selection_contract": VALIDATION_SELECTION_CONTRACT,
    "receipt_contract": RECEIPT_CONTRACT,
}

_SCOPED_PACK_TEXT: dict[str, str] = {
    "gui_rules": GUI_RULES,
    "drone_rules": DRONE_RULES,
    "provider_rules": PROVIDER_RULES,
    "build_pipeline_rules": BUILD_PIPELINE_RULES,
}

_PLANNER_RESEARCH_PACK_TEXT: dict[str, str] = {
    "web_research_rules": WEB_RESEARCH_RULES,
}

_CODING_TASK_KINDS = {
    "new tool or app",
    "bugfix",
    "gui polish",
    "cleanup",
    "refactor",
}


@dataclass(frozen=True)
class _ScopedPackRule:
    scope_name: str
    path_prefixes: tuple[str, ...]
    path_globs: tuple[str, ...]
    task_hints: tuple[str, ...]


_SCOPED_PACK_RULES: dict[str, _ScopedPackRule] = {
    "gui_rules": _ScopedPackRule(
        scope_name="gui",
        path_prefixes=(
            "aura/gui/",
            "aura/assets/",
        ),
        path_globs=(
            "media/ui/**",
            "media/ui_assets/**",
            "media/**/ui/**",
            "media/**/*ui*",
        ),
        task_hints=(
            "gui",
            "ui",
            "polish",
            "window",
            "widget",
            "dialog",
            "button",
            "layout",
            "screen",
            "theme",
        ),
    ),
    "drone_rules": _ScopedPackRule(
        scope_name="drone",
        path_prefixes=(
            "aura/drones/",
            "aura/gui/drone",
            "drones/",
            "bundled_drones/",
        ),
        path_globs=(
            "**/drone_manifest*.json",
            "**/drone_manifests/**",
            "**/drone_templates/**",
            "**/drone*/templates/**",
        ),
        task_hints=(
            "drone",
            "loop",
            "capability runner",
            "runner",
            "receipt",
            "bounded run",
        ),
    ),
    "provider_rules": _ScopedPackRule(
        scope_name="provider",
        path_prefixes=(
            "aura/providers/",
            "aura/backends/",
            "aura/client/",
        ),
        path_globs=(
            "aura/**/*provider*settings*.py",
            "aura/**/*settings*provider*.py",
            "aura/**/*provider*config*.py",
            "aura/**/*config*provider*.py",
        ),
        task_hints=(
            "model",
            "provider",
            "api key",
            "api keys",
            "apikey",
            "credits",
            "aura credits",
            "byok",
            "openrouter",
            "openai",
            "deepseek",
        ),
    ),
    "build_pipeline_rules": _ScopedPackRule(
        scope_name="build",
        path_prefixes=(
            "installer/",
            "packaging/",
        ),
        path_globs=(
            "scripts/build_*.py",
            "scripts/*nuitka*.py",
            "scripts/*package*.py",
            "pyproject.toml",
            "requirements*.txt",
            "**/nuitka/**",
            "**/installer/**",
            "**/packaging/**",
        ),
        task_hints=(
            "build",
            "release",
            "installer",
            "package",
            "packaging",
            "nuitka",
            "compile",
            "executable",
            "pyproject",
            "requirements",
            "dependency",
        ),
    ),
}

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
        source_id="web_research_rules",
        kind="planner_research_pack",
        roles=(RuntimeRole.PLANNER,),
        reason="turn is shaped as external web research",
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
    ContextSource(
        source_id="gui_rules",
        kind="scoped_coding_pack",
        roles=(RuntimeRole.WORKER, RuntimeRole.SINGLE),
        reason="target files or task kind match GUI scope",
    ),
    ContextSource(
        source_id="drone_rules",
        kind="scoped_coding_pack",
        roles=(RuntimeRole.WORKER, RuntimeRole.SINGLE),
        reason="target files or task kind match drone scope",
    ),
    ContextSource(
        source_id="provider_rules",
        kind="scoped_coding_pack",
        roles=(RuntimeRole.WORKER, RuntimeRole.SINGLE),
        reason="target files or task kind match provider scope",
    ),
    ContextSource(
        source_id="build_pipeline_rules",
        kind="scoped_coding_pack",
        roles=(RuntimeRole.WORKER, RuntimeRole.SINGLE),
        reason="target files or task kind match build scope",
    ),
    ContextSource(
        source_id="skill_pack",
        kind="skill_pack",
        roles=(RuntimeRole.PLANNER, RuntimeRole.WORKER),
        reason="terrain-selected skills for this context",
    ),
)


def iter_registered_sources() -> tuple[ContextSource, ...]:
    return CONTEXT_SOURCES


def collect_source_text(
    source: ContextSource,
    role: RuntimeRole,
    workspace_root: Path | None,
    *,
    force: bool = False,
    task_kind: str | None = None,
    target_files: tuple[str, ...] | None = None,
    content: str | None = None,
) -> tuple[str, ContextLedgerEntry, list[ContextLedgerEntry]]:
    try:
        skill_ids: list[str] = []
        if source.kind == "skill_pack":
            if role not in source.roles:
                text, reason = "", f"not scoped to {role.value} role"
            else:
                text, reason, skill_ids = _load_skill_pack(
                    workspace_root,
                    task_kind,
                    target_files,
                    content,
                )
        else:
            text, reason = _load_source_text(
                source,
                workspace_root,
                force=force,
                role=role,
                task_kind=task_kind,
                target_files=target_files,
                content=content,
            )
        included = bool(text)
        entry = ContextLedgerEntry(
            source_id=source.source_id,
            kind=source.kind,
            role=role,
            reason=reason or source.reason,
            included=included,
            char_count=len(text),
        )

        # For skill_pack, include per-skill ledger entries
        extra: list[ContextLedgerEntry] = []
        if source.kind == "skill_pack" and workspace_root is not None:
            extra = [
                ContextLedgerEntry(
                    source_id=sid,
                    kind="individual_skill",
                    role=role,
                    reason="individual skill in terrain-selected pack",
                    included=included,
                    char_count=0,
                )
                for sid in skill_ids
            ]

        return text, entry, extra
    except Exception as exc:
        return "", ContextLedgerEntry(
            source_id=source.source_id,
            kind=source.kind,
            role=role,
            reason=source.reason,
            included=False,
            char_count=0,
            error=f"{type(exc).__name__}: {exc}",
        ), []


def _load_source_text(
    source: ContextSource,
    workspace_root: Path | None,
    *,
    force: bool,
    role: RuntimeRole,
    task_kind: str | None,
    target_files: tuple[str, ...] | None,
    content: str | None,
) -> tuple[str, str]:
    if role not in source.roles:
        return "", f"not scoped to {role.value} role"
    if source.kind == "quality_contract":
        return _load_quality_contract(source, role, task_kind, target_files)
    if source.kind == "planner_research_pack":
        return _load_planner_research_pack(source, role, task_kind)
    if source.kind == "scoped_coding_pack":
        return _load_scoped_coding_pack(
            source,
            workspace_root,
            role,
            task_kind,
            target_files,
        )
    if source.kind == "skill_pack":
        text, reason, _skill_ids = _load_skill_pack(
            workspace_root,
            task_kind,
            target_files,
            content,
        )
        return text, reason
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


def _load_planner_research_pack(
    source: ContextSource,
    role: RuntimeRole,
    task_kind: str | None,
) -> tuple[str, str]:
    text = _PLANNER_RESEARCH_PACK_TEXT.get(source.source_id, "")
    if not text:
        return "", "unknown planner research pack"
    if role != RuntimeRole.PLANNER:
        return "", f"not scoped to {role.value} role"
    if not _task_kind_is_research_shaped(task_kind):
        return "", "turn is not research-shaped"
    return text, source.reason


def _load_scoped_coding_pack(
    source: ContextSource,
    workspace_root: Path | None,
    role: RuntimeRole,
    task_kind: str | None,
    target_files: tuple[str, ...] | None,
) -> tuple[str, str]:
    text = _SCOPED_PACK_TEXT.get(source.source_id, "")
    rule = _SCOPED_PACK_RULES.get(source.source_id)
    if not text or rule is None:
        return "", "unknown scoped coding pack"
    normalized_targets = _normalize_target_file_paths(target_files, workspace_root)
    if role == RuntimeRole.SINGLE and not _single_scoped_pack_applies(
        task_kind,
        normalized_targets,
    ):
        return "", "single task is not coding-shaped"
    if not _scoped_pack_matches(rule, normalized_targets, task_kind):
        return "", f"target files do not match {rule.scope_name} scope"
    return text, source.reason


def _load_skill_pack(
    workspace_root: Path | None,
    task_kind: str | None,
    target_files: tuple[str, ...] | None,
    content: str | None,
) -> tuple[str, str, list[str]]:
    if workspace_root is None:
        return "", "no workspace root", []
    text, skill_ids = build_skill_context_with_ids(
        workspace_root,
        task_kind=task_kind,
        target_files=tuple(target_files or ()),
        content=content,
    )
    if text:
        return text, "terrain-selected skills for this context", skill_ids
    return "", "no skills matched for this terrain", []


def _single_contract_applies(
    task_kind: str | None,
    target_files: tuple[str, ...] | None,
) -> bool:
    if _is_coding_task_kind(task_kind):
        return True
    return bool(target_files)


def _single_scoped_pack_applies(
    task_kind: str | None,
    normalized_targets: tuple[str, ...],
) -> bool:
    if normalized_targets:
        return True
    if _is_coding_task_kind(task_kind):
        return True
    return _task_kind_matches_any_scoped_hint(task_kind)


def _scoped_pack_matches(
    rule: _ScopedPackRule,
    normalized_targets: tuple[str, ...],
    task_kind: str | None,
) -> bool:
    if any(_path_matches_rule(path, rule) for path in normalized_targets):
        return True
    return _task_kind_has_any_hint(task_kind, rule.task_hints)


def _normalize_target_file_paths(
    target_files: tuple[str, ...] | None,
    workspace_root: Path | None,
) -> tuple[str, ...]:
    root = _normalize_path_string(workspace_root) if workspace_root is not None else ""
    normalized: list[str] = []
    for raw_path in target_files or ():
        path = _normalize_path_string(raw_path)
        if not path:
            continue
        if root:
            root_prefix = root.rstrip("/") + "/"
            if path.startswith(root_prefix):
                path = path[len(root_prefix):]
        while path.startswith("./"):
            path = path[2:]
        path = path.lstrip("/")
        if path:
            normalized.append(path)
    return tuple(normalized)


def _normalize_path_string(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/").lower()
    while "//" in text:
        text = text.replace("//", "/")
    return text


def _path_matches_rule(path: str, rule: _ScopedPackRule) -> bool:
    return _path_matches_prefixes(path, rule.path_prefixes) or _path_matches_globs(
        path,
        rule.path_globs,
    )


def _path_matches_prefixes(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(path.startswith(prefix) for prefix in prefixes)


def _path_matches_globs(path: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatchcase(path, pattern) for pattern in patterns)


def _task_kind_matches_any_scoped_hint(task_kind: str | None) -> bool:
    return any(
        _task_kind_has_any_hint(task_kind, rule.task_hints)
        for rule in _SCOPED_PACK_RULES.values()
    )


def _task_kind_has_any_hint(task_kind: str | None, hints: tuple[str, ...]) -> bool:
    normalized = _normalize_task_kind(task_kind)
    if not normalized:
        return False
    compact = normalized.replace(" ", "")
    return any(
        hint in normalized or hint.replace(" ", "") in compact
        for hint in hints
        if hint
    )


def _is_coding_task_kind(task_kind: str | None) -> bool:
    return _normalize_task_kind(task_kind) in _CODING_TASK_KINDS


def _task_kind_is_research_shaped(task_kind: str | None) -> bool:
    normalized = _normalize_task_kind(task_kind)
    return normalized in {
        "answer only",
        "research",
        "web research",
        "research then worker",
    }


def _normalize_task_kind(value: Any) -> str:
    return " ".join(
        str(value or "")
        .strip()
        .lower()
        .replace("-", " ")
        .replace("_", " ")
        .split()
    )
