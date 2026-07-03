"""UI contract helpers for web-research execution."""

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
    """Return the explicit UI/browser contract for a research run."""
    resolved_ui_mode = (
        RESEARCH_UI_MODE_VISIBLE
        if str(ui_mode or "").strip().lower() == RESEARCH_UI_MODE_VISIBLE
        else RESEARCH_UI_MODE_SILENT
    )
    silent = resolved_ui_mode == RESEARCH_UI_MODE_SILENT
    return {
        "route": route or ANSWER_ONLY_RESEARCH_ROUTE,
        "ui_mode": resolved_ui_mode,
        "silent": silent,
        "headless": silent,
        "visible": not silent,
        "answer_only": route == ANSWER_ONLY_RESEARCH_ROUTE,
        "no_work_surface": silent,
    }


def with_research_ui_contract(
    upstream: dict[str, Any] | None,
    *,
    route: str = ANSWER_ONLY_RESEARCH_ROUTE,
    ui_mode: str = RESEARCH_UI_MODE_SILENT,
) -> dict[str, Any]:
    """Merge the research UI contract into an upstream payload."""
    merged = dict(upstream or {})
    contract = research_ui_contract(route=route, ui_mode=ui_mode)
    existing = merged.get("research_ui")
    if isinstance(existing, dict):
        contract = {**contract, **existing}
        contract["ui_mode"] = (
            RESEARCH_UI_MODE_VISIBLE
            if str(contract.get("ui_mode") or "").strip().lower() == RESEARCH_UI_MODE_VISIBLE
            else RESEARCH_UI_MODE_SILENT
        )
        silent = contract["ui_mode"] == RESEARCH_UI_MODE_SILENT
        contract["silent"] = silent
        contract["headless"] = silent
        contract["visible"] = not silent
        contract["no_work_surface"] = silent
    merged["research_ui"] = contract
    merged.setdefault("ui_mode", contract["ui_mode"])
    merged.setdefault("headless", contract["headless"])
    merged.setdefault("silent", contract["silent"])
    merged.setdefault("visible", contract["visible"])
    return merged


def silent_research_requested(
    *,
    upstream: dict[str, Any] | None = None,
    input_payload: dict[str, Any] | None = None,
) -> bool:
    """Return whether the caller requested silent/headless research."""
    for source in (input_payload, upstream):
        if not isinstance(source, dict):
            continue
        contract = source.get("research_ui")
        if isinstance(contract, dict):
            if _bool_value(contract.get("headless")) is True:
                return True
            if _bool_value(contract.get("silent")) is True:
                return True
            if str(contract.get("ui_mode") or "").strip().lower() == RESEARCH_UI_MODE_SILENT:
                return True
        if _bool_value(source.get("headless")) is True:
            return True
        if _bool_value(source.get("silent")) is True:
            return True
        if str(source.get("ui_mode") or "").strip().lower() == RESEARCH_UI_MODE_SILENT:
            return True
    return False


def research_subprocess_env(
    *,
    upstream: dict[str, Any] | None = None,
    input_payload: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Environment overrides for subprocesses that can honor silent research."""
    if not silent_research_requested(upstream=upstream, input_payload=input_payload):
        return {}
    return {
        "AURA_RESEARCH_UI_MODE": RESEARCH_UI_MODE_SILENT,
        "AURA_WEB_RESEARCH_HEADLESS": "1",
        "AURA_WEB_RESEARCH_VISIBLE": "0",
    }


def env_requests_headless(environ: dict[str, str] | None = None) -> bool | None:
    """Read headless/silent intent from environment variables."""
    env = environ or os.environ
    ui_mode = str(env.get("AURA_RESEARCH_UI_MODE", "")).strip().lower()
    if ui_mode == RESEARCH_UI_MODE_SILENT:
        return True
    if ui_mode == RESEARCH_UI_MODE_VISIBLE:
        return False

    headless = _env_bool(env.get("AURA_WEB_RESEARCH_HEADLESS"))
    if headless is not None:
        return headless

    visible = _env_bool(env.get("AURA_WEB_RESEARCH_VISIBLE"))
    if visible is not None:
        return not visible
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
