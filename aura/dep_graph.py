"""Standalone queryable import/usage dependency index.

Builds a cross-module dependency graph from Python AST data,
supporting queries like who-imports-this-module, who-references-this-symbol,
and blast-radius analysis.
"""

from __future__ import annotations

import ast
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aura.ast_utils import parse_python_ast
from aura.config import MAX_READ_BYTES
from aura.fs_utils import SKIP_DIRS, get_max_mtime

_cache: dict[str, tuple[float, DepGraph]] = {}


def _file_to_module(workspace_relative_path: str) -> str:
    """Convert a workspace-relative .py path to a dotted module path."""
    normalized = workspace_relative_path.replace("\\", "/")
    # Handle __init__.py → directory module
    if normalized.endswith("/__init__.py"):
        return normalized[: -len("/__init__.py")].replace("/", ".")
    # Regular .py file
    path = normalized
    if path.endswith(".py"):
        path = path[:-3]
    return path.replace("/", ".")


def _extract(path: str, source: str) -> dict[str, Any]:
    """Extract defines, imports, and references from a Python source file."""
    defines: set[str] = set()
    imports: list[dict[str, Any]] = []
    references: set[str] = set()

    try:
        tree = parse_python_ast(source, filename=path)
    except SyntaxError:
        return {"defines": defines, "imports": imports, "references": references}

    # Top-level nodes for defines and imports
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            defines.add(node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defines.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    defines.add(target.id)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                defines.add(node.target.id)
        elif isinstance(node, ast.NamedExpr):
            if isinstance(node.target, ast.Name):
                defines.add(node.target.id)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imports.append({
                    "module": alias.name,
                    "name": "",
                    "alias": alias.asname or "",
                    "line": node.lineno,
                })
        elif isinstance(node, ast.ImportFrom):
            if node.module is not None:
                for alias in node.names:
                    imports.append({
                        "module": node.module,
                        "name": alias.name,
                        "alias": alias.asname or "",
                        "line": node.lineno,
                    })

    # All nodes for fuzzy reference collection
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            references.add(node.id)
        elif isinstance(node, ast.Attribute):
            references.add(node.attr)

    return {"defines": defines, "imports": imports, "references": references}


@dataclass
class DepGraph:
    """Cross-module dependency graph built from AST analysis of workspace files."""

    _def_index: dict[str, set[str]] = field(default_factory=dict)
    _ref_index: dict[str, set[str]] = field(default_factory=dict)
    _import_index: dict[str, set[str]] = field(default_factory=dict)
    _file_defines: dict[str, set[str]] = field(default_factory=dict)
    _file_imports: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    @staticmethod
    def _build(workspace_root: Path) -> DepGraph:
        """Walk workspace .py files and build the full index."""
        graph = DepGraph()

        for dirpath, dirnames, filenames in os.walk(workspace_root):
            # Prune hidden dirs and SKIP_DIRS (same logic as repo_map)
            dirnames[:] = [
                d
                for d in dirnames
                if not d.startswith(".")
                and d not in SKIP_DIRS
                and (workspace_root / d).parts[-1] not in SKIP_DIRS
            ]
            # Explicitly skip .aura and its backups
            dirnames[:] = [d for d in dirnames if d != ".aura" and not d.startswith(".aura")]

            rel_dir = os.path.relpath(dirpath, workspace_root)
            if rel_dir == ".":
                rel_dir = ""

            for fname in filenames:
                if not fname.endswith(".py"):
                    continue

                fpath = os.path.join(dirpath, fname)
                try:
                    file_size = os.path.getsize(fpath)
                    if file_size > MAX_READ_BYTES:
                        continue
                    with open(fpath, "rb") as f:
                        raw = f.read(MAX_READ_BYTES)
                except (OSError, PermissionError):
                    continue

                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    continue

                rel_path = os.path.join(rel_dir, fname) if rel_dir else fname
                rel_path = rel_path.replace("\\", "/")

                info = _extract(fpath, text)

                for sym in info["defines"]:
                    graph._def_index.setdefault(sym, set()).add(rel_path)
                    graph._file_defines.setdefault(rel_path, set()).add(sym)

                for sym in info["references"]:
                    graph._ref_index.setdefault(sym, set()).add(rel_path)

                for imp in info["imports"]:
                    graph._import_index.setdefault(imp["module"], set()).add(rel_path)

                if info["imports"]:
                    graph._file_imports[rel_path] = info["imports"]

        return graph

    def who_references(self, symbol: str) -> list[str]:
        """Return sorted list of files referencing the given symbol."""
        return sorted(self._ref_index.get(symbol, set()))

    def who_imports(self, module: str) -> list[str]:
        """Return sorted list of files importing the given module.

        Fuzzy over-include for dotted names: also matches when a parent
        module is imported and the child name is imported separately.
        """
        results: set[str] = set()

        # Exact match
        results.update(self._import_index.get(module, set()))

        # Fuzzy match for dotted names: e.g. "aura.repo_map" → parent "aura", child "repo_map"
        if "." in module:
            parent, child = module.split(".", 1)
            parent_importers = self._import_index.get(parent, set())
            for f in parent_importers:
                file_imports = self._file_imports.get(f, [])
                if any(imp["module"] == parent and imp["name"] == child for imp in file_imports):
                    results.add(f)

        return sorted(results)

    def blast_radius(self, target: str) -> list[str]:
        """Estimate impact scope of changing a file or symbol.

        For file paths (containing / or \\ or ending with .py): returns
        files that import that module plus files referencing any symbol
        defined in the file, excluding the file itself.
        For bare symbols: returns files that reference the symbol.
        Intentionally over-includes.
        """
        results: set[str] = set()
        normalized_target = target.replace("\\", "/")

        if "/" in normalized_target or normalized_target.endswith(".py"):
            module_path = _file_to_module(normalized_target)
            results.update(self.who_imports(module_path))

            for sym in self._file_defines.get(normalized_target, set()):
                results.update(self._ref_index.get(sym, set()))

            results.discard(normalized_target)
        else:
            results.update(self.who_references(target))

        return sorted(results)


def build_graph(workspace_root: Path, force: bool = False) -> DepGraph:
    """Build (or retrieve from cache) a DepGraph for the given workspace.

    Args:
        workspace_root: Root directory of the workspace.
        force: If True, always rebuild. If False, return cached result
               when mtimes are unchanged.
    """
    root_str = str(workspace_root.resolve())

    # Fast path: use cached result when available and not forced
    if not force and root_str in _cache:
        _, cached_graph = _cache[root_str]
        if cached_graph is not None:
            return cached_graph

    # Mtime validation
    current_mtime = get_max_mtime(workspace_root)
    cached_mtime, cached_graph = _cache.get(root_str, (0.0, None))
    if current_mtime == cached_mtime and cached_graph is not None:
        return cached_graph

    graph = DepGraph._build(workspace_root)
    _cache[root_str] = (current_mtime, graph)
    return graph


if __name__ == "__main__":
    from aura.paths import aura_root

    root = aura_root()
    graph = build_graph(root, force=True)
    print(f"Files indexed: {len(graph._file_defines)}")
    print(f"who_imports('aura.repo_map'):", graph.who_imports("aura.repo_map"))
    print(f"who_references('build_tier1_context'):", graph.who_references("build_tier1_context"))
    print(f"blast_radius('aura/repo_map.py'):", graph.blast_radius("aura/repo_map.py"))
