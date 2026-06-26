"""Deterministic character-budget helpers for assembling a Worker Context Pack."""

from __future__ import annotations

_TRUNCATION_MARKER = "\n... [truncated]"


def truncate(text: str, max_chars: int) -> str:
    """Truncate *text* to *max_chars*, appending the truncation marker if cut.

    The returned string is guaranteed to be at most *max_chars* characters.
    """
    if len(text) <= max_chars:
        return text
    available = max_chars - len(_TRUNCATION_MARKER)
    if available <= 0:
        return _TRUNCATION_MARKER[:max_chars]
    return text[:available] + _TRUNCATION_MARKER


class BudgetTracker:
    """Tracks a character budget across sections.

    Sections are added sequentially.  Once the budget is exhausted (or a
    section is truncated), subsequent calls to ``add_section`` return False
    without adding anything.
    """

    def __init__(self, max_chars: int) -> None:
        self._max_chars = max_chars
        self._sections: list[str] = []
        self._truncated = False
        self._total = 0

    def add_section(self, text: str) -> bool:
        """Add *text* if it fits within the remaining budget.

        Returns True if the section was added (possibly truncated).  Returns
        False once the budget is exhausted.
        """
        if self._truncated:
            return False

        remaining = self._max_chars - self._total
        if remaining <= 0:
            self._truncated = True
            return False

        # Reserve space for the "\n\n" separator if this is not the first section
        separator_overhead = 2 if self._sections else 0
        available = remaining - separator_overhead
        if available <= 0:
            self._truncated = True
            return False

        if len(text) > available:
            text = truncate(text, available)
            self._truncated = True

        self._sections.append(text)
        self._total += len(text) + separator_overhead
        return True

    @property
    def content(self) -> str:
        """Joined section text.

        The truncation marker is already embedded in the last section's text
        if truncation occurred.
        """
        return "\n\n".join(self._sections)

    @property
    def remaining(self) -> int:
        return max(0, self._max_chars - self._total)

    @property
    def truncated(self) -> bool:
        return self._truncated

    @property
    def total(self) -> int:
        return self._total
