"""Optional report tools: structured blocker and already-satisfied.

Both are ordinary tools on the stable production catalog — the model calls them
from the normal agent loop when it wants to record why an attempt stopped or
that the requested state already exists.  They mutate nothing, and they are
bookkeeping only: calling one does not end the turn, trigger a finalization
round, or change the next request.  The completion receipt reads them as
observed evidence.
"""
from __future__ import annotations

#: Optional report for an attempt that cannot be carried out: names the
#: external reason and what is needed.  Summarized in the completion receipt.
REPORT_BLOCKER: str = "report_blocker"

#: Optional report for an attempt the repository already satisfies: the model
#: inspected authoritative evidence and records that the requested state
#: already exists, so no change is required.  Summarized in the receipt.
REPORT_ALREADY_SATISFIED: str = "report_already_satisfied"

__all__ = [
    "REPORT_ALREADY_SATISFIED",
    "REPORT_BLOCKER",
]
