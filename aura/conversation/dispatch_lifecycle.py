"""Canonical dispatch lifecycle predicates."""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Sentinel constants (not a full enum — cheap, no dependency)
# ---------------------------------------------------------------------------

_TERMINAL_BLOCKER_REASONS: frozenset[str] = frozenset({"limit", "repeated"})

_TERMINAL_STATUSES: frozenset[str] = frozenset({
    "approval_rejected",
    "cancelled",
})

_USER_VISIBLE_EXTRAS: frozenset[str] = frozenset({
    "user_visible_blocker",
    "user_only_blocker",
    "terminal_environment_blocker",
})

# ---------------------------------------------------------------------------
# Input normalisation
# ---------------------------------------------------------------------------


def _normalise_source(
    source: Any,
    extras_override: dict[str, Any] | None = None,
    **overrides: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(extras, meta)`` from whatever the caller passed.

    ``source`` may be a ``WorkerDispatchResult`` (duck-typed via
    ``.extras``), a JSON-parsed dict payload (which may contain an
    ``extras`` key), or a bare extras dict.

    ``extras_override`` lets the caller supply extras separately (e.g.
    ChatView already parsed ``data`` and ``extras``).

    ``overrides`` are keyword-only values that take precedence over
    anything extracted from *source*.
    """
    # ---- extras -----------------------------------------------------------
    if extras_override is not None:
        extras = dict(extras_override)
    elif source is None:
        extras = {}
    elif hasattr(source, "extras"):
        # WorkerDispatchResult (or anything with an .extras attribute)
        extras = dict(getattr(source, "extras", {}) or {})
    elif isinstance(source, dict):
        inner = source.get("extras")
        if isinstance(inner, dict):
            extras = dict(inner)
        else:
            # Bare extras dict — keys like recoverable,
            # dispatch_spec_rejected, etc.
            extras = {str(k): v for k, v in source.items()}
    else:
        extras = {}

    # ---- meta -------------------------------------------------------------
    meta: dict[str, Any] = {}

    # Pull from WorkerDispatchResult attributes (duck-typed)
    if hasattr(source, "cancelled"):
        meta["cancelled"] = bool(getattr(source, "cancelled", False))
    if hasattr(source, "ok"):
        meta["ok"] = bool(getattr(source, "ok", False))
    if hasattr(source, "recoverable"):
        meta["recoverable"] = bool(getattr(source, "recoverable", False))
    if hasattr(source, "needs_followup"):
        meta["needs_followup"] = bool(getattr(source, "needs_followup", False))
    if hasattr(source, "phase_boundary"):
        meta["phase_boundary"] = bool(getattr(source, "phase_boundary", False))
    if hasattr(source, "status"):
        meta["status"] = str(getattr(source, "status", "") or "")
    if hasattr(source, "mismatch"):
        meta["mismatch"] = getattr(source, "mismatch", None)

    # Pull from dict payload (may override attribute-derived values)
    if isinstance(source, dict):
        meta.setdefault("cancelled", bool(source.get("cancelled", False)))
        meta.setdefault("ok", bool(source.get("ok", False)))
        meta.setdefault("recoverable", bool(source.get("recoverable", False)))
        meta.setdefault("needs_followup", bool(source.get("needs_followup", False)))
        meta.setdefault("phase_boundary", bool(source.get("phase_boundary", False)))
        meta.setdefault("status", str(source.get("status", "") or ""))
        if source.get("mismatch") is not None:
            meta.setdefault("mismatch", source.get("mismatch"))
        if source.get("failure_constraint"):
            meta.setdefault("failure_constraint", str(source.get("failure_constraint", "")))

    # Extras override: when an attribute carries a default (False/empty) but
    # extras has a non-default value, prefer extras.  Extras can only *upgrade*
    # (False→True, empty→non-empty), never downgrade.
    if extras.get("recoverable"):
        meta["recoverable"] = True
    if extras.get("needs_followup"):
        meta["needs_followup"] = True
    if extras.get("phase_boundary"):
        meta["phase_boundary"] = True
    meta.setdefault("status", str(extras.get("status", extras.get("outcome_status", "")) or ""))
    if extras.get("status"):
        meta["status"] = str(extras["status"])
    meta.setdefault("failure_constraint", str(extras.get("failure_constraint", "") or ""))
    if extras.get("failure_constraint"):
        meta["failure_constraint"] = str(extras["failure_constraint"])

    # dispatch_spec_rejected may be in extras OR top-level payload
    if isinstance(source, dict) and source.get("dispatch_spec_rejected"):
        meta["dispatch_spec_rejected"] = True
    elif extras.get("dispatch_spec_rejected"):
        meta["dispatch_spec_rejected"] = True
    else:
        meta.setdefault("dispatch_spec_rejected", False)

    # Keyword overrides (highest precedence)
    for key in ("cancelled", "ok", "recoverable", "needs_followup",
                "phase_boundary", "status", "failure_constraint",
                "dispatch_spec_rejected", "blocker_reason"):
        if key in overrides and overrides[key] is not None:
            meta[key] = overrides[key]

    return extras, meta


# ---------------------------------------------------------------------------
# Public predicates
# ---------------------------------------------------------------------------


def is_user_visible_dispatch_blocker(
    source: Any = None,
    *,
    extras: dict[str, Any] | None = None,
    **overrides: Any,
) -> bool:
    """Return True when a dispatch failure should be surfaced to the user."""
    ex, meta = _normalise_source(source, extras, **overrides)

    # Explicit user-visible / environment flags
    if ex.get("user_visible_blocker") or ex.get("user_only_blocker"):
        return True
    if ex.get("terminal_environment_blocker"):
        return True

    # Cancelled is always surfaced
    if meta.get("cancelled"):
        return True

    # Approval rejected is surfaced
    if meta.get("status") == "approval_rejected":
        return True

    # Not ok surfaces.
    if not meta.get("ok", True):
        return True

    return False


def is_terminal_dispatch_blocker(
    source: Any = None,
    *,
    extras: dict[str, Any] | None = None,
    blocker_reason: str = "",
    **overrides: Any,
) -> bool:
    """Return True when the Planner loop must stop after this dispatch result."""
    ex, meta = _normalise_source(source, extras, blocker_reason=blocker_reason, **overrides)

    # Explicit terminal signals
    if meta.get("cancelled"):
        return True
    if (blocker_reason or "") in _TERMINAL_BLOCKER_REASONS:
        return True
    if ex.get("user_visible_blocker") or ex.get("user_only_blocker"):
        return True
    if ex.get("terminal_environment_blocker"):
        return True
    if meta.get("status") in _TERMINAL_STATUSES:
        return True

    # Success completions are terminal
    if meta.get("ok"):
        return True
    if meta.get("status") in ("completed", "completed_with_caveats"):
        return True

    # Harness error without any recovery signal → terminal
    if meta.get("status") == "harness_error":
        if not meta.get("recoverable") and not meta.get("needs_followup") and not meta.get("phase_boundary"):
            return True

    # Phase-boundary remains non-terminal.
    if meta.get("phase_boundary"):
        return False

    # Default: not ok + no recovery → terminal
    if not meta.get("ok", True):
        return True

    return False


# ---------------------------------------------------------------------------
# Convenience: single-call classification
# ---------------------------------------------------------------------------


def classify_dispatch_result(
    source: Any = None,
    *,
    extras: dict[str, Any] | None = None,
    blocker_reason: str = "",
    **overrides: Any,
) -> dict[str, bool]:
    """Return a classification dict with user-visible and terminal predicates."""
    return {
        "user_visible": is_user_visible_dispatch_blocker(
            source, extras=extras, **overrides
        ),
        "terminal": is_terminal_dispatch_blocker(
            source, extras=extras, blocker_reason=blocker_reason, **overrides
        ),
    }


__all__ = [
    "is_user_visible_dispatch_blocker",
    "is_terminal_dispatch_blocker",
    "classify_dispatch_result",
]
