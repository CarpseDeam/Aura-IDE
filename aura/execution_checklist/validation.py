"""Validation and normalization helpers for execution checklist rows."""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

from aura.execution_checklist.models import (
    ExecutionChecklistItem,
    normalize_status,
)


def compact_checklist_label(value: str, fallback: str = "Execution step") -> str:
    """Return a compact display-safe label for one checklist row."""
    if not value or not value.strip():
        return fallback

    lines = [line.strip() for line in value.strip().splitlines() if line.strip()]
    if not lines:
        return fallback
    text = lines[0]

    text = re.sub(r"^#{1,6}\s+", "", text)
    text = re.sub(r"^[\-\*\+]\s+", "", text)
    text = re.sub(r"^\[[ xX]\]\s*", "", text)
    text = re.sub(r"^\d+[\.\)]\s*", "", text)

    for prefix in (
        "Step",
        "Objective",
        "Summary",
        "Acceptance",
        "Goal",
        "Task",
        "Phase",
        "Milestone",
    ):
        text = re.sub(
            rf"^{re.escape(prefix)}\s*\d*\s*[:.\-]\s*",
            "",
            text,
            flags=re.IGNORECASE,
        )

    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return fallback
    if len(text) > 90:
        text = text[:87] + "..."
    return text


def validate_checklist_items(
    items: list[ExecutionChecklistItem],
) -> list[ExecutionChecklistItem]:
    """Return normalized, displayable checklist rows with stable ids."""
    normalized: list[ExecutionChecklistItem] = []
    seen_ids: set[str] = set()
    seen_descriptions: set[str] = set()

    for index, item in enumerate(items, start=1):
        description = compact_checklist_label(
            item.description,
            fallback=item.id or f"item-{index}",
        )
        if not description or is_implementation_detail(description):
            continue
        desc_key = normalize_text(description)
        if desc_key in seen_descriptions:
            continue

        item_id = str(item.id or f"item-{index}").strip() or f"item-{index}"
        if item_id in seen_ids:
            item_id = f"{item_id}-{index}"

        normalized.append(
            replace(
                item,
                id=item_id,
                description=description,
                status=normalize_status(item.status),
                files=tuple(_dedupe(list(item.files))),
                owning_step_id=str(item.owning_step_id or "").strip(),
                metadata=dict(item.metadata),
            )
        )
        seen_ids.add(item_id)
        seen_descriptions.add(desc_key)

    return normalized


def request_is_non_trivial(request: Any) -> bool:
    """Return True when flat fallback should not collapse to one title row."""
    files = _str_list(getattr(request, "files", []))
    if len(files) > 1:
        return True
    if _str_list(getattr(request, "risk_notes", [])):
        return True
    if len(_str_list(getattr(request, "validation_commands", []))) > 1:
        return True

    text = " ".join(
        str(value or "")
        for value in (
            getattr(request, "goal", ""),
            getattr(request, "summary", ""),
            getattr(request, "spec", ""),
            getattr(request, "acceptance", ""),
            " ".join(files),
        )
    )
    return _matches_any(text, _NON_TRIVIAL_PATTERNS)


def is_implementation_detail(text: str) -> bool:
    """Return True for code/import/declaration noise, not work items."""
    stripped = str(text or "").strip()
    if not stripped:
        return True
    if _IMPORT_STMT.match(stripped):
        return True
    if _FUTURE_IMPORT.search(stripped):
        return True
    if '"""' in stripped:
        return True
    match = _FIELD_DECL.match(stripped)
    if match:
        rest = stripped[match.end() :]
        if not rest or rest.lstrip().startswith(("|", "=")):
            return True
    if _PAREN_FILLER.match(stripped):
        return True
    if _SHORT_SYMBOLIC.match(stripped):
        return True
    if _OPERATOR_LEAD.match(stripped):
        return True
    if _BARE_CODE_SYMBOL.match(stripped) and (
        stripped.startswith("_") or stripped.endswith("()") or "_" in stripped
    ):
        return True
    return False


def normalize_text(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip().lower())
    return text.strip(" .:-")


_NON_TRIVIAL_PATTERNS = (
    r"\bmulti[- ]?(?:part|file|stage|step|phase)\b",
    r"\bsubsystem\b",
    r"\barchitect(?:ure|ural)?\b",
    r"\brefactor(?:ing)?\b",
    r"\bfeature\b",
    r"\bvalidation\b.*\b(?:rung|pipeline|orchestrat|stage|system|flow)\b",
    r"\b(?:build|create|implement|add)\b.*\b(?:system|subsystem|architecture|feature|workflow|pipeline|rung)\b",
)

_IMPORT_STMT = re.compile(r"^(?:from\s+\S+\s+import\s+|import\s+\S+)")
_FIELD_DECL = re.compile(
    r"^[a-zA-Z_][a-zA-Z0-9_]*\s*:\s*"
    r"(?:str|int|bool|float|bytes|Any|None"
    r"|dict\s*(?:\[[^]]*\])?"
    r"|list\s*(?:\[[^]]*\])?"
    r"|tuple\s*(?:\[[^]]*\])?"
    r"|set\s*(?:\[[^]]*\])?"
    r"|frozenset\s*(?:\[[^]]*\])?"
    r"|Dict\s*(?:\[[^]]*\])?"
    r"|List\s*(?:\[[^]]*\])?"
    r"|Set\s*(?:\[[^]]*\])?"
    r"|Optional\s*(?:\[[^]]*\])?"
    r"|Callable\s*(?:\[[^]]*\])?"
    r")"
)
_FUTURE_IMPORT = re.compile(r"from\s+__future__\s+import", re.IGNORECASE)
_PAREN_FILLER = re.compile(r"^\([^)]*\)$")
_SHORT_SYMBOLIC = re.compile(r"^[^\w\s]{4,}$")
_OPERATOR_LEAD = re.compile(r"^[|\-=\[\]]+\s")
_BARE_CODE_SYMBOL = re.compile(r"^_?[A-Za-z][A-Za-z0-9_]*(?:\(\))?$")


def _matches_any(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def _str_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if str(item or "").strip()]


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value or "").strip()
        if not item or item in seen:
            continue
        result.append(item)
        seen.add(item)
    return result


__all__ = [
    "compact_checklist_label",
    "is_implementation_detail",
    "normalize_text",
    "request_is_non_trivial",
    "validate_checklist_items",
]
