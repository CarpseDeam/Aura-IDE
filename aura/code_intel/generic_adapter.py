"""Tree-sitter-backed code-intelligence adapter for non-Python languages.

Provides symbols and outlines using tree-sitter grammar parsing and
tags queries.  References and dependency resolution are not implemented
(return empty lists) — this adapter lights up repo maps and
removed-public-symbol blocking for non-Python languages without
pretending cross-file import resolution exists yet.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from aura.code_intel.adapter import CodeIntelAdapter, register_adapter
from aura.resources import get_resource_path

logger = logging.getLogger(__name__)

# Wrap tree-sitter imports in try/except so importing this module is safe
# when the packages are not installed.
try:
    import tree_sitter as _ts
    import tree_sitter_language_pack as _lp

    _TS_AVAILABLE = True
except ImportError:
    _TS_AVAILABLE = False
    logger.info("tree-sitter-language-pack not available; generic adapters disabled")

# ---------------------------------------------------------------------------
# Build note for packaging
#
# Packaging MUST pre-warm the bundled grammars directory with
# grammar_prewarm_script() from scripts/build_nuitka.py, which downloads
# all SUPPORTED_GRAMMARS grammars from the language pack into
# <app_root>/grammars/ (the path resolved by get_resource_path("grammars")).
# Include that directory as packaged data.  Frozen-app validation must be
# done with network disabled.
# ---------------------------------------------------------------------------

# (language_id, extensions)
_SUPPORTED_LANGUAGES: list[tuple[str, tuple[str, ...]]] = [
    ("javascript", (".js", ".jsx", ".mjs", ".cjs")),
    ("typescript", (".ts",)),
    ("tsx", (".tsx",)),
    ("go", (".go",)),
    ("rust", (".rs",)),
    ("java", (".java",)),
    ("c", (".c", ".h")),
    ("cpp", (".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx")),
    ("csharp", (".cs",)),
    ("php", (".php",)),
    ("ruby", (".rb",)),
    ("swift", (".swift",)),
    ("kotlin", (".kt", ".kts")),
    ("dart", (".dart",)),
    ("scala", (".scala", ".sc")),
    ("lua", (".lua",)),
    ("bash", (".sh", ".bash", ".zsh")),
    ("powershell", (".ps1", ".psm1", ".psd1")),
    ("html", (".html", ".htm")),
    ("css", (".css",)),
    ("scss", (".scss",)),
    ("json", (".json",)),
    ("yaml", (".yaml", ".yml")),
    ("toml", (".toml",)),
    ("xml", (".xml",)),
    ("sql", (".sql",)),
    ("markdown", (".md", ".markdown")),
    ("dockerfile", ("dockerfile", ".dockerfile")),
    ("gdscript", (".gd",)),
    ("gdshader", (".gdshader",)),
    ]

_CONFIGURED = False


def _ensure_configured() -> None:
    """Configure tree-sitter-language-pack cache once at import time."""
    global _CONFIGURED
    if _CONFIGURED or not _TS_AVAILABLE:
        return
    try:
        cache_path = get_resource_path("grammars")
        if not cache_path.exists() or not cache_path.is_dir():
            logger.warning(
                "Bundled grammars path %s not found; "
                "packager should pre-warm grammars with "
                "_lp.download(...) against the pinned version",
                cache_path,
            )
            _CONFIGURED = True
            return

        pack_config_cls = getattr(_lp, "PackConfig", None)
        if pack_config_cls is not None:
            _lp.configure(pack_config_cls(cache_dir=str(cache_path)))
        else:
            _lp.configure({"cache_dir": str(cache_path)})
    except Exception:
        logger.warning("Failed to configure tree-sitter cache", exc_info=True)
    _CONFIGURED = True


class GenericSymbolAdapter(CodeIntelAdapter):
    """Tree-sitter-backed adapter for a single non-Python language.

    Provides symbol and outline extraction via tree-sitter grammar
    parsing and tags queries.  References and dependencies return empty
    lists — no cross-file import resolution.
    """

    def __init__(
        self, language_id: str, extensions: tuple[str, ...]
    ) -> None:
        self._language_id = language_id
        self._extensions = extensions
        self._language: Any | None = None
        self._parser: Any | None = None
        self._tags_query_source: str | None = None
        self._tags_query: Any | None = None
        self._initialized = False

    @property
    def language_id(self) -> str:
        return self._language_id

    def detect(self, file_path: str, content: str | None = None) -> bool:
        """Return True when file suffix or basename matches this adapter's extensions."""
        p = Path(file_path)
        suffix = p.suffix.lower()
        if suffix in self._extensions:
            return True
        # Also check basename for extensions that are full filenames (e.g. "dockerfile" without leading dot)
        name = p.name.lower()
        for ext in self._extensions:
            if not ext.startswith(".") and name == ext:
                return True
        return False

    def _lazy_init(self) -> bool:
        """Load language, parser, and tags query on first use.

        Returns False if initialization fails or the grammar is missing.
        """
        if self._initialized:
            return self._parser is not None

        self._initialized = True

        if not _TS_AVAILABLE:
            return False

        _ensure_configured()

        try:
            downloaded = _lp.downloaded_languages()
            if self._language_id not in downloaded:
                logger.info(
                    "Tree-sitter grammar for '%s' not in downloaded set; no on-demand fetch performed",
                    self._language_id,
                )
                return False
        except Exception:
            logger.warning(
                "Failed to check downloaded languages for '%s'",
                self._language_id,
                exc_info=True,
            )
            return False

        try:
            self._language = _lp.get_language(self._language_id)
            # Use tree_sitter.Parser (not language-pack's get_parser) so
            # parsed nodes are tree_sitter.Node instances compatible with
            # tree_sitter.QueryCursor.
            self._parser = _ts.Parser(self._language)
        except Exception:
            logger.exception(
                "Failed to initialize tree-sitter for '%s'", self._language_id
            )
            return False

        # Resolve tags query directly — no fallback
        self._tags_query_source = _lp.get_tags_query(self._language_id)
        if self._tags_query_source is not None:
            try:
                self._tags_query = _ts.Query(
                    self._language, self._tags_query_source
                )
            except Exception:
                logger.exception(
                    "Failed to compile tags query for '%s'", self._language_id
                )

        return True

    def parse(
        self, file_path: str, content: str
    ) -> tuple[list[Any], list[Any], list[Any]]:
        """Parse file content and return (symbols, references, diagnostics).

        References are always empty for this adapter.
        """
        from aura.code_intel.models import ParseDiagnostic, SymbolInfo

        symbols: list[SymbolInfo] = []
        diags: list[ParseDiagnostic] = []

        if not self._lazy_init():
            diags.append(
                ParseDiagnostic(
                    file=file_path,
                    line=None,
                    message=(
                        f"Tree-sitter grammar for '{self._language_id}' "
                        "not available"
                    ),
                    severity="warning",
                )
            )
            return (symbols, [], diags)

        # Parse to tree-sitter tree (tree-sitter is lenient — never raises)
        utf8_bytes = content.encode("utf-8")
        tree = self._parser.parse(utf8_bytes)

        # Tree-sitter parse errors are tolerant, not fatal
        if tree is not None and tree.root_node.has_error:
            diags.append(
                ParseDiagnostic(
                    file=file_path,
                    line=None,
                    message=(
                        f"Tree-sitter reported parse errors in "
                        f"'{self._language_id}' file"
                    ),
                    severity="warning",
                )
            )

        if tree is None or self._tags_query is None:
            return (symbols, [], diags)

        # Run tags query to extract symbol captures.
        # In tree_sitter 0.25.x, QueryCursor.matches(node) returns
        # an iterator of (pattern_index, captures_dict) tuples
        # where captures_dict maps capture_name -> list[Node].
        try:
            cursor = _ts.QueryCursor(self._tags_query)
            for pattern_index, captures in cursor.matches(tree.root_node):
                kind = None
                name_node = None

                for capture_name, nodes in captures.items():
                    if capture_name.startswith("definition."):
                        _, def_kind = capture_name.split(".", 1)
                        if def_kind in ("class", "function", "method", "constant"):
                            kind = def_kind
                        else:
                            kind = "variable"
                    elif capture_name == "name":
                        if nodes:
                            name_node = nodes[0]

                if name_node is not None and kind is not None:
                    name_bytes = utf8_bytes[name_node.start_byte:name_node.end_byte]
                    name = name_bytes.decode("utf-8", errors="replace")
                    line = name_node.start_point[0] + 1  # 0-based → 1-based
                    column = name_node.start_point[1]

                    symbols.append(
                        SymbolInfo(
                            name=name,
                            kind=kind,
                            file=file_path,
                            line=line,
                            column=column,
                        )
                    )
        except Exception as exc:
            diags.append(
                ParseDiagnostic(
                    file=file_path,
                    line=None,
                    message=f"Tags query error: {exc}",
                    severity="warning",
                )
            )

        return (symbols, [], diags)

    def outline(self, file_path: str, content: str) -> dict[str, Any]:
        """Return structural outline from parsed symbols."""
        symbols, _, _ = self.parse(file_path, content)
        classes: list[dict[str, Any]] = []
        functions: list[dict[str, Any]] = []

        for sym in symbols:
            entry: dict[str, Any] = {"name": sym.name, "line": sym.line}
            if sym.kind == "class":
                entry["bases"] = []
                entry["methods"] = []
                classes.append(entry)
            elif sym.kind in ("function", "method"):
                entry["signature"] = sym.signature or sym.name
                functions.append(entry)

        return {
            "language": self._language_id,
            "imports": [],
            "classes": classes,
            "functions": functions,
        }

    def symbols(self, file_path: str, content: str) -> list[Any]:
        parsed, _, _ = self.parse(file_path, content)
        return parsed

    def references(self, file_path: str, content: str) -> list[Any]:
        return []

    def dependencies(self, file_path: str, content: str) -> list[str]:
        return []


def register_generic_adapters() -> None:
    """Register one GenericSymbolAdapter per supported language."""
    if not _TS_AVAILABLE:
        return
    for language_id, extensions in _SUPPORTED_LANGUAGES:
        register_adapter(GenericSymbolAdapter(language_id, extensions))


# Auto-register at import time
register_generic_adapters()
