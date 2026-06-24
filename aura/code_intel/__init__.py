# Import adapters to trigger registration — text first for last-match-wins semantics
from aura.code_intel import (  # isort: skip
    text_adapter,  # noqa: F401
    generic_adapter,  # noqa: F401
    python_adapter,  # noqa: F401
)

from aura.code_intel.adapter import (  # isort: skip
    ADAPTER_REGISTRY,
    CodeIntelAdapter,
    get_adapter,
    register_adapter,
)
from aura.code_intel.audit import audit_changed_files
from aura.code_intel.index import CodeIntelIndex

# Self-check: verify adapter resolution is correct
_py = get_adapter("test.py")
_txt = get_adapter("test.xyz")
assert _py is not None and _py.language_id == "python", \
    f"Python adapter mismatch: expected python, got {_py.language_id if _py else None}"
assert _txt is not None and _txt.language_id == "text", \
    f"Text fallback mismatch: expected text, got {_txt.language_id if _txt else None}"
# Optional generic adapter checks (only when tree-sitter is installed)
_js = get_adapter("app.js")
if _js is not None and _js.language_id == "javascript":
    _ts = get_adapter("app.ts")
    _tsx = get_adapter("app.tsx")
    _go = get_adapter("app.go")
    _rs = get_adapter("app.rs")
    assert _ts is not None and _ts.language_id == "typescript"
    assert _tsx is not None and _tsx.language_id == "tsx"
    assert _go is not None and _go.language_id == "go"
    assert _rs is not None and _rs.language_id == "rust"
from aura.code_intel.models import (
    AuditFinding,
    FileInfo,
    ParseDiagnostic,
    ReferenceEdge,
    SymbolInfo,
)

__all__ = [
    "ADAPTER_REGISTRY",
    "AuditFinding",
    "CodeIntelAdapter",
    "CodeIntelIndex",
    "FileInfo",
    "ParseDiagnostic",
    "ReferenceEdge",
    "SymbolInfo",
    "audit_changed_files",
    "get_adapter",
    "register_adapter",
]
