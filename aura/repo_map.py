"""Generate a concise AST-based structural map of the workspace.

Tier 1 (Core Context) uses this to inject a repo map into the system prompt,
giving the model a structural overview of the codebase on every turn.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path
from typing import Any

from aura.ast_utils import parse_python_ast
from aura.repository_inventory import RepositoryInventory, build_inventory

logger = logging.getLogger(__name__)

# Cache: workspace_root_str -> (fingerprint, cached_text)
_repo_map_cache: dict[str, tuple[tuple[int, float], str]] = {}
_repo_summary_cache: dict[str, tuple[tuple[int, float], str]] = {}

MAX_LINES = 300

# The production agent gets the directory index instead of the full outline.
# The full map is a 300-line prefix of an alphabetical walk, so on any real
# repository it is both large and arbitrary — it spends most of the prompt on
# whichever files sort first and truncates before reaching the rest. A
# directory index is what orientation actually needs: where things live, so
# search and read land on the right subtree the first time.
SUMMARY_MAX_DIRS = 40

_PY_FUNC_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef)


def _fingerprint(inventory: RepositoryInventory) -> tuple[int, float]:
    """A cheap freshness fingerprint for a repository inventory snapshot.

    Not a full content hash — (file_count, max_mtime) is enough to notice an
    addition (count changes), a removal (count changes), or an edit (the
    edited file's mtime becomes "now", which is >= the prior max) without
    reading any file body. This replaces the old max-mtime-of-.py/.ts/.tsx/.js
    check, which never noticed a file being added or removed at all and
    ignored every other supported repository language.
    """
    if not inventory.files:
        return (0, 0.0)
    return (len(inventory.files), max(f.mtime for f in inventory.files))


def _outline_python(text: str, filename: str = "<unknown>") -> dict[str, Any]:
    """AST-based outline for Python files.

    Returns dict with keys: language, imports, classes, functions.
    """
    imports: list[str] = []
    classes: list[dict[str, Any]] = []
    functions: list[dict[str, Any]] = []

    try:
        tree = parse_python_ast(text, filename=filename)
    except SyntaxError:
        return {"language": "python", "imports": [], "classes": [], "functions": []}

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name
                if alias.asname:
                    imports.append(f"import {name} as {alias.asname}")
                else:
                    imports.append(f"import {name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names: list[str] = []
            for alias in node.names:
                if alias.asname:
                    names.append(f"{alias.name} as {alias.asname}")
                else:
                    names.append(alias.name)
            imports.append(f"from {module} import {', '.join(names)}")
        elif isinstance(node, ast.ClassDef):
            bases = [_ast_expr_to_str(b) for b in node.bases]
            methods: list[str] = []
            for body_node in node.body:
                if isinstance(body_node, _PY_FUNC_TYPES):
                    sig = _py_func_signature(body_node)
                    methods.append(sig)
            classes.append(
                {
                    "name": node.name,
                    "line": node.lineno,
                    "bases": bases,
                    "methods": methods,
                }
            )
        elif isinstance(node, _PY_FUNC_TYPES):
            sig = _py_func_signature(node)
            functions.append(
                {
                    "name": node.name,
                    "line": node.lineno,
                    "signature": sig,
                }
            )

    return {
        "language": "python",
        "imports": imports,
        "classes": classes,
        "functions": functions,
    }


def _ast_expr_to_str(node: ast.expr) -> str:
    """Convert an AST expression node to a source string."""
    try:
        return ast.unparse(node)
    except (AttributeError, Exception):
        return str(type(node).__name__)


def _py_func_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Reconstruct a Python function signature from AST."""
    try:
        return ast.unparse(node).split("\n")[0].rstrip(":")
    except (AttributeError, Exception):
        args = ", ".join(a.arg for a in node.args.args)
        prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        return f"{prefix} {node.name}({args})"


def generate_repo_map(workspace_root: Path, force: bool = False) -> str:
    """Generate a concise AST-based structural map of the workspace.

    Args:
        workspace_root: Root directory of the workspace.
        force: If True, always validate mtime and regenerate if stale.
               If False (default), return cached result immediately when
               available, avoiding a full workspace tree mtime scan.

    Returns:
        A tree-like string showing top-level directories, then per-file:
        classes, functions, and top-level variables.
        Returns 'No Python/TypeScript files found.' if no relevant files exist.
        Returns an empty string on errors.

    Test cases:
        1. Empty workspace returns "No Python/TypeScript files found."
        2. Workspace with one file yields correct outline.
        3. Adding a new file invalidates cache.
    """
    root_str = str(workspace_root.resolve())

    # Fast path: use cached result when available and not forced to refresh.
    # Avoids a full workspace tree mtime scan on hot GUI paths.
    if not force and root_str in _repo_map_cache:
        _, cached_text = _repo_map_cache[root_str]
        if cached_text:
            return cached_text

    # Check cache with a fingerprint derived from the canonical inventory
    inventory = build_inventory(workspace_root)
    current_fingerprint = _fingerprint(inventory)
    cached_fingerprint, cached_text = _repo_map_cache.get(root_str, ((0, 0.0), ""))
    if current_fingerprint == cached_fingerprint and cached_text:
        return cached_text

    from aura.code_intel.index import CodeIntelIndex

    index = CodeIntelIndex(workspace_root)
    index.refresh()

    # Collect outlines from the index
    tree_lines: list[str] = []
    file_count = index.file_count()

    for rel_path in index.file_paths():
        outline = index.get_outline(rel_path)

        if not outline.get("classes") and not outline.get("functions"):
            tree_lines.append(rel_path)
            continue

        tree_lines.append("")
        tree_lines.append(rel_path)
        if outline["classes"]:
            for cls in outline["classes"]:
                bases_str = f"(extends {', '.join(cls['bases'])})" if cls["bases"] else ""
                tree_lines.append(f"  class {cls['name']}{bases_str}")
                for m in cls["methods"]:
                    tree_lines.append(f"    {m}")
        if outline["functions"]:
            for fn in outline["functions"]:
                tree_lines.append(f"  {fn['signature']}")

    if file_count == 0:
        result = "No Python/TypeScript files found."
        _repo_map_cache[root_str] = (current_fingerprint, result)
        return result

    # Build header
    header = f"### Repository Structure ({file_count} files)\n"

    # Trim to MAX_LINES
    if len(tree_lines) > MAX_LINES:
        tree_lines = tree_lines[: MAX_LINES - 2]
        tree_lines.append("")
        tree_lines.append("... (output truncated)")

    result = header + "\n".join(tree_lines)
    _repo_map_cache[root_str] = (current_fingerprint, result)
    return result


def generate_repo_summary(workspace_root: Path, force: bool = False) -> str:
    """Return a compact directory index of the indexed source tree.

    One line per directory that holds indexed files, with its file count, so
    the agent can aim its searches and reads without the full per-file outline
    being injected on every turn.  Returns an empty string when the workspace
    has nothing to index or the scan fails.
    """
    root_str = str(workspace_root.resolve())

    if not force and root_str in _repo_summary_cache:
        _, cached_text = _repo_summary_cache[root_str]
        if cached_text:
            return cached_text

    inventory = build_inventory(workspace_root)
    current_fingerprint = _fingerprint(inventory)
    cached_fingerprint, cached_text = _repo_summary_cache.get(root_str, ((0, 0.0), ""))
    if current_fingerprint == cached_fingerprint and cached_text:
        return cached_text

    try:
        from aura.code_intel.index import CodeIntelIndex

        index = CodeIntelIndex(workspace_root)
        index.refresh()
        file_count = index.file_count()
        rel_paths = list(index.file_paths())
    except Exception:
        logger.debug("generate_repo_summary failed", exc_info=True)
        return ""

    if file_count == 0:
        _repo_summary_cache[root_str] = (current_fingerprint, "")
        return ""

    counts: dict[str, int] = {}
    for rel_path in rel_paths:
        parent = Path(rel_path).parent.as_posix()
        directory = "." if parent in ("", ".") else parent
        counts[directory] = counts.get(directory, 0) + 1

    # Largest directories first decides *what* survives the cap; path order
    # decides how it reads.
    kept = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    omitted = len(kept) - SUMMARY_MAX_DIRS
    kept = kept[:SUMMARY_MAX_DIRS]

    lines = [
        f"### Repository layout ({file_count} indexed source files)",
        "Directory index only. Use search and read tools for file and symbol detail.",
    ]
    lines.extend(
        f"- {directory}/ ({count})" if directory != "." else f"- ./ ({count})"
        for directory, count in sorted(kept, key=lambda item: item[0])
    )
    if omitted > 0:
        lines.append(f"- ... {omitted} smaller directories not listed")

    result = "\n".join(lines)
    _repo_summary_cache[root_str] = (current_fingerprint, result)
    return result