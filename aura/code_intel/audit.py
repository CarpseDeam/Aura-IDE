"""Structural audit API — cheap deterministic checks on changed files.

Detects parse failures, removed exports, stale references, and
unresolved dependencies in changed files.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aura.code_intel.index import get_cached_index

if TYPE_CHECKING:
    from aura.code_intel.index import CodeIntelIndex

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


def _check_name_re_exported(content: str, name: str) -> bool:
    """Return True when *name* is bound by a top-level import/re-export in *content*.

    Checks the AST of a Python source string and returns ``True`` if any
    top-level ``import`` or ``from … import`` statement binds *name* in the
    local scope.

    Supported forms::

        from module import name
        from module import original as name
        import name
        import module as name
    """
    try:
        import ast
        tree = ast.parse(content)
    except SyntaxError:
        return False

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                bound = alias.asname if alias.asname else alias.name
                if bound == name:
                    return True
        elif isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname if alias.asname else alias.name.split(".", 1)[0]
                if bound == name:
                    return True
    return False


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

        # Read post-change source for re-export check (Python only)
        _post_content: str | None = None
        if path_str.endswith(".py"):
            try:
                _post_content = (workspace_root / path_str).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                pass

        for name in sorted(removed):
            # Private symbol re-exported via import is still accessible —
            # skip entirely (no removed_export, no defined_elsewhere warning).
            if (
                name.startswith("_")
                and _post_content is not None
                and _check_name_re_exported(_post_content, name)
            ):
                continue

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
            # silently skip private (stale-reference audit catches actual
            # broken references to removed private symbols).
            if is_private:
                continue
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

        # Read post-change source for re-export check (Python only)
        _post_content: str | None = None
        if path_str.endswith(".py"):
            try:
                _post_content = (workspace_root / path_str).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                pass

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
                    # Symbol re-exported from the changed module still
                    # resolves — skip stale reference.
                    if (
                        _post_content is not None
                        and _check_name_re_exported(_post_content, bare)
                    ):
                        continue
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

            resolution = _resolve_python_dependency_candidate(dep_path, workspace_root)
            if resolution is not False:
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


def _resolve_python_dependency_candidate(
    dep_path: str, workspace_root: Path
) -> bool | None:
    """Return whether a dependency candidate resolves as a Python import.

    ``True`` means the import is resolvable, ``False`` means it is clearly
    unresolved, and ``None`` means the candidate is not safely recognizable as
    a Python import dependency.  The audit is intentionally conservative:
    callers should only warn on ``False``.
    """
    module_name = _dependency_candidate_to_module_name(dep_path)
    if module_name is None:
        return None

    top_level = module_name.split(".", 1)[0]
    if _is_stdlib_module(top_level):
        return True

    return _find_python_spec(module_name, workspace_root)


def _dependency_candidate_to_module_name(dep_path: str) -> str | None:
    """Convert obvious path/module dependency forms to dotted module names."""
    candidate = dep_path.strip().replace("\\", "/")
    if not candidate or candidate.startswith("."):
        return None

    if candidate.endswith("/__init__.py"):
        candidate = candidate[: -len("/__init__.py")]
    elif candidate.endswith("/__init__"):
        candidate = candidate[: -len("/__init__")]
    elif candidate.endswith(".py"):
        candidate = candidate[:-3]
    elif "/" not in candidate:
        # Existing logic skips bare imports before this helper.  If another
        # adapter sends one here, only dotted module names are import-style.
        if "." not in candidate:
            return None

    module_name = candidate.replace("/", ".").strip(".")
    if not module_name:
        return None

    parts = module_name.split(".")
    if any(not part.isidentifier() for part in parts):
        return None
    return module_name


def _is_stdlib_module(top_level: str) -> bool:
    """Return True when *top_level* is a builtin or stdlib module name."""
    if top_level in sys.builtin_module_names:
        return True

    stdlib_names = getattr(sys, "stdlib_module_names", ())
    return top_level in stdlib_names


def _find_python_spec(module_name: str, workspace_root: Path) -> bool | None:
    """Resolve *module_name* with the workspace root temporarily importable."""
    root = str(workspace_root.resolve())
    inserted = False

    try:
        if root not in sys.path:
            sys.path.insert(0, root)
            inserted = True
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, AttributeError, ValueError):
        return None
    finally:
        if inserted:
            try:
                sys.path.remove(root)
            except ValueError:
                pass
