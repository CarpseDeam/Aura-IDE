"""Assistant display text normalization helpers.

``ExecutionLogStreamBuffer`` was removed in Phase 4B along with the
Execution Log text stream it fed; ``formatter.py`` stays because
``normalize_assistant_display_text`` is shared by assistant chat rendering.
"""

from aura.gui.execution_log_stream.formatter import (
    compact_excess_blank_lines,
    needs_section_break,
    normalize_assistant_display_text,
    normalize_execution_log_text,
    separate_glued_prose,
)

__all__ = [
    "compact_excess_blank_lines",
    "needs_section_break",
    "normalize_assistant_display_text",
    "normalize_execution_log_text",
    "separate_glued_prose",
]
