from __future__ import annotations

from dataclasses import dataclass


@dataclass
class FileInfo:
    path: str  # workspace-relative posix path
    language: str  # 'python', 'typescript', 'javascript', 'text', etc.
    content_hash: str  # sha256 hex
    mtime: float  # stat().st_mtime
    size: int  # stat().st_size


@dataclass
class SymbolInfo:
    name: str
    kind: str  # 'class', 'function', 'method', 'variable', 'import', 'export'
    file: str  # workspace-relative
    line: int
    column: int | None = None
    # Parser-owned end-of-declaration coordinates. None when the adapter has
    # no honest source for them — never inferred/guessed by callers.
    end_line: int | None = None
    end_column: int | None = None
    signature: str | None = None  # human-readable, e.g. "def foo(x: int) -> str"
    parent: str | None = None  # enclosing class or function name
    docstring: str | None = None


@dataclass
class ReferenceEdge:
    source_file: str
    source_symbol: str | None  # None if reference is at module-level
    target_file: str | None  # None if unresolved
    target_symbol: str
    line: int
    kind: str  # 'import', 'call', 'usage', 'inherit', 'reference'


@dataclass
class ParseDiagnostic:
    file: str
    line: int | None
    message: str
    severity: str  # 'error', 'warning'


@dataclass
class AuditFinding:
    file: str
    line: int | None
    message: str
    severity: str  # 'error', 'warning', 'info'
    kind: str  # 'removed_export', 'stale_reference', 'parse_failure', 'unresolved_dependency'
