# Import adapters to trigger registration — text first for last-match-wins semantics
from aura.code_intel import (  # isort: skip
    text_adapter,  # noqa: F401
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
