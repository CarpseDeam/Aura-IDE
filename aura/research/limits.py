"""Research limits and deadline helpers."""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(frozen=True)
class ResearchLimits:
    """Bounds for a research session."""

    max_pages: int = 5
    max_attempts: int = 3
    timeout_seconds: float = 60.0


class Deadline:
    """Monotonic deadline for timing out an operation."""

    def __init__(self, timeout_seconds: float) -> None:
        self._start: float = time.monotonic()
        self.timeout_seconds = timeout_seconds

    def expired(self) -> bool:
        """Return True if the deadline has passed."""
        return time.monotonic() - self._start >= self.timeout_seconds

    def remaining(self) -> float:
        """Return the remaining time in seconds (capped at 0)."""
        return max(0.0, self.timeout_seconds - (time.monotonic() - self._start))


DEFAULT_LIMITS = ResearchLimits()
