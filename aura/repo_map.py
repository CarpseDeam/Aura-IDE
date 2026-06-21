"""Generate a concise AST-based structural map of the workspace.

Tier 1 (Core Context) uses this to inject a repo map into the system prompt,
giving the model a structural overview of the codebase on every turn.
"""

from __future__ import annotations

import ast
import logging
import os
import time
from pathlib import Path
from typing import Any

from aura.ast_utils import parse_python_ast
from aura.fs_utils import SKIP_DIRS, SKIP_FILE_SUFFIXES, MAX_DIRS_VISITED, MAX_FILES_CONSIDERED, MAX_SCAN_SECONDS, get_max_mtime

logger = logging.getLogger(__name__)

# Cache: workspace_root_str -> (max_mtime, cached_text)
_repo_map_cache: dict[str, tuple[float, str]] = {}

MAX_LINES = 300

_PY_FUNC_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef)

def _should_skip(path: Path) -> bool:
    """Check if a path should be excluded from the repo map."""
    parts = set(path.parts)
    if parts & SKIP_DIRS:
        return True
    if path.name.startswith("."):
        return True
    if path.suffix in SKIP_FILE_SUFFIXES:
        return True
    return False



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

    # Check cache with mtime validation
    current_mtime = get_max_mtime(workspace_root)
    cached_mtime, cached_text = _repo_map_cache.get(root_str, (0.0, ""))
    if current_mtime == cached_mtime and cached_text:
        return cached_text

    from aura.config import MAX_READ_BYTES

    # Walk workspace and collect outlines
    tree_lines: list[str] = []
    file_count = 0
    dirs_visited = 0
    files_considered = 0
    start_time = time.monotonic()
    budget_exceeded = False

    for dirpath, dirnames, filenames in os.walk(workspace_root):
        dirs_visited += 1
        if dirs_visited > MAX_DIRS_VISITED or time.monotonic() - start_time > MAX_SCAN_SECONDS:
            budget_exceeded = True
        if budget_exceeded:
            break

        # Prune skipped/hidden dirs
        dirnames[:] = [
            d
            for d in dirnames
            if not d.startswith(".") and d not in SKIP_DIRS and (workspace_root / d).parts[-1] not in SKIP_DIRS
        ]

        rel_dir = os.path.relpath(dirpath, workspace_root)
        if rel_dir == ".":
            rel_dir = ""

        for fname in sorted(filenames):
            suffix = Path(fname).suffix.lower()
            if suffix not in (".py", ".ts", ".tsx", ".js"):
                continue
            files_considered += 1
            if files_considered > MAX_FILES_CONSIDERED:
                budget_exceeded = True
                break

            fpath = os.path.join(dirpath, fname)
            try:
                # Use chunked read here as well
                file_size = os.path.getsize(fpath)
                if file_size > MAX_READ_BYTES:
                    continue  # Skip massive files for outline

                with open(fpath, "rb") as f:
                    raw = f.read(MAX_READ_BYTES)
            except (OSError, PermissionError):
                continue

            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue

            rel_path = os.path.join(rel_dir, fname) if rel_dir else fname
            file_count += 1

            if suffix == ".py":
                outline = _outline_python(text, filename=fpath)
            else:
                # For non-Python files, just record the file name
                tree_lines.append(rel_path)
                continue

            if not outline["classes"] and not outline["functions"]:
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

        if budget_exceeded:
            break

    if budget_exceeded:
        tree_lines.append("")
        tree_lines.append("... (repo map truncated: scan budget exceeded)")
        logger.info(
            "Repo-map scan truncated: root=%s dirs_visited=%d files_considered=%d elapsed_ms=%.0f",
            root_str, dirs_visited, files_considered,
            (time.monotonic() - start_time) * 1000,
        )

    if file_count == 0 and not budget_exceeded:
        result = "No Python/TypeScript files found."
        _repo_map_cache[root_str] = (current_mtime, result)
        return result

    # Build header
    header = f"### Repository Structure ({file_count} files)\n"

    # Trim to MAX_LINES
    if len(tree_lines) > MAX_LINES:
        tree_lines = tree_lines[: MAX_LINES - 2]
        tree_lines.append("")
        tree_lines.append("... (output truncated)")

    result = header + "\n".join(tree_lines)
    _repo_map_cache[root_str] = (current_mtime, result)
    return result