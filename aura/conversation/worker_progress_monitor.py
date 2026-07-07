"""One progress monitor, no accumulated budgets.

Uses the fingerprint-plus-write-attempt stall rule from
``validation_failure_routing.py``: something is stuck iff its failure
signature is identical to the previous pass and no write attempt happened
since.

Target state: every subsystem that asks "is the Worker making progress?"
asks the same question the same way.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ProgressVerdict:
    """What the progress monitor concluded for this check.

    Attributes:
        progressing: ``True`` when the tracked signal is still changing
            (fingerprint differs or writes occurred).
        reason: Short description — ``"progressing"`` or ``"stalled"``.
    """
    progressing: bool
    reason: str = ""


class WorkerProgressMonitor:
    """Tracks the last-seen fingerprint and write count across checks.

    Each call to ``check()`` compares the current fingerprint and write
    count against the stored values to decide stalled *vs.* progressing.

    There are no attempt counters, budgets, or hard limits — the verdict
    is derived fresh each turn from content (fingerprint) and behaviour
    (write count).

    Usage::

        monitor = WorkerProgressMonitor()
        while True:
            fp = compute_fingerprint(...)
            verdict = monitor.check(fp, write_count)
            if verdict.progressing:
                # keep going — the signal is still evolving
            else:
                # identical fingerprint, no writes — stalled
    """

    def __init__(self) -> None:
        self._last_fingerprint: str | None = None
        self._last_write_count: int = 0

    def check(self, fingerprint: str, write_count: int) -> ProgressVerdict:
        """Decide whether the tracked signal is progressing or stalled.

        Parameters
        ----------
        fingerprint:
            A caller-defined string that captures the current state of
            the signal being monitored (e.g. reason + steering + message
            content).  The caller chooses what to include — the monitor
            only compares for equality.
        write_count:
            The total number of write attempts so far (e.g. the value
            from ``_SendState.write_attempt_count()``).

        Returns
        -------
        ProgressVerdict
            ``progressing=True`` when the fingerprint differs from the
            last seen value *or* writes have occurred since the last
            check; ``progressing=False`` (stalled) when the fingerprint
            matches *and* no new writes have been observed.
        """
        progressing = (
            self._last_fingerprint is None
            or fingerprint != self._last_fingerprint
            or write_count > self._last_write_count
        )
        if progressing:
            self._last_fingerprint = fingerprint
            self._last_write_count = write_count
            return ProgressVerdict(progressing=True, reason="progressing")
        return ProgressVerdict(progressing=False, reason="stalled")

    def reset(self) -> None:
        """Clear stored state so the next ``check()`` starts fresh."""
        self._last_fingerprint = None
        self._last_write_count = 0


__all__ = [
    "ProgressVerdict",
    "WorkerProgressMonitor",
]
