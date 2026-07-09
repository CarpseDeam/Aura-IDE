"""Shared helper for GCC/Clang compiler diagnostic parsing.

GCC and Clang share nearly identical diagnostic output formats.
This module provides regex building, message classification, and
structured extraction used by both ``c_probe`` and ``cpp_probe``.
"""
from __future__ import annotations

import os
import re


def _compiler_diag_re_for_target(target_path: str) -> re.Pattern:
    """Build a compiled regex matching GCC/Clang diagnostics for *target_path*.

    GCC/Clang diagnostics follow either format::

        <file>:<line>:<col>: <severity>: <message>
        <file>:<line>: <severity>: <message>       (column omitted)

    The returned pattern handles both ``/`` and ``\\\\`` path separators
    so that paths normalised on either platform are matched.

    Parameters
    ----------
    target_path:
        The file path to match in diagnostic output.

    Returns
    -------
    A compiled ``re.Pattern`` with named groups ``line``, ``col`` (optional),
    and ``message``.
    """
    norm_target = os.path.normpath(target_path)
    escaped = re.escape(norm_target)
    # Allow forward and backslash path separators interchangeably.
    escaped = escaped.replace("\\/", r"[\\/]")
    escaped = escaped.replace("\\\\", r"[\\/]")
    # Group 1: line, Group 2 (optional): column, Group 3: message
    return re.compile(
        r"^" + escaped + r":(\d+)(?::(\d+))?:\s*(?:error|warning|note|fatal error):\s*(.+)$",
        re.MULTILINE,
    )


# Keywords that indicate a diagnostic *is* a syntax error.
_SYNTAX_KEYWORDS: tuple[str, ...] = (
    "expected",
    "parse error",
    "stray",
    "missing terminating",
    "expected ';'",
)

# Keywords that indicate a diagnostic is *not* a syntax error.
_UNRELATED_KEYWORDS: tuple[str, ...] = (
    "fatal error:",
    "no such file",
    "undefined reference",
    "collect2:",
    "required from",
    "initializer element is not constant",
)


def _is_syntax_diagnostic(message: str) -> bool:
    """Return ``True`` if the diagnostic message appears to be a syntax error.

    Checks for known syntax-related keywords such as *expected*,
    *parse error*, *stray*, *missing terminating*, etc.

    Returns ``False`` for include errors, linker errors, symbol resolution
    issues, ``fatal error:``, ``no such file``, ``undefined reference``,
    ``collect2:``, ``required from`` context notes, etc.
    """
    lower = message.lower()
    for kw in _SYNTAX_KEYWORDS:
        if kw.lower() in lower:
            return True
    return False


def _is_unrelated_diagnostic(message: str) -> bool:
    """Return ``True`` when *message* is clearly a non-syntax diagnostic.

    Returns ``True`` for include errors, missing header files, linker
    errors/symbol resolution, and context notes.
    """
    lower = message.lower()
    for kw in _UNRELATED_KEYWORDS:
        if kw.lower() in lower:
            return True
    return False


def _parse_compiler_diagnostic(
    stderr: str,
    target_path: str,
) -> tuple[int, int, str] | None:
    """Extract *(line, column, message)* for a confirmed syntax diagnostic.

    Parses *stderr* from GCC/Clang for diagnostics that reference
    *target_path*.  Only returns a result when **both** conditions hold:

    * the diagnostic line references *target_path*;
    * the message is classified as a syntax error (not include/linker/symbol).

    Parameters
    ----------
    stderr:
        Raw stderr output from the compiler.
    target_path:
        The file whose diagnostics to extract.

    Returns
    -------
    A ``(line, column, message)`` tuple when a syntax error is found,
    otherwise ``None``.  *column* is ``1`` when the compiler omits it.
    """
    pattern = _compiler_diag_re_for_target(target_path)

    for m in pattern.finditer(stderr):
        line = int(m.group(1))
        col = int(m.group(2)) if m.group(2) is not None else 1
        message = m.group(3).strip()

        if _is_unrelated_diagnostic(message):
            continue
        if _is_syntax_diagnostic(message):
            return line, col, message

    return None
