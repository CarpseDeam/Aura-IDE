"""Schema for the single production semantic-inspection tool: ``inspect_code``.

Kept standalone rather than folded into ``_schemas.py`` — that module already
carries the whole withheld-but-replayable read/search/code-intel surface, and
``inspect_code`` is a distinct, focused capability rather than another entry
in that pile.
"""

from __future__ import annotations

from typing import Any

INSPECT_CODE_TOOL_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "inspect_code",
        "description": (
            "Return a bounded evidence packet about one source location or symbol, "
            "denser than a single read_file or grep_search hit. The result has four "
            "parts. 'target': the best symbol match Aura's parser found for the given "
            "line and/or symbol, with a 'resolution' field stating how it was found "
            "('exact_symbol_match', 'exact_line_match', 'nearest_preceding_declaration', "
            "or 'unresolved') and a 'provenance' field naming which parser produced it — "
            "never claimed as a resolved language reference. 'source_excerpt': actual "
            "source text in a fixed-size line window around the target, with its own "
            "start/end lines and a 'truncated' flag; the window is a bound, not a "
            "declaration's real boundaries. 'occurrences': a bounded, explicitly lexical "
            "(word-boundary text search, not language-resolved) set of other places the "
            "symbol's name appears, with total/returned/truncated counts. 'diagnostics': "
            "parser diagnostics for the file, if any. 'limitations': which semantic "
            "capabilities this result does not have (e.g. resolved references, call "
            "graphs, type inference, exact declaration ranges). Works across languages "
            "at whatever quality Aura's adapter for that file provides; an unsupported "
            "or partial adapter still returns the source excerpt and states what is "
            "missing rather than failing outright."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Workspace-relative path to the file to inspect.",
                },
                "line": {
                    "type": "integer",
                    "description": "Optional 1-based line number to anchor the inspection.",
                },
                "symbol": {
                    "type": "string",
                    "description": "Optional symbol name to anchor the inspection.",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
}
