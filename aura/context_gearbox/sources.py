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
- Use dispatch_to_worker for code or file implementation.
- Do not call write tools directly.
- Dispatch only when the goal, files or scope, acceptance, and non-goals are clear enough for Worker execution.
- Broad, multi-file, high-risk, or multi-stage work must be one visible campaign with ordered Worker steps.
- Each Worker step must be bounded and independently executable.
- Include clear files, scope, acceptance, validation, and non-goals where knowable.
- Do not dispatch vague "fix everything" tasks.
- Ask the user only for genuinely user-owned decisions.
- Preserve existing architecture and avoid unnecessary scope expansion.
- Fill structured contract fields when knowable: expected_public_symbols, expected_dataclass_fields, forbidden_calls, forbidden_public_methods, and non_goals."""

WEB_RESEARCH_RULES = """### web_research_rules
- Use run_read_only_drone with drone_id "web-research" for latest/current facts, external docs/API examples, pricing, versions/releases/changelogs, schedules, current people/roles, error lookup, URLs, and external references.
- Do not use web research for local repo, file, workspace, git, or ordinary coding tasks unless the user explicitly needs current external facts.
- Pure research answers must use the sourced research result and must not dispatch Worker.
- For hybrid research-plus-code, Planner should research first and dispatch Worker only after findings create a concrete code objective with target files and acceptance.
- Keep chat output compact and sourced; keep raw metadata in tool logs."""

WORKER_EXECUTION_CONTRACT = """### worker_execution_contract
- Implement only the dispatched spec and current step.
- If the task is part of a campaign, DispatchSession starts each step sequentially; Worker must not plan or schedule the whole campaign.
- Do not ask the user to resolve implementation ambiguity; use repository facts, recover internally, or return a blocker.
- Inspect live files with tools before editing.
- Use patch_file for existing-file edits unless full replacement is explicitly justified.
- Keep changes inside assigned files and scope unless the spec explicitly allows expansion.
- Do not fabricate files, APIs, validation results, or receipts.
- If required context or files are missing, stop and return a clear blocker with the next useful action."""

CODE_QUALITY_CONTRACT = """### code_quality_contract
- Code must compile or parse when applicable.
- Do not leave placeholders, stubs, incomplete implementations, or fake fallbacks.
- Preserve public behavior unless the spec requires a change.
- The current contents of your target files are provided in context. Read them before editing.
- Use the real imports, types, and helpers that already exist. Never invent an API, re-declare a constant, or re-implement a helper the codebase already provides.
- Do not imitate existing structure for its own sake. Do not extend god files, copy ceremony, or reproduce over-engineering. Put each change where responsibility belongs and leave the code better-shaped than you found it.
- Prefer narrow edits over broad rewrites, but a narrow edit in the wrong place is not correct — split or relocate when the right home does not exist.
- Do not swallow errors silently.
- Keep validation focused on the changed surface."""

VALIDATION_SELECTION_CONTRACT = """### validation_selection_contract
- Validate the changed surface when practical.
- Compile touched Python files when Python changed.
- Run focused tests for touched behavior when available.
- Use broader tests only for shared infrastructure, integration seams, or high-risk changes.
- Prefer fast deterministic checks over slow unrelated suites.
- Do not run irrelevant expensive tests as ritual validation.
- State skipped validation honestly.
- Never claim checks that were not run."""

RECEIPT_CONTRACT = """### receipt_contract
- List changed files.
- Summarize what changed.
- List validation run and result.
- State blockers or skipped validation honestly.
- Do not claim checks that were not run."""

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

_TARGET_FILE_CHAR_CAP = 24_000
_TARGET_FILES_TOTAL_CAP = 64_000
_TARGET_FILE_TRUNCATION_MARKER = "[truncated — read the rest with tools if needed]"
_TARGET_FILES_TOTAL_CAP_MARKER = "[target file contents total cap reached; omitted files: {files}]"


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
        source_id="target_file_contents",
        kind="workspace_files",
        roles=(RuntimeRole.WORKER, RuntimeRole.SINGLE),
        reason="live contents of the files this step edits",
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
    if source.source_id == "target_file_contents":
        return _load_target_file_contents(workspace_root, target_files)
    return "", "unknown context source"


def _load_target_file_contents(
    workspace_root: Path | None,
    target_files: tuple[str, ...] | None,
) -> tuple[str, str]:
    if workspace_root is None:
        return "", "no workspace root"
    if not target_files:
        return "", "no target files"

    blocks: list[str] = []
    omitted_files: list[str] = []
    loaded_chars = 0

    for index, raw_path in enumerate(target_files):
        resolved = _resolve_target_file_path(workspace_root, raw_path)
        if resolved is None:
            continue
        path, relpath = resolved
        if not path.is_file():
            continue
        if loaded_chars >= _TARGET_FILES_TOTAL_CAP:
            omitted_files.extend(
                _remaining_target_file_labels(workspace_root, target_files[index:])
            )
            break

        try:
            contents = path.read_text(encoding="utf-8")
        except (OSError, PermissionError, UnicodeError):
            continue

        remaining = _TARGET_FILES_TOTAL_CAP - loaded_chars
        file_text = contents[: min(len(contents), _TARGET_FILE_CHAR_CAP, remaining)]
        loaded_chars += len(file_text)

        marker_lines: list[str] = []
        if len(contents) > len(file_text):
            if len(file_text) >= _TARGET_FILE_CHAR_CAP:
                marker_lines.append(_TARGET_FILE_TRUNCATION_MARKER)
            elif loaded_chars >= _TARGET_FILES_TOTAL_CAP:
                marker_lines.append(
                    "[truncated because target file contents total cap was reached; read the rest with tools if needed]"
                )

        block_text = file_text
        if marker_lines:
            block_text = block_text.rstrip("\n") + "\n" + "\n".join(marker_lines)
        fence = _fence_for_target_file_block(block_text)
        closing_separator = "" if block_text.endswith("\n") else "\n"
        blocks.append(f"### Target file: {relpath}\n{fence}\n{block_text}{closing_separator}{fence}")

        if loaded_chars >= _TARGET_FILES_TOTAL_CAP:
            omitted_files.extend(
                _remaining_target_file_labels(workspace_root, target_files[index + 1 :])
            )
            break

    if omitted_files:
        blocks.append(_TARGET_FILES_TOTAL_CAP_MARKER.format(files=", ".join(omitted_files)))

    if not blocks:
        return "", "no readable target files"
    return "\n\n".join(blocks), "live contents of the files this step edits"


def _resolve_target_file_path(
    workspace_root: Path,
    raw_path: str,
) -> tuple[Path, str] | None:
    path_text = str(raw_path or "").strip()
    if not path_text:
        return None

    try:
        root = workspace_root.resolve()
        candidate = Path(path_text)
        if not candidate.is_absolute():
            candidate = root / candidate
        resolved = candidate.resolve()
        relpath = resolved.relative_to(root).as_posix()
    except (OSError, ValueError):
        return None
    if not relpath:
        return None
    return resolved, relpath


def _fence_for_target_file_block(contents: str) -> str:
    longest_run = 0
    current_run = 0
    for char in contents:
        if char == "`":
            current_run += 1
            longest_run = max(longest_run, current_run)
        else:
            current_run = 0
    return "`" * max(3, longest_run + 1)


def _remaining_target_file_labels(
    workspace_root: Path,
    target_files: tuple[str, ...],
) -> list[str]:
    labels: list[str] = []
    for raw_path in target_files:
        resolved = _resolve_target_file_path(workspace_root, raw_path)
        if resolved is None:
            continue
        path, relpath = resolved
        if path.is_file():
            labels.append(relpath)
    return labels


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
