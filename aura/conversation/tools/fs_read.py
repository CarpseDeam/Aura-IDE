"""Read-only filesystem tools: read_file, list_directory, glob, read_file_outline."""
from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

from aura.config import MAX_GLOB_RESULTS, MAX_READ_BYTES, SKIP_DIRS, SKIP_FILE_SUFFIXES


def _should_skip(path: Path) -> bool:
    parts = set(path.parts)
    if parts & SKIP_DIRS:
        return True
    if path.name.startswith("."):
        return True
    if path.suffix in SKIP_FILE_SUFFIXES:
        return True
    return False


def read_file(workspace_root: Path, target: Path) -> dict[str, Any]:
    if not target.exists():
        return {"ok": False, "error": f"file not found: {target.relative_to(workspace_root)}"}
    if not target.is_file():
        return {"ok": False, "error": f"not a regular file: {target.relative_to(workspace_root)}"}
    raw = target.read_bytes()
    truncated = False
    if len(raw) > MAX_READ_BYTES:
        raw = raw[:MAX_READ_BYTES]
        truncated = True
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return {"ok": False, "error": f"file cannot be decoded as UTF-8: {target.relative_to(workspace_root)}"}
    if truncated:
        text += f"\n\n[... truncated at {MAX_READ_BYTES} bytes ...]"
    rel = target.relative_to(workspace_root).as_posix()
    return {"ok": True, "path": rel, "content": text, "truncated": truncated}


def read_file_outline(workspace_root: Path, target: Path) -> dict[str, Any]:
    """Extract class names, function signatures, and imports from a file.

    Uses AST for Python, and generic regex for
    other languages. Returns a structural summary without full file content.
    """
    if not target.exists():
        return {"ok": False, "error": f"file not found: {target.relative_to(workspace_root)}"}
    if not target.is_file():
        return {"ok": False, "error": f"not a regular file: {target.relative_to(workspace_root)}"}

    raw = target.read_bytes()
    truncated = False
    if len(raw) > MAX_READ_BYTES:
        raw = raw[:MAX_READ_BYTES]
        truncated = True
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return {"ok": False, "error": "file cannot be decoded as UTF-8"}

    suffix = target.suffix.lower()
    rel = target.relative_to(workspace_root).as_posix()
    lines = text.splitlines()
    total_lines = len(lines) + (0 if not truncated else 0)  # line count from what we read

    if suffix == ".py":
        result = _outline_python(text, lines)
    else:
        result = _outline_generic(lines)

    language = result["language"]
    imports = result["imports"]
    classes = result["classes"]
    functions = result["functions"]

    # Build compact text summary
    text_parts: list[str] = []
    text_parts.append(f"# read_file_outline: {rel} ({language}, {total_lines} lines)")

    if imports:
        text_parts.append("# Imports:")
        for imp in imports:
            text_parts.append(imp)
        text_parts.append("")

    if classes:
        text_parts.append("# Classes:")
        for cls in classes:
            bases_str = " extends " + ", ".join(cls["bases"]) if cls["bases"] else ""
            text_parts.append(f"## {cls['name']} (line {cls['line']}){bases_str}")
            for m in cls["methods"]:
                text_parts.append(f"  {m}")
            if not cls["methods"]:
                text_parts.append("  (no methods)")
        text_parts.append("")

    if functions:
        text_parts.append("# Functions:")
        for fn in functions:
            text_parts.append(f"## {fn['signature']} (line {fn['line']})")
        text_parts.append("")

    if not imports and not classes and not functions:
        text_parts.append("# (no structural elements detected)")

    text_out = "\n".join(text_parts)

    return {
        "ok": True,
        "path": rel,
        "language": language,
        "total_lines": total_lines,
        "imports": imports,
        "classes": classes,
        "functions": functions,
        "text": text_out,
    }


# ---------------------------------------------------------------------------
# Internal outline helpers
# ---------------------------------------------------------------------------

_PY_FUNC_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef)


def _outline_python(text: str, lines: list[str]) -> dict[str, Any]:
    """AST-based outline for Python files."""
    imports: list[str] = []
    classes: list[dict[str, Any]] = []
    functions: list[dict[str, Any]] = []

    try:
        tree = ast.parse(text)
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
            names = []
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
                    sig = _py_func_signature(body_node, lines)
                    methods.append(sig)
            classes.append({
                "name": node.name,
                "line": node.lineno,
                "bases": bases,
                "methods": methods,
            })
        elif isinstance(node, _PY_FUNC_TYPES):
            sig = _py_func_signature(node, lines)
            functions.append({
                "name": node.name,
                "line": node.lineno,
                "signature": sig,
            })

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


def _py_func_signature(node: ast.FunctionDef | ast.AsyncFunctionDef, lines: list[str]) -> str:
    """Reconstruct a Python function signature from AST + source lines.

    For decorated functions, node.lineno points to the first decorator, so we
    scan forward from that line to find the actual 'def' line.
    """
    try:
        line_idx = node.lineno - 1
        # For decorated functions, lineno is the first decorator — scan forward
        # to find the actual def/async def line
        for offset in range(20):  # safety limit
            idx = line_idx + offset
            if 0 <= idx < len(lines):
                raw_line = lines[idx].strip()
                if raw_line.startswith(("def ", "async def ")):
                    sig = raw_line.rstrip(":")
                    return sig
            else:
                break
        # If we didn't find it, use the original lineno
        if 0 <= line_idx < len(lines):
            raw_line = lines[line_idx].strip().rstrip(":")
            return raw_line
    except (IndexError, Exception):
        pass

    # Fallback: reconstruct from AST
    try:
        return ast.unparse(node).split("\n")[0].rstrip(":")
    except (AttributeError, Exception):
        args = ", ".join(a.arg for a in node.args.args)
        return f"def {node.name}({args})"


# ---------------------------------------------------------------------------
# Generic / unknown
# ---------------------------------------------------------------------------

_GENERIC_CLASS_RE = re.compile(
    r"^(class|struct|interface|trait|enum)\s+\w+", re.IGNORECASE
)
_GENERIC_FUNC_RE = re.compile(
    r"^(def|func|function|fn|sub|void|public|private|protected|static)\s+\w+\s*\(",
    re.IGNORECASE,
)
_GENERIC_IMPORT_RE = re.compile(
    r"^(import|use|include|require|from)\s", re.IGNORECASE
)


def _outline_generic(lines: list[str]) -> dict[str, Any]:
    """Generic regex-based outline for unknown file types."""
    imports: list[str] = []
    classes: list[dict[str, Any]] = []
    functions: list[dict[str, Any]] = []

    for i, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped:
            continue

        if _GENERIC_IMPORT_RE.match(stripped):
            imports.append(stripped)
        elif _GENERIC_CLASS_RE.match(stripped):
            parts = stripped.split()
            name = parts[1] if len(parts) > 1 else stripped
            classes.append({
                "name": name,
                "line": i,
                "bases": [],
                "methods": [],
            })
        elif _GENERIC_FUNC_RE.match(stripped):
            sig = stripped.rstrip("{").strip()
            name = stripped.split("(")[0].split()[-1] if "(" in stripped else stripped.split()[-1]
            functions.append({
                "name": name,
                "line": i,
                "signature": sig,
            })

    return {
        "language": "unknown",
        "imports": imports,
        "classes": classes,
        "functions": functions,
    }


def list_directory(workspace_root: Path, target: Path) -> dict[str, Any]:
    if not target.exists():
        return {"ok": False, "error": f"not found: {target.relative_to(workspace_root)}"}
    if not target.is_dir():
        return {"ok": False, "error": f"not a directory: {target.relative_to(workspace_root)}"}
    files: list[str] = []
    dirs: list[str] = []
    for entry in sorted(target.iterdir()):
        if entry.name.startswith(".") or entry.name in SKIP_DIRS:
            continue
        if entry.is_dir():
            dirs.append(entry.name + "/")
        elif entry.suffix in SKIP_FILE_SUFFIXES:
            continue
        else:
            files.append(entry.name)
    rel = target.relative_to(workspace_root).as_posix() or "."
    return {"ok": True, "path": rel, "directories": dirs, "files": files}


def glob_files(workspace_root: Path, pattern: str) -> dict[str, Any]:
    matches: list[str] = []
    for p in workspace_root.rglob(pattern):
        if _should_skip(p.relative_to(workspace_root)):
            continue
        if p.is_file():
            matches.append(p.relative_to(workspace_root).as_posix())
        if len(matches) >= MAX_GLOB_RESULTS:
            break
    return {
        "ok": True,
        "pattern": pattern,
        "matches": matches,
        "truncated": len(matches) >= MAX_GLOB_RESULTS,
    }
