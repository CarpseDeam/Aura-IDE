"""The production SINGLE exit tools: structured blocker and already-satisfied.

Both are ordinary tools on the stable production catalog — the model calls them
from the normal agent loop when it cannot truthfully continue an implementation
turn.  They mutate nothing; each turns the model's stated outcome into a
structured result the send loop and the completion receipt can recognise as a
truthful terminal outcome.
"""
from __future__ import annotations

#: Exit hatch for an attempt that cannot be carried out: names the external
#: reason and what is needed.  A successful call ends the turn blocked.
REPORT_BLOCKER: str = "report_blocker"

#: Exit hatch for an attempt the repository already satisfies: the model
#: inspected authoritative evidence and records that the requested state
#: already exists, so no change is required.  A successful call ends the turn
#: already-satisfied.
REPORT_ALREADY_SATISFIED: str = "report_already_satisfied"

__all__ = [
    "REPORT_ALREADY_SATISFIED",
    "REPORT_BLOCKER",
]
