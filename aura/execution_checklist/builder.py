"""Pure builder for visible execution checklists."""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

from aura.execution_checklist.models import (
    ExecutionChecklistItem,
    ExecutionChecklistSnapshot,
)
from aura.execution_checklist.validation import (
    compact_checklist_label,
    is_implementation_detail,
    request_is_non_trivial,
    validate_checklist_items,
)


def build_execution_checklist(request: Any) -> ExecutionChecklistSnapshot:
    """Build the visible execution checklist for a dispatch request or plan.

    This function is intentionally pure: it only reads request/plan-shaped
    objects and returns a new snapshot. It does not mutate dispatch/session
    state, import Qt, or call Worker execution paths.
    """
    return ExecutionChecklistSnapshot(
        items=tuple(build_execution_checklist_items(request))
    )


def build_execution_checklist_items(request: Any) -> list[ExecutionChecklistItem]:
    """Return normalized visible checklist rows in display order."""
    steps = _request_steps(request)

    explicit = raw_execution_checklist(request)
    if explicit:
        items = normalize_execution_checklist(explicit, steps)
        if items:
            return items

    accepted = _accepted_work_contract_items(str(getattr(request, "spec", "") or ""), steps)
    if accepted:
        return accepted

    return _fallback_items(request, steps)


def normalize_execution_checklist(
    checklist: list[Any],
    steps: list[Any],
) -> list[ExecutionChecklistItem]:
    """Normalize explicit Planner-authored checklist rows."""
    valid_step_ids = set(_step_ids(steps))
    step_owner_by_item = _step_owner_by_item_id(steps)
    single_step_owner = _step_id(steps[0]) if len(steps) == 1 else ""

    raw_items: list[ExecutionChecklistItem] = []
    for index, raw_item in enumerate(checklist, start=1):
        item = ExecutionChecklistItem.from_raw(raw_item)
        item_id = item.id or f"item-{index}"
        owner = item.owning_step_id if item.owning_step_id in valid_step_ids else ""
        owner = owner or step_owner_by_item.get(item_id, "") or single_step_owner
        raw_items.append(
            replace(
                item,
                id=item_id,
                status="pending",
                owning_step_id=owner,
            )
        )

    return _assign_missing_owners(validate_checklist_items(raw_items), steps)


def raw_execution_checklist(request: Any) -> list[Any]:
    """Return the first recognized explicit checklist field from *request*."""
    for key in (
        "execution_checklist",
        "todo_checklist",
        "visible_checklist",
        "checklist",
    ):
        value = _get_value(request, key)
        if isinstance(value, list):
            return list(value)
    return []


def _accepted_work_contract_items(
    spec: str,
    steps: list[Any],
) -> list[ExecutionChecklistItem]:
    lines = str(spec or "").splitlines()
    in_contract = False
    descriptions: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            if in_contract and descriptions:
                break
            continue
        if re.match(r"^accepted\s+work\s+contract\s*:", stripped, re.IGNORECASE):
            in_contract = True
            continue
        if not in_contract:
            continue
        match = re.match(r"^(?:[-*+]|\d+[.)])\s+(.+)$", stripped)
        if not match:
            if descriptions:
                break
            continue
        description = compact_checklist_label(match.group(1), fallback="")
        if description and not is_implementation_detail(description):
            descriptions.append(description)

    raw_items = [
        ExecutionChecklistItem(id=f"item-{index}", description=description)
        for index, description in enumerate(descriptions, start=1)
    ]
    return _assign_missing_owners(validate_checklist_items(raw_items), steps)


def _fallback_items(
    request: Any,
    steps: list[Any],
) -> list[ExecutionChecklistItem]:
    if steps:
        raw_items: list[ExecutionChecklistItem] = []
        for index, step in enumerate(steps, start=1):
            step_id = _step_id(step) or f"step-{index}"
            description = compact_checklist_label(
                str(getattr(step, "title", "") or "")
                or str(getattr(step, "goal", "") or "")
                or step_id,
                fallback=step_id,
            )
            raw_items.append(
                ExecutionChecklistItem(
                    id=step_id,
                    description=description,
                    files=tuple(_step_files(step)),
                    owning_step_id=step_id,
                )
            )
        return validate_checklist_items(raw_items)

    if request_is_non_trivial(request):
        return []

    owner = "step-1"
    description = compact_checklist_label(
        str(getattr(request, "summary", "") or "")
        or str(getattr(request, "goal", "") or "")
        or "Complete execution step",
        fallback="Complete execution step",
    )
    return validate_checklist_items([
        ExecutionChecklistItem(
            id=owner,
            description=description,
            files=tuple(_str_list(getattr(request, "files", []))),
            owning_step_id=owner,
        )
    ])


def _assign_missing_owners(
    items: list[ExecutionChecklistItem],
    steps: list[Any],
) -> list[ExecutionChecklistItem]:
    if not items:
        return []
    step_ids = _step_ids(steps)
    if not step_ids:
        return items

    valid_step_ids = set(step_ids)
    if len(step_ids) == 1:
        owner = step_ids[0]
        return [
            replace(
                item,
                owning_step_id=item.owning_step_id
                if item.owning_step_id in valid_step_ids
                else owner,
            )
            for item in items
        ]

    assigned: list[ExecutionChecklistItem] = []
    total = len(items)
    for index, item in enumerate(items):
        owner = item.owning_step_id if item.owning_step_id in valid_step_ids else ""
        if not owner:
            owner = _distributed_step_id(index, total, step_ids)
        assigned.append(replace(item, owning_step_id=owner))
    return assigned


def _distributed_step_id(index: int, total_items: int, step_ids: list[str]) -> str:
    if not step_ids:
        return ""
    if total_items <= 0:
        return step_ids[0]
    step_index = min((index * len(step_ids)) // total_items, len(step_ids) - 1)
    return step_ids[step_index]


def _step_owner_by_item_id(steps: list[Any]) -> dict[str, str]:
    owners: dict[str, str] = {}
    for step in steps:
        step_id = _step_id(step)
        if not step_id:
            continue
        for item_id in _str_list(getattr(step, "checklist_item_ids", [])):
            if item_id and item_id not in owners:
                owners[item_id] = step_id
    return owners


def _request_steps(request: Any) -> list[Any]:
    steps = _get_value(request, "steps")
    if isinstance(steps, list):
        return list(steps)
    return []


def _step_ids(steps: list[Any]) -> list[str]:
    return [step_id for step_id in (_step_id(step) for step in steps) if step_id]


def _step_id(step: Any) -> str:
    return str(getattr(step, "id", "") or _get_value(step, "id") or "").strip()


def _step_files(step: Any) -> list[str]:
    files = _str_list(getattr(step, "files", []) or _get_value(step, "files"))
    if files:
        return _dedupe(files)
    return _files_from_target_regions(
        getattr(step, "target_regions", []) or _get_value(step, "target_regions")
    )


def _files_from_target_regions(target_regions: Any) -> list[str]:
    if not isinstance(target_regions, list):
        return []
    paths: list[str] = []
    for region in target_regions:
        if not isinstance(region, dict):
            continue
        path = str(region.get("path") or "").strip()
        if path:
            paths.append(path)
    return _dedupe(paths)


def _get_value(source: Any, key: str) -> Any:
    if isinstance(source, dict):
        return source.get(key)
    return getattr(source, key, None)


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
    "build_execution_checklist",
    "build_execution_checklist_items",
    "normalize_execution_checklist",
    "raw_execution_checklist",
]
