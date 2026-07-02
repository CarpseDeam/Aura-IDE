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
        if isinstance(raw, cls):
            return raw
        if not isinstance(raw, dict):
            description = compact_todo_label(str(raw or ""), fallback="")
            return cls(id="", description=description)
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
        normalized = normalize_dispatch_todo_checklist(explicit, steps)
        if normalized:
            return normalized
    accepted_items = _accepted_work_contract_todo_items(
        str(getattr(req, "spec", "") or ""),
        steps,
    )
    if accepted_items:
        return accepted_items
    return _fallback_step_todo_items(
        steps,
        goal=str(getattr(req, "goal", "") or ""),
        summary=str(getattr(req, "summary", "") or ""),
        files=list(getattr(req, "files", []) or []),
    )


def normalize_dispatch_todo_checklist(
    checklist: list[Any],
    steps: list[Any],
) -> list[DispatchTodoItem]:
    """Normalize explicit checklist rows and fill missing owning step ids."""
    step_owner_by_item = _step_owner_by_item_id(steps)
    valid_step_ids = set(_step_ids(steps))
    single_step_owner = str(getattr(steps[0], "id", "") or "") if len(steps) == 1 else ""
    normalized: list[DispatchTodoItem] = []
    for index, raw_item in enumerate(checklist, start=1):
        item = raw_item if isinstance(raw_item, DispatchTodoItem) else DispatchTodoItem.from_dict(raw_item)
        item_id = str(item.id or f"todo-{index}").strip()
        description_fallback = item_id if str(item.description or "").strip() else ""
        description = compact_todo_label(item.description, fallback=description_fallback)
        if not description:
            continue
        if _is_implementation_detail(description):
            continue
        owner = item.owning_step_id if item.owning_step_id in valid_step_ids else ""
        owner = owner or step_owner_by_item.get(item_id, "") or single_step_owner
        normalized.append(
            DispatchTodoItem(
                id=item_id,
                description=description,
                status="pending",
                files=list(item.files),
                owning_step_id=owner,
            )
        )
    return _assign_missing_todo_owners(_dedupe_todo_items(normalized), steps)


def _accepted_work_contract_todo_items(
    spec: str,
    steps: list[Any],
) -> list[DispatchTodoItem]:
    """Extract visible checklist rows from an accepted-work bullet block."""
    lines = str(spec or "").splitlines()
    in_contract = False
    raw_items: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if in_contract and raw_items:
                break
            continue
        if re.match(r"^accepted\s+work\s+contract\s*:", stripped, re.IGNORECASE):
            in_contract = True
            continue
        if not in_contract:
            continue
        match = re.match(r"^(?:[-*+]|\d+[.)])\s+(.+)$", stripped)
        if not match:
            if raw_items:
                break
            continue
        description = compact_todo_label(match.group(1))
        if description and not _is_implementation_detail(description):
            raw_items.append(description)

    items = [
        DispatchTodoItem(id=f"todo-{index}", description=description)
        for index, description in enumerate(raw_items, start=1)
    ]
    return _assign_missing_todo_owners(_dedupe_todo_items(items), steps)


def todo_tasks_from_plan(
    plan: Any,
    *,
    active_step_id: str | None = None,
    completed_step_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Convert a dispatch plan into visible TODO snapshot rows.

    Planner-authored visible_checklist rows are the canonical rail when they
    survive normalization. Step rows are only the compatibility fallback.
    """
    completed: set[str] = completed_step_ids or set()
    tasks: list[dict[str, Any]] = []
    steps = list(getattr(plan, "steps", []) or [])
    visible_checklist = list(getattr(plan, "visible_checklist", []) or [])
    items = normalize_dispatch_todo_checklist(visible_checklist, steps) if visible_checklist else []
    if not items:
        items = _fallback_step_todo_items(
            steps,
            goal=str(getattr(plan, "overall_goal", "") or ""),
            summary=str(getattr(plan, "visible_summary", "") or ""),
            files=list(getattr(plan, "global_files", []) or []),
        )

    for item in items:
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

        if owner and owner in completed:
            task["status"] = "done"
        elif owner and owner == active_step_id:
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


# ── Implementation-detail filter ──────────────────────────────────────────
# Rows matching these patterns are code / import / declaration noise that
# should never appear in the user-facing TODO checklist.

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
_OPERATOR_LEAD = re.compile(r"^[|\-=\[\]]+\s")  # "| None = None", "= default"
_BARE_CODE_SYMBOL = re.compile(r"^_?[A-Za-z][A-Za-z0-9_]*(?:\(\))?$")


def _is_implementation_detail(text: str) -> bool:
    """Return True if *text* is code/import/declaration noise, not a work item."""
    stripped = str(text or "").strip()
    if not stripped:
        return True

    # Literal import statements: "from x import y" / "import x"
    if _IMPORT_STMT.match(stripped):
        return True
    # "from __future__ import annotations"
    if _FUTURE_IMPORT.search(stripped):
        return True

    # Docstring / code-fence fragments
    if '"""' in stripped:
        return True

    # Field declarations: "full_message: dict[str, Any] | None = None"
    m = _FIELD_DECL.match(stripped)
    if m:
        rest = stripped[m.end() :]
        if not rest or rest.lstrip().startswith(("|", "=")):
            return True

    # Parenthetical filler: "(no other fields)"
    if _PAREN_FILLER.match(stripped):
        return True

    # Short purely-symbolic fragments
    if _SHORT_SYMBOLIC.match(stripped):
        return True

    # Lines starting with an operator: "| None = None",  "= default"
    if _OPERATOR_LEAD.match(stripped):
        return True

    # Bare helper/function names are scratch/code fragments, not work items.
    if _BARE_CODE_SYMBOL.match(stripped) and (
        stripped.startswith("_") or stripped.endswith("()") or "_" in stripped
    ):
        return True

    return False


def _fallback_step_todo_items(
    steps: list[Any],
    *,
    goal: str = "",
    summary: str = "",
    files: list[str] | None = None,
) -> list[DispatchTodoItem]:
    """Build one compatibility TODO row per Worker execution step."""
    if steps:
        items: list[DispatchTodoItem] = []
        for index, step in enumerate(steps, start=1):
            step_id = str(getattr(step, "id", "") or f"step-{index}")
            description = compact_todo_label(
                str(getattr(step, "title", "") or "")
                or str(getattr(step, "goal", "") or "")
                or step_id,
                fallback=step_id or "Worker step",
            )
            items.append(
                DispatchTodoItem(
                    id=step_id,
                    description=description,
                    files=_step_files(step),
                    owning_step_id=step_id,
                )
            )
        return _dedupe_todo_items(items)

    owner = "step-1"
    return [
        DispatchTodoItem(
            id=owner,
            description=compact_todo_label(
                summary or goal or "Complete Worker dispatch",
                fallback="Complete Worker dispatch",
            ),
            files=list(files or []),
            owning_step_id=owner,
        )
    ]

def _step_owner_by_item_id(steps: list[Any]) -> dict[str, str]:
    owners: dict[str, str] = {}
    for step in steps:
        step_id = str(getattr(step, "id", "") or "")
        if not step_id:
            continue
        for item_id in _str_list(getattr(step, "checklist_item_ids", [])):
            item = str(item_id or "").strip()
            if item and item not in owners:
                owners[item] = step_id
    return owners


def _assign_missing_todo_owners(
    items: list[DispatchTodoItem],
    steps: list[Any],
) -> list[DispatchTodoItem]:
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

    total = len(items)
    assigned: list[DispatchTodoItem] = []
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


def _step_ids(steps: list[Any]) -> list[str]:
    return [
        step_id
        for step_id in (str(getattr(step, "id", "") or "").strip() for step in steps)
        if step_id
    ]


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
