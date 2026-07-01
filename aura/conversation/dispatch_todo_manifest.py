"""Visible dispatch TODO manifest construction.

This module owns the user-facing checklist contract for Planner -> Worker
dispatches. Execution steps remain in dispatch_plan.py; this file only builds,
normalizes, serializes, and snapshots visible checklist rows.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any


@dataclass(frozen=True)
class DispatchTodoItem:
    """One user-visible row in the dispatch TODO rail."""

    id: str
    description: str
    status: str = "pending"
    files: list[str] = field(default_factory=list)
    owning_step_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "description": self.description,
            "status": self.status,
        }
        if self.files:
            payload["files"] = list(self.files)
        if self.owning_step_id:
            payload["owning_step_id"] = self.owning_step_id
            payload["step_id"] = self.owning_step_id
        return payload

    @classmethod
    def from_dict(cls, raw: Any) -> "DispatchTodoItem":
        if not isinstance(raw, dict):
            raw = {}
        item_id = str(raw.get("id") or raw.get("checklist_item_id") or "")
        description = str(
            raw.get("description")
            or raw.get("content")
            or raw.get("text")
            or raw.get("task")
            or item_id
        )
        status = str(raw.get("status") or "pending").lower().strip()
        if status not in {"pending", "active", "done"}:
            status = "pending"
        owning_step_id = str(raw.get("owning_step_id") or raw.get("step_id") or "")
        return cls(
            id=item_id,
            description=compact_todo_label(description, fallback=item_id or "Worker task"),
            status=status,
            files=_str_list(raw.get("files")),
            owning_step_id=owning_step_id,
        )


def raw_dispatch_todo_checklist(raw: dict[str, Any]) -> list[Any]:
    """Return the first recognized raw checklist field from a payload."""
    for key in ("todo_checklist", "visible_checklist", "checklist"):
        value = raw.get(key)
        if isinstance(value, list):
            return value
    return []


def ensure_dispatch_todo_checklist(req: Any) -> Any:
    """Return a request carrying a durable user-visible TODO checklist."""
    if getattr(req, "todo_checklist", None):
        return replace(
            req,
            todo_checklist=normalize_dispatch_todo_checklist(
                req.todo_checklist,
                getattr(req, "steps", []),
            ),
        )
    return replace(req, todo_checklist=dispatch_todo_manifest_from_request(req))


def dispatch_todo_manifest_from_request(req: Any) -> list[DispatchTodoItem]:
    """Build the visible checklist from an accepted dispatch request."""
    explicit = getattr(req, "todo_checklist", None)
    steps = list(getattr(req, "steps", []) or [])
    if explicit:
        return normalize_dispatch_todo_checklist(explicit, steps)

    items: list[DispatchTodoItem] = []
    if steps:
        for step in steps:
            items.extend(_items_from_step(step))
        items.extend(
            _items_from_contract_text(
                item_prefix="contract",
                owner_steps=steps,
                files=list(getattr(req, "files", []) or []),
                texts=[
                    str(getattr(req, "spec", "") or ""),
                    str(getattr(req, "acceptance", "") or ""),
                    *_str_list(getattr(req, "required_outputs", [])),
                ],
                validation_commands=_str_list(getattr(req, "validation_commands", [])),
            )
        )
    else:
        owner = "step-1"
        item_texts = _checklist_texts_from_sources(
            [
                str(getattr(req, "goal", "") or ""),
                str(getattr(req, "spec", "") or ""),
                str(getattr(req, "acceptance", "") or ""),
                *_str_list(getattr(req, "required_outputs", [])),
            ],
            validation_commands=_str_list(getattr(req, "validation_commands", [])),
        )
        if not item_texts:
            item_texts = [
                str(getattr(req, "summary", "") or "")
                or str(getattr(req, "goal", "") or "")
                or "Complete Worker dispatch"
            ]
        for index, text in enumerate(item_texts, start=1):
            items.append(
                DispatchTodoItem(
                    id=f"{owner}-todo-{index}",
                    description=text,
                    files=list(getattr(req, "files", []) or []),
                    owning_step_id=owner,
                )
            )

    return _dedupe_todo_items(items)


def normalize_dispatch_todo_checklist(
    checklist: list[Any],
    steps: list[Any],
) -> list[DispatchTodoItem]:
    """Normalize explicit checklist rows and fill missing owning step ids."""
    step_owner_by_item = _step_owner_by_item_id(steps)
    single_step_owner = str(getattr(steps[0], "id", "") or "") if len(steps) == 1 else ""
    normalized: list[DispatchTodoItem] = []
    for index, raw_item in enumerate(checklist, start=1):
        item = raw_item if isinstance(raw_item, DispatchTodoItem) else DispatchTodoItem.from_dict(raw_item)
        item_id = item.id or f"todo-{index}"
        description = compact_todo_label(item.description, fallback=item_id)
        if not description:
            continue
        owner = item.owning_step_id or step_owner_by_item.get(item_id, "") or single_step_owner
        normalized.append(
            DispatchTodoItem(
                id=item_id,
                description=description,
                status="pending",
                files=list(item.files),
                owning_step_id=owner,
            )
        )
    return _dedupe_todo_items(normalized)


def todo_tasks_from_plan(
    plan: Any,
    *,
    active_step_id: str | None = None,
    completed_step_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Convert a dispatch plan's visible checklist into TODO snapshot rows."""
    completed: set[str] = completed_step_ids or set()
    checklist = list(getattr(plan, "visible_checklist", []) or [])
    if not checklist:
        checklist = [
            DispatchTodoItem(
                id=str(getattr(step, "id", "") or ""),
                description=(
                    str(getattr(step, "title", "") or "")
                    or str(getattr(step, "goal", "") or "")
                    or str(getattr(step, "id", "") or "")
                ),
                files=list(getattr(step, "files", []) or []),
                owning_step_id=str(getattr(step, "id", "") or ""),
            )
            for step in list(getattr(plan, "steps", []) or [])
        ]

    tasks: list[dict[str, Any]] = []
    for item in checklist:
        owner = item.owning_step_id or item.id
        task: dict[str, Any] = {
            "id": item.id,
            "step_id": owner,
            "owning_step_id": owner,
            "description": item.description,
            "status": "pending",
        }
        if item.files:
            task["files"] = list(item.files)

        if item.id in completed or owner in completed:
            task["status"] = "done"
        elif item.id == active_step_id or owner == active_step_id:
            task["status"] = "active"

        tasks.append(task)
    return tasks


def compact_todo_label(value: str, fallback: str = "Worker step") -> str:
    """Return a compact, display-safe label for one TODO rail row."""
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
            rf"^{re.escape(prefix)}\s*\d*\s*[:\.\-]\s*",
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


def _items_from_step(step: Any) -> list[DispatchTodoItem]:
    validation_policy = getattr(step, "validation_policy", None)
    policy_commands = getattr(validation_policy, "commands", []) if validation_policy is not None else []
    item_texts = _checklist_texts_from_sources(
        [
            str(getattr(step, "title", "") or ""),
            str(getattr(step, "spec", "") or ""),
            str(getattr(step, "acceptance", "") or ""),
            *_str_list(getattr(step, "required_outputs", [])),
        ],
        validation_commands=_str_list(getattr(step, "validation_commands", []) or policy_commands),
    )
    if not item_texts:
        item_texts = [
            str(getattr(step, "title", "") or "")
            or str(getattr(step, "goal", "") or "")
            or str(getattr(step, "id", "") or "")
            or "Worker step"
        ]

    checklist_item_ids = _str_list(getattr(step, "checklist_item_ids", []))
    step_id = str(getattr(step, "id", "") or "")
    items: list[DispatchTodoItem] = []
    for index, text in enumerate(item_texts, start=1):
        explicit_id = checklist_item_ids[index - 1] if index <= len(checklist_item_ids) else ""
        item_id = explicit_id or _todo_id(step_id, index)
        items.append(
            DispatchTodoItem(
                id=item_id,
                description=text,
                files=_step_files(step),
                owning_step_id=step_id,
            )
        )
    return items


def _items_from_contract_text(
    *,
    item_prefix: str,
    owner_steps: list[Any],
    files: list[str],
    texts: list[str],
    validation_commands: list[str],
) -> list[DispatchTodoItem]:
    descriptions = _checklist_texts_from_sources(texts, validation_commands=validation_commands)
    items: list[DispatchTodoItem] = []
    for index, text in enumerate(descriptions, start=1):
        owner = _best_owner_step_id(text, owner_steps)
        items.append(
            DispatchTodoItem(
                id=f"{item_prefix}-todo-{index}",
                description=text,
                files=list(files),
                owning_step_id=owner,
            )
        )
    return items


def _checklist_texts_from_sources(
    texts: list[str],
    *,
    validation_commands: list[str],
) -> list[str]:
    candidates: list[str] = []
    for text in texts:
        candidates.extend(_extract_checklist_lines(text))
    for command in validation_commands:
        command = str(command or "").strip()
        if command:
            candidates.append(f"Run {command}")
    return _dedupe([compact_todo_label(item) for item in candidates if item.strip()])


def _extract_checklist_lines(text: str) -> list[str]:
    lines = str(text or "").splitlines()
    items: list[str] = []
    in_list_section = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            in_list_section = False
            continue
        bullet_match = re.match(
            r"^(?:[-*+]|\d+[\.)]|\[[ xX]\])\s+(?P<item>.+)$",
            stripped,
        )
        if bullet_match:
            in_list_section = True
            items.extend(_split_compound_checklist_item(bullet_match.group("item")))
            continue
        if stripped.endswith(":") and _line_starts_checklist_section(stripped):
            in_list_section = True
            continue
        if in_list_section and _looks_like_checklist_item(stripped):
            items.extend(_split_compound_checklist_item(stripped))

    if items:
        return items

    stripped_text = str(text or "").strip()
    if _looks_like_checklist_item(stripped_text):
        return _split_compound_checklist_item(stripped_text)
    return []


def _split_compound_checklist_item(text: str) -> list[str]:
    cleaned = compact_todo_label(text)
    if not cleaned:
        return []
    parts = [
        part.strip(" .")
        for part in re.split(r"\s*;\s*", cleaned)
        if part.strip(" .")
    ]
    if len(parts) > 1:
        return parts
    return [cleaned]


def _line_starts_checklist_section(line: str) -> bool:
    text = line.lower()
    return bool(
        re.search(r"\b(?:acceptance|checklist|requirements?|tasks?|subtasks?|definition of done)\b", text)
    )


def _looks_like_checklist_item(text: str) -> bool:
    lowered = str(text or "").lower().strip()
    if not lowered:
        return False
    if len(lowered) > 180:
        return False
    return bool(
        re.match(
            r"^(?:create|add|move|wire|remove|run|validate|preserve|update|extract|"
            r"rename|ensure|include|implement|fix|keep|compile|selfcheck)\b",
            lowered,
        )
    )


def _best_owner_step_id(description: str, steps: list[Any]) -> str:
    if not steps:
        return ""
    description_words = _signal_words(description)
    if not description_words:
        return str(getattr(steps[-1], "id", "") or "")
    best_step = steps[-1]
    best_score = -1
    for step in steps:
        step_text = " ".join(
            [
                str(getattr(step, "title", "") or ""),
                str(getattr(step, "goal", "") or ""),
                str(getattr(step, "spec", "") or ""),
                str(getattr(step, "acceptance", "") or ""),
            ]
        )
        score = len(description_words & _signal_words(step_text))
        if score > best_score:
            best_step = step
            best_score = score
    lowered = description.lower()
    if best_score <= 0 and (
        "validate" in lowered
        or "validation" in lowered
        or "compile" in lowered
        or "selfcheck" in lowered
        or lowered.startswith("run ")
    ):
        return str(getattr(steps[-1], "id", "") or "")
    return str(getattr(best_step, "id", "") or "")


def _signal_words(text: str) -> set[str]:
    stop_words = {
        "and",
        "the",
        "from",
        "into",
        "with",
        "that",
        "this",
        "must",
        "should",
        "worker",
        "dispatch",
        "step",
        "task",
        "todo",
        "list",
        "rail",
    }
    return {
        word
        for word in re.findall(r"[a-zA-Z][a-zA-Z0-9_]{2,}", str(text or "").lower())
        if word not in stop_words
    }


def _todo_id(step_id: str, index: int) -> str:
    safe_step_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", step_id or "step").strip("-") or "step"
    return f"{safe_step_id}-todo-{index}"


def _step_owner_by_item_id(steps: list[Any]) -> dict[str, str]:
    owners: dict[str, str] = {}
    for step in steps:
        step_id = str(getattr(step, "id", "") or "")
        for item_id in _str_list(getattr(step, "checklist_item_ids", [])):
            item = str(item_id or "").strip()
            if item and item not in owners:
                owners[item] = step_id
    return owners


def _dedupe_todo_items(items: list[DispatchTodoItem]) -> list[DispatchTodoItem]:
    result: list[DispatchTodoItem] = []
    seen_descriptions: set[str] = set()
    seen_ids: set[str] = set()
    for index, item in enumerate(items, start=1):
        description = compact_todo_label(item.description, fallback=item.id or f"todo-{index}")
        key = _normalize_todo_text(description)
        if not description or key in seen_descriptions:
            continue
        item_id = item.id or f"todo-{index}"
        if item_id in seen_ids:
            item_id = f"{item_id}-{index}"
        result.append(
            DispatchTodoItem(
                id=item_id,
                description=description,
                status="pending",
                files=list(item.files),
                owning_step_id=item.owning_step_id,
            )
        )
        seen_descriptions.add(key)
        seen_ids.add(item_id)
    return result


def _normalize_todo_text(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip().lower())
    return text.strip(" .:-")


def _step_files(step: Any) -> list[str]:
    files = _dedupe(_str_list(getattr(step, "files", [])))
    if files:
        return files
    return _files_from_target_regions(list(getattr(step, "target_regions", []) or []))


def _files_from_target_regions(target_regions: list[Any]) -> list[str]:
    paths: list[str] = []
    for region in target_regions:
        if not isinstance(region, dict):
            continue
        path = str(region.get("path") or "").strip()
        if path:
            paths.append(path)
    return _dedupe(paths)


def _str_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw]


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
    "DispatchTodoItem",
    "compact_todo_label",
    "dispatch_todo_manifest_from_request",
    "ensure_dispatch_todo_checklist",
    "normalize_dispatch_todo_checklist",
    "raw_dispatch_todo_checklist",
    "todo_tasks_from_plan",
]
