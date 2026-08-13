"""Initial scoped context source registry."""
from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from aura.context_gearbox.models import ContextLedgerEntry, ContextSource
from aura.repo_map import generate_repo_summary
from aura.skills.text import SkillPack, build_skill_pack

CORE_KERNEL_TEXT = """Core kernel:
- Work inside the selected workspace.
- Do not make claims about repository contents you have not verified.
- Keep the response and any changes scoped to the user's request."""

GUI_RULES = """### gui_rules
- Preserve existing signal wiring and data flow.
- Use existing theme tokens, styles, and components before adding new UI paths.
- The focused check for UI-adjacent changes is a GUI test or selfcheck.
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
- Keep BYOK and OpenRouter paths distinct.
- Surface provider errors cleanly without hiding useful diagnostics.
- Do not hardcode pricing or model lists unless explicitly required."""

WINDOWS_COMPUTER_USE_CONTEXT = """### Windows Computer Use
Structured Windows UI Automation tools are available for GUI-only workflows. Prefer file, terminal, Git, and Godot tools when they can perform the task deterministically. Treat successful tool results as the only evidence that a UI action occurred."""

BUILD_PIPELINE_RULES = """### build_pipeline_rules
- Do not break the Nuitka or installer flow.
- Keep build scripts Windows-safe.
- Avoid unrelated dependency churn.
- Validate compile assumptions before packaging assumptions.
- Preserve bundled resource handling."""

_SCOPED_PACK_TEXT: dict[str, str] = {
    "gui_rules": GUI_RULES,
    "drone_rules": DRONE_RULES,
    "provider_rules": PROVIDER_RULES,
    "build_pipeline_rules": BUILD_PIPELINE_RULES,
}

#: Context that exists only while the capability naming it is live.  Keyed by
#: the capability id the tool registry reports, never by a setting: a setting
#: says what someone asked for, and this text claims tools are *available*.
#: Those diverge exactly when it matters — enabled but still connecting,
#: enabled but the server failed to launch — and a prompt that promises tools
#: the model cannot call is how a turn ends up inventing UI actions.
_CAPABILITY_PACK_TEXT: dict[str, str] = {
    "windows_computer_use": WINDOWS_COMPUTER_USE_CONTEXT,
}

_CODING_TASK_KINDS = {
    "new tool or app",
    "bugfix",
    "gui polish",
    "cleanup",
    "refactor",
    "implementation",
}

# Sized for the current DeepSeek V4 models (1M-token context), not for the
# old cramped defaults. A single file may take up to half the turn budget so
# one large file still loads whole, while a wide multi-file turn stays bounded.
_TARGET_FILE_CHAR_CAP = 128_000
_TARGET_FILES_TOTAL_CAP = 256_000
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
        reason="baseline runtime posture",
    ),
    ContextSource(
        source_id="project_rules",
        kind="workspace_file",
        reason="explicit project rules file",
    ),
    ContextSource(
        source_id="repo_map",
        kind="workspace_structure",
        reason="repository structure overview",
    ),
    ContextSource(
        source_id="target_file_contents",
        kind="workspace_files",
        reason="edit-surface files (compact manifest of the files this step touches)",
    ),
    ContextSource(
        source_id="gui_rules",
        kind="scoped_coding_pack",
        reason="target files or task kind match GUI scope",
    ),
    ContextSource(
        source_id="drone_rules",
        kind="scoped_coding_pack",
        reason="target files or task kind match drone scope",
    ),
    ContextSource(
        source_id="provider_rules",
        kind="scoped_coding_pack",
        reason="target files or task kind match provider scope",
    ),
    ContextSource(
        source_id="build_pipeline_rules",
        kind="scoped_coding_pack",
        reason="target files or task kind match build scope",
    ),
    ContextSource(
        source_id="windows_computer_use",
        kind="capability_pack",
        reason="Windows UI Automation tools are connected for this request",
    ),
    ContextSource(
        source_id="skill_pack",
        kind="skill_pack",
        reason="terrain-selected skills for this context",
    ),
)

def iter_registered_sources() -> tuple[ContextSource, ...]:
    return CONTEXT_SOURCES

def collect_source_text(
    source: ContextSource,
    workspace_root: Path | None,
    *,
    force: bool = False,
    model: str | None = None,
    task_kind: str | None = None,
    target_files: tuple[str, ...] | None = None,
    content: str | None = None,
    active_capabilities: frozenset[str] | None = None,
) -> tuple[str, ContextLedgerEntry, list[ContextLedgerEntry]]:
    try:
        pack: SkillPack | None = None
        if _source_origin_matches_target_file(source, target_files, workspace_root):
            text, reason = "", "skipped because target file owns this context source"
        elif source.kind == "skill_pack":
            text, reason, pack = _load_skill_pack(
                workspace_root,
                model,
                task_kind,
                target_files,
                content,
            )
        else:
            text, reason = _load_source_text(
                source,
                workspace_root,
                force=force,

                model=model,
                task_kind=task_kind,
                target_files=target_files,
                content=content,
                active_capabilities=active_capabilities,
            )
        included = bool(text)
        entry = ContextLedgerEntry(
            source_id=source.source_id,
            kind=source.kind,
            reason=reason or source.reason,
            included=included,
            char_count=len(text),
        )
        if source.kind == "skill_pack" and pack is not None:
            # The aggregate skill-pack entry separately reports the compact
            # candidate-index characters and the eagerly injected guard
            # characters, so the Gearbox can see exactly what each contributes.
            entry = ContextLedgerEntry(
                source_id=source.source_id,
                kind=source.kind,
                reason=reason or source.reason,
                included=included,
                char_count=len(text),
                detail=f"index_chars={pack.index_chars}; guard_chars={pack.guard_chars}",
            )

        # For skill_pack, include per-skill ledger entries so the Context
        # Gearbox shows the actual skill IDs, the deterministic selection
        # reason, the character contribution, and the lifecycle class of each
        # candidate — indexed, eagerly injected guard, or skipped.
        extra: list[ContextLedgerEntry] = []
        if pack is not None:
            for candidate in pack.candidates:
                if candidate.eager_guard:
                    detail = (
                        "eager_guard "
                        f"guard_chars={candidate.guard_chars} "
                        f"body_chars={candidate.body_chars} "
                        f"body_hash={candidate.body_hash}"
                    )
                    char_count = candidate.guard_chars
                else:
                    detail = (
                        "candidate_indexed "
                        f"index_chars={candidate.index_chars} "
                        f"body_chars={candidate.body_chars} "
                        f"body_hash={candidate.body_hash}"
                    )
                    char_count = candidate.index_chars
                extra.append(
                    ContextLedgerEntry(
                        source_id=candidate.skill_id,
                        kind="individual_skill",
                        reason=(
                            f"{candidate.label} [{candidate.provenance}] "
                            f"{candidate.reason}"
                        ),
                        included=True,
                        char_count=char_count,
                        detail=detail,
                    )
                )
            for record in pack.skipped:
                extra.append(
                    ContextLedgerEntry(
                        source_id=record.skill_id,
                        kind="individual_skill",
                        reason=f"{record.label} [{record.provenance}] {record.reason}",
                        included=False,
                        char_count=0,
                        detail="skipped",
                    )
                )

        return text, entry, extra
    except Exception as exc:
        return "", ContextLedgerEntry(
            source_id=source.source_id,
            kind=source.kind,
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
    model: str | None,
    task_kind: str | None,
    target_files: tuple[str, ...] | None,
    content: str | None,
    active_capabilities: frozenset[str] | None = None,
) -> tuple[str, str]:
    if source.kind == "capability_pack":
        return _load_capability_pack(source, active_capabilities)
    if source.kind == "scoped_coding_pack":
        return _load_scoped_coding_pack(
            source,
            workspace_root,
            task_kind,
            target_files,
        )
    if source.kind == "skill_pack":
        text, reason, _pack = _load_skill_pack(
            workspace_root,
            model,
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
        # Production context gets the bounded directory index. The full
        # per-file outline was the single largest block in every turn and
        # truncated arbitrarily; structure detail loads through the search
        # and read tools instead.
        summary = generate_repo_summary(workspace_root, force=force)
        if not summary:
            return "", "no indexed source files"
        return summary, "bounded repository directory index"
    if source.source_id == "target_file_contents":
        return _load_target_file_manifest(workspace_root, target_files)
    return "", "unknown context source"

def _load_target_file_manifest(
    workspace_root: Path,
    target_files: tuple[str, ...],
) -> tuple[str, str]:
    """Render the compact target-file manifest for the production conversation.

    Only normalized workspace-relative paths survive — mentioning a file must
    not inject its body into the system prompt. Bodies enter through the
    explicit read tools. The ledger stays honest too: this source reports the
    manifest's size, not hundreds of kilobytes of source.
    """
    resolved: list[str] = []
    for raw_path in target_files:
        resolved_pair = _resolve_target_file_path(workspace_root, raw_path)
        if resolved_pair is None:
            continue
        path, relpath = resolved_pair
        if not path.is_file():
            continue
        if relpath not in resolved:
            resolved.append(relpath)
    if not resolved:
        return "", "no readable target files"
    lines = [
        "### Target files (manifest)",
        "This step's edit surface. Contents are not preloaded; read each "
        "file you will edit with the read tools before changing it.",
    ]
    lines.extend(f"- {relpath}" for relpath in resolved)
    return "\n".join(lines), "manifest of target files; contents load through read tools"

def loaded_target_files(
    workspace_root: Path | None,
    target_files: tuple[str, ...] | None,
) -> list[str]:
    """Return normalized target files whose contents can be preloaded."""
    if workspace_root is None or not target_files:
        return []
    loaded: list[str] = []
    loaded_chars = 0
    for raw_path in target_files:
        resolved = _resolve_target_file_path(workspace_root, raw_path)
        if resolved is None:
            continue
        path, relpath = resolved
        if not path.is_file():
            continue
        if loaded_chars >= _TARGET_FILES_TOTAL_CAP:
            break
        try:
            contents = path.read_text(encoding="utf-8")
        except (OSError, PermissionError, UnicodeError):
            continue
        remaining = _TARGET_FILES_TOTAL_CAP - loaded_chars
        file_text = contents[: min(len(contents), _TARGET_FILE_CHAR_CAP, remaining)]
        loaded_chars += len(file_text)
        loaded.append(relpath)
    return loaded

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

def _load_capability_pack(
    source: ContextSource,
    active_capabilities: frozenset[str] | None,
) -> tuple[str, str]:
    """Load a capability pack only while its capability is actually live."""
    text = _CAPABILITY_PACK_TEXT.get(source.source_id, "")
    if not text:
        return "", "unknown capability pack"
    if source.source_id not in (active_capabilities or frozenset()):
        return "", "capability is not connected for this request"
    return text, source.reason

def _load_scoped_coding_pack(
    source: ContextSource,
    workspace_root: Path | None,
    task_kind: str | None,
    target_files: tuple[str, ...] | None,
) -> tuple[str, str]:
    text = _SCOPED_PACK_TEXT.get(source.source_id, "")
    rule = _SCOPED_PACK_RULES.get(source.source_id)
    if not text or rule is None:
        return "", "unknown scoped coding pack"
    normalized_targets = _normalize_target_file_paths(target_files, workspace_root)
    if not _single_scoped_pack_applies(
        task_kind,
        normalized_targets,
    ):
        return "", "single task is not coding-shaped"
    if not _scoped_pack_matches(rule, normalized_targets, task_kind):
        return "", f"target files do not match {rule.scope_name} scope"
    return text, source.reason

def _load_skill_pack(
    workspace_root: Path | None,
    model: str | None,
    task_kind: str | None,
    target_files: tuple[str, ...] | None,
    content: str | None,
) -> tuple[str, str, SkillPack | None]:
    if workspace_root is None:
        return "", "no workspace root", None
    pack = build_skill_pack(
        workspace_root,
        model=model,
        task_kind=task_kind,
        target_files=tuple(target_files or ()),
        content=content,
    )
    if pack.text:
        return pack.text, "terrain-selected skills for this context", pack
    return "", "no skills matched for this terrain", pack

def _single_scoped_pack_applies(
    task_kind: str | None,
    normalized_targets: tuple[str, ...],
) -> bool:
    # Validating a GUI/provider/build/drone file must not pull in that
    # subsystem's implementation guidance.
    if _is_validation_task_kind(task_kind):
        return False
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

def _source_origin_matches_target_file(
    source: ContextSource,
    target_files: tuple[str, ...] | None,
    workspace_root: Path | None,
) -> bool:
    if not source.origin_paths or not target_files:
        return False
    normalized_targets = set(_normalize_target_file_paths(target_files, workspace_root))
    if not normalized_targets:
        return False
    normalized_origins = set(_normalize_target_file_paths(source.origin_paths, workspace_root))
    return bool(normalized_targets & normalized_origins)

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

def _is_validation_task_kind(task_kind: str | None) -> bool:
    return _normalize_task_kind(task_kind) == "validation"

def _normalize_task_kind(value: Any) -> str:
    return " ".join(
        str(value or "")
        .strip()
        .lower()
        .replace("-", " ")
        .replace("_", " ")
        .split()
    )
