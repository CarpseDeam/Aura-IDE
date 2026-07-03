"""UI contract helpers for web-research execution.

⚠️  Legacy module — maintained only for import compatibility.

The Research Browser Controller (``aura/browser/research_controller.py``)
now owns all browser-facing decisions for web research (visibility, headless,
profile, runtime route).  The functions below are kept as inert compatibility
shims so existing importers do not break.
"""

from __future__ import annotations

import os
from typing import Any

ANSWER_ONLY_RESEARCH_ROUTE = "answer_only"
RESEARCH_UI_MODE_SILENT = "silent"
RESEARCH_UI_MODE_VISIBLE = "visible"

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}


def research_ui_contract(
    *,
    route: str = ANSWER_ONLY_RESEARCH_ROUTE,
    ui_mode: str = RESEARCH_UI_MODE_SILENT,
) -> dict[str, Any]:
    """Return the explicit UI/browser contract for a research run.

    Returns **visible-by-default** values since the controller now owns
    browser decisions.  The return value is a plain dict for import
    compatibility only.
    """
    return {
        "route": route or ANSWER_ONLY_RESEARCH_ROUTE,
        "ui_mode": RESEARCH_UI_MODE_VISIBLE,
        "silent": False,
        "headless": False,
        "visible": True,
        "answer_only": route == ANSWER_ONLY_RESEARCH_ROUTE,
        "no_work_surface": False,
    }


def with_research_ui_contract(
    upstream: dict[str, Any] | None,
    *,
    route: str = ANSWER_ONLY_RESEARCH_ROUTE,
    ui_mode: str = RESEARCH_UI_MODE_SILENT,
) -> dict[str, Any]:
    """Merge the research UI contract into an upstream payload.

    ⚠️  Inert compatibility shim — the controller owns all browser
    decisions, so this no longer influences browser behaviour.
    """
    merged = dict(upstream or {})
    merged["research_ui"] = research_ui_contract(route=route, ui_mode=ui_mode)
    merged.setdefault("ui_mode", RESEARCH_UI_MODE_VISIBLE)
    merged.setdefault("headless", False)
    merged.setdefault("silent", False)
    merged.setdefault("visible", True)
    return merged


def silent_research_requested(
    *,
    upstream: dict[str, Any] | None = None,
    input_payload: dict[str, Any] | None = None,
) -> bool:
    """Return whether the caller requested silent/headless research.

    ⚠️  Compatibility shim — always returns ``False``.  The Research
    Browser Controller is the sole owner of browser visibility decisions.
    """
    return False


def research_subprocess_env(
    *,
    upstream: dict[str, Any] | None = None,
    input_payload: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Environment overrides for subprocesses that can honor silent research.

    ⚠️  Compatibility shim — returns an empty dict.  The controller
    manages browser launch natively without environment variable overrides.
    """
    return {}


def env_requests_headless(environ: dict[str, str] | None = None) -> bool | None:
    """Read headless/silent intent from environment variables.

    ⚠️  Compatibility shim — always returns ``None``.
    """
    return None


def _env_bool(value: str | None) -> bool | None:
    if value is None:
        return None
    return _bool_value(value)


def _bool_value(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in _TRUTHY:
        return True
    if text in _FALSY:
        return False
    return None
