"""Structural audit API — cheap deterministic checks on changed files.

Detects parse failures, removed exports, stale references, and
unresolved dependencies in changed files.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from aura.code_intel.index import get_cached_index

logger = logging.getLogger(__name__)


def audit_changed_files(
    workspace_root: Path, changed_files: list[str]
) -> list[Any]:
    """Run structural audit on a set of changed files.

    Phases:

    1. Cached/refresh a :class:`CodeIntelIndex` for the workspace (full
       refresh for blast-radius context, then re-parse changed files).
    2. Parse-failure detection on each changed file.
    3. Removed-export detection via git HEAD comparison.
    4. Stale-reference detection in blast-radius dependents.
    5. Unresolved-dependency detection.
    6. Return findings sorted by file, then line.

    Returns:
        list[AuditFinding]
    """
    from aura.code_intel.models import AuditFinding

    if not changed_files:
        return []

    findings: list[AuditFinding] = []

    try:
        index = get_cached_index(workspace_root)
        # Full refresh first to build whole-repo context for blast radius
        index.refresh()
        # Then re-parse changed files specifically
        index.refresh(changed_files=changed_files)
    except Exception as exc:
        logger.warning("audit_changed_files: index refresh failed — %s", exc)
        return [
            AuditFinding(
                file="",
                line=None,
                message=f"Audit index refresh failed: {exc}",
                severity="warning",
                kind="parse_failure",
            )
        ]

    # Phase 1 — Parse failures
    findings.extend(_detect_parse_failures(index, changed_files))

    # Phase 2 — Removed exports
    findings.extend(_detect_removed_exports(index, workspace_root, changed_files))

    # Phase 3 — Stale references in blast radius
    findings.extend(_detect_stale_references(index, workspace_root, changed_files))

    # Phase 4 — Unresolved dependencies
    findings.extend(_detect_unresolved_dependencies(index, workspace_root, changed_files))

    findings.sort(key=lambda f: (f.file, f.line or 0))
    return findings


def _get_pre_change_content(workspace_root: Path, file_path: str) -> str | None:
    """Return git HEAD content for *file_path*, or None if untracked/unavailable."""
    import subprocess
    try:
        result = subprocess.run(
            ["git", "-C", str(workspace_root), "show", f"HEAD:{file_path}"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return None
        return result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def _find_symbol_line(symbols, name: str) -> int | None:
    """Return the line number of *name* in a list of SymbolInfo, or None."""
    for s in symbols:
        if s.name == name:
            return s.line
    return None


def _detect_parse_failures(
    index: CodeIntelIndex, changed_files: list[str]
) -> list[Any]:
    """Return parse_failure findings for changed files."""
    from aura.code_intel.models import AuditFinding

    findings: list[AuditFinding] = []
    changed_norm = {p.replace("\\", "/") for p in changed_files}

    for path_str in changed_norm:
        file_info = index.get_file(path_str)
        if file_info is None:
            findings.append(
                AuditFinding(
                    file=path_str,
                    line=None,
                    message="File could not be indexed (skipped, binary, or missing)",
                    severity="warning",
                    kind="parse_failure",
                )
            )
        else:
            for diag in index.get_diagnostics(path_str):
                findings.append(
                    AuditFinding(
                        file=diag.file or path_str,
                        line=diag.line,
                        message=diag.message,
                        severity=diag.severity or "warning",
                        kind="parse_failure",
                    )
                )
    return findings


def _detect_removed_exports(
    index: CodeIntelIndex, workspace_root: Path, changed_files: list[str]
) -> list[Any]:
    """Detect removed/renamed top-level symbols by comparing git HEAD with index."""
    from aura.code_intel.adapter import get_adapter
    from aura.code_intel.models import AuditFinding

    findings: list[AuditFinding] = []
    changed_norm = {p.replace("\\", "/") for p in changed_files}

    for path_str in changed_norm:
        pre_content = _get_pre_change_content(workspace_root, path_str)
        if pre_content is None:
            continue  # New file or not tracked

        adapter = get_adapter(path_str, content=pre_content)
        if adapter is None:
            continue

        pre_symbols = adapter.symbols(path_str, pre_content)
        pre_names = {s.name for s in pre_symbols}

        post_symbols = index.get_symbols(path_str)
        post_names = {s.name for s in post_symbols}

        removed = pre_names - post_names
        for name in sorted(removed):
            line = _find_symbol_line(pre_symbols, name)
            is_private = name.startswith("_")
            defined_elsewhere = index._symbol_defs.get((adapter.language_id, name), set()) - {path_str}

            if defined_elsewhere:
                # Cross-file collision — symbol removed from this file but
                # still defined in other files.  Cannot hard-block because
                # the name survives, but the removal may break intra-file
                # references within this changed file.
                elsewhere = sorted(defined_elsewhere)
                findings.append(
                    AuditFinding(
                        file=path_str,
                        line=line,
                        message=f"Removed '{name}' from this file; still defined in {', '.join(elsewhere)}. "
                        "Verify importers within this file.",
                        severity="warning",
                        kind="removed_export",
                    )
                )
                continue

            # Genuinely gone from the workspace — hard error for public,
            # warning for private.
            if is_private:
                findings.append(
                    AuditFinding(
                        file=path_str,
                        line=line,
                        message=f"Removed private symbol '{name}'",
                        severity="warning",
                        kind="removed_export",
                    )
                )
            else:
                findings.append(
                    AuditFinding(
                        file=path_str,
                        line=line,
                        message=f"Removed public symbol '{name}'",
                        severity="error",
                        kind="removed_export",
                    )
                )
    return findings


def _detect_stale_references(
    index: CodeIntelIndex, workspace_root: Path, changed_files: list[str]
) -> list[Any]:
    """Detect references to symbols that this change concretely removed.

    For each changed file, reads the pre-change symbol set from git HEAD,
    computes which symbols were removed, then flags blast-radius files that
    import those specific names from that specific module.
    """
    from aura.code_intel.adapter import get_adapter
    from aura.code_intel.models import AuditFinding

    findings: list[AuditFinding] = []
    changed_norm = {p.replace("\\", "/") for p in changed_files}
    seen: set[tuple[str, int, str]] = set()

    for path_str in changed_norm:
        # --- Pre-change baseline ---
        pre_content = _get_pre_change_content(workspace_root, path_str)
        if pre_content is None:
            continue  # New file or untracked \u2014 no baseline

        adapter = get_adapter(path_str, content=pre_content)
        if adapter is None:
            continue

        pre_names = {s.name for s in adapter.symbols(path_str, pre_content)}
        post_names = {s.name for s in index.get_symbols(path_str)}

        removed_here = pre_names - post_names
        if not removed_here:
            continue

        # --- Derive dotted module path ---
        # e.g. "aura/repo_map.py"         -> "aura.repo_map"
        #      "aura/code_intel/__init__.py" -> "aura.code_intel"
        mp = path_str
        if mp.endswith(".py"):
            mp = mp[:-3]
        if mp.endswith("/__init__"):
            mp = mp[:-9]
        changed_module = mp.replace("/", ".")

        try:
            blast_files = index.get_blast_radius(path_str)
        except Exception:
            continue

        for blast_file in blast_files:
            refs = index._refs.get(blast_file, [])
            for ref in refs:
                # Skip references within changed files themselves
                if ref.source_file in changed_norm:
                    continue

                key = (ref.source_file, ref.line, ref.target_symbol)
                if key in seen:
                    continue
                seen.add(key)

                # Flag only if this reference targets the changed module
                if not (
                    ref.target_symbol == changed_module
                    or ref.target_symbol.startswith(changed_module + ".")
                ):
                    continue

                bare = ref.target_symbol.rsplit(".", 1)[-1]
                if bare in removed_here:
                    findings.append(
                        AuditFinding(
                            file=ref.source_file,
                            line=ref.line,
                            message=(
                                f"Stale reference to removed symbol '{bare}' "
                                f"(imported from {changed_module})"
                            ),
                            severity="warning",
                            kind="stale_reference",
                        )
                    )
    return findings


def _detect_unresolved_dependencies(
    index: CodeIntelIndex, workspace_root: Path, changed_files: list[str]
) -> list[Any]:
    """Detect import paths in changed files that no longer resolve to workspace files."""
    from aura.code_intel.adapter import get_adapter
    from aura.code_intel.models import AuditFinding

    findings: list[AuditFinding] = []
    changed_norm = {p.replace("\\", "/") for p in changed_files}
    indexed_paths = set(index.file_paths())

    for path_str in changed_norm:
        abs_path = workspace_root / path_str
        if not abs_path.is_file():
            continue

        try:
            content = abs_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        adapter = get_adapter(path_str, content=content)
        if adapter is None:
            continue

        try:
            deps = adapter.dependencies(path_str, content)
        except Exception:
            continue

        for dep in deps:
            dep_path = dep.replace("\\", "/")

            # Skip relative imports
            if dep_path.startswith("."):
                continue

            # Skip bare module names with no path separator (stdlib / third-party)
            if "/" not in dep_path and not dep_path.endswith(".py"):
                continue

            # Try direct file path
            if (workspace_root / dep_path).is_file():
                continue

            # Try dotted-module -> path conversion
            if "." in dep_path and dep_path.endswith(".py"):
                alt = dep_path.replace(".", "/")
                if (workspace_root / alt).is_file():
                    continue

            if dep_path in indexed_paths:
                continue

            # For bare module names ending in .py (e.g. "os.py", "click.py")
            # with no directory separator: likely stdlib or third-party
            if "/" not in dep_path and dep_path.endswith(".py"):
                continue

            findings.append(
                AuditFinding(
                    file=path_str,
                    line=None,
                    message=f"Unresolved dependency '{dep}'",
                    severity="warning",
                    kind="unresolved_dependency",
                )
            )
    return findings

