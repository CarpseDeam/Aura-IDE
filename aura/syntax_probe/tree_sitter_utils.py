"""Shared internal helper for tree-sitter syntax checking.

Provides ``_tree_sitter_check`` which returns a
``(evidence, line, column, message)`` tuple for a given file and
language name.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _tree_sitter_check(
    file_path: str | Path,
    language_name: str,
) -> tuple[str, int | None, int | None, str]:
    """Check *file_path* for syntax errors using the tree-sitter *language_name*.

    Loads the language grammar from ``tree_sitter_language_pack``, parses the
    file content, and walks the parse tree for ``ERROR`` or ``MISSING`` nodes.

    Returns a ``(evidence, line, column, message)`` tuple where *evidence* is
    one of ``"pass"``, ``"fail"``, or ``"no_evidence"``.

    * ``"pass"`` — the file parsed without error or missing nodes.
    * ``"fail"`` — at least one ``ERROR`` or ``MISSING`` node was found;
      *line* and *column* point to the first such node (1-based).
    * ``"no_evidence"`` — the language grammar is unavailable, the file
      could not be read, or parsing itself failed.
    """
    resolved = Path(file_path).resolve(strict=False)
    path_str = str(resolved)

    if not resolved.is_file():
        return ("no_evidence", None, None, f"File not found: {path_str}")

    # ---- lazy import so callers without tree-sitter installed can still
    #      import this module safely.
    try:
        import tree_sitter as _ts
        import tree_sitter_language_pack as _lp
    except ImportError as exc:
        return ("no_evidence", None, None, f"tree-sitter not available: {exc}")

    # ---- load language --------------------------------------------------
    try:
        language = _lp.get_language(language_name)
    except Exception as exc:
        logger.debug("get_language(%r) failed: %s", language_name, exc)
        return ("no_evidence", None, None, f"Language not available: {language_name}")

    if language is None:
        return ("no_evidence", None, None, f"Language not found: {language_name}")

    # ---- parse ----------------------------------------------------------
    try:
        parser = _ts.Parser(language)
        content = resolved.read_bytes()
        tree = parser.parse(content)
    except Exception as exc:
        logger.debug("tree-sitter parse failed for %s: %s", path_str, exc)
        return ("no_evidence", None, None, f"Parse failed: {exc}")

    if tree is None:
        return ("no_evidence", None, None, "Parser returned no tree")

    # ---- walk tree for ERROR / MISSING nodes ----------------------------
    root = tree.root_node

    def _find_first_error(node: Any) -> tuple[int, int] | None:
        """Recursively search *node* and its children for ERROR/MISSING.

        Returns ``(line, column)`` (0-based) of the first such node found
        via depth-first traversal, or ``None``.
        """
        if node.type in ("ERROR", "MISSING"):
            pt = node.start_point
            return (pt.row, pt.column)
        for child in node.children:
            result = _find_first_error(child)
            if result is not None:
                return result
        return None

    error_pos = _find_first_error(root)

    if error_pos is None:
        return ("pass", None, None, "")

    line_0, column_0 = error_pos
    line_1 = line_0 + 1  # convert to 1-based
    return (
        "fail",
        line_1,
        column_0,
        f"Syntax error at line {line_1}",
    )
