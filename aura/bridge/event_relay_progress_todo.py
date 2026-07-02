"""Non-canonical progress/TODO overlay machinery extracted from WorkerEventRelay.

This module owns the progress-TODO state and overlay logic for free-form
(non-canonical) Worker calls.  During canonical DispatchSession campaigns
suppress_todo_updates is set and all emissions are dropped.
"""

from __future__ import annotations

from typing import Any, Callable

from aura.bridge.event_relay_write_tracking import (
    DEFAULT_WRITE_ACTION_WORDS,
    PATH_FIELDS,
    PATH_MENTION_RE,
    VALIDATION_PROGRESS_TOOLS,
    _append_path_values,
    _dedupe,
    _is_file_mutation_tool,
    _normalize_path,
    _progress_key_for_tool,
    _tool_progress_details_from_payload,
)
from aura.todo_state import todo_signature, todo_task_description, todo_task_status

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROGRESS_TODO_LABELS = {
    "inspect": "Inspect relevant files",
    "edit": "Apply changes",
    "validate": "Run validation",
    "recover": "Handle recovery",
    "finish": "Deliver final report",
}
PROGRESS_TODO_ORDER = ("inspect", "edit", "validate", "recover", "finish")
PHASE_ACTION_WORDS = {
    "inspect": (
        "inspect",
        "read",
        "review",
        "search",
        "find",
        "locate",
        "analyze",
        "investigate",
        "understand",
    ),
    "validate": (
        "validate",
        "validation",
        "test",
        "tests",
        "check",
        "compile",
        "pytest",
        "verify",
        "verification",
    ),
    "recover": (
        "recover",
        "recovery",
        "retry",
        "failure",
        "failed",
        "blocker",
        "blocked",
    ),
    "finish": (
        "finish",
        "final",
        "report",
        "summary",
        "summarize",
        "deliver",
    ),
}


# ---------------------------------------------------------------------------
# Module-level helper functions
# ---------------------------------------------------------------------------


def _phase_action_words(phase: str, details: dict[str, Any]) -> tuple[str, ...]:
    if phase == "edit":
        words = details.get("action_words")
        if isinstance(words, list) and words:
            return tuple(str(word).lower() for word in words if str(word).strip())
        return DEFAULT_WRITE_ACTION_WORDS
    return PHASE_ACTION_WORDS.get(phase, ())


def _todo_task_overlay_keys(task: Any) -> list[str]:
    keys: list[str] = []
    description = _normalize_todo_text(todo_task_description(task))
    if description:
        keys.append(f"desc:{description}")
    for path in _todo_task_paths(task):
        keys.append(f"path:{path}")
    return _dedupe(keys)


def _todo_task_paths(task: Any) -> list[str]:
    paths: list[str] = []
    if isinstance(task, dict):
        for field in PATH_FIELDS:
            _append_path_values(paths, task.get(field))
        _append_path_values(paths, task.get("paths"))
        _append_path_values(paths, task.get("files"))
    description = todo_task_description(task)
    for match in PATH_MENTION_RE.finditer(description):
        path = _normalize_path(match.group(0))
        if path:
            paths.append(path)
    return _dedupe(paths)


def _normalized_detail_paths(details: dict[str, Any]) -> list[str]:
    paths = details.get("paths", [])
    if not isinstance(paths, list):
        return []
    return _dedupe([_normalize_path(path) for path in paths if _normalize_path(path)])


def _normalize_todo_text(text: str) -> str:
    return " ".join(str(text).lower().split())


def _normalized_todo_text(task: Any) -> str:
    return _normalize_todo_text(todo_task_description(task))


def _path_basename(path: str) -> str:
    return path.rsplit("/", 1)[-1] if path else ""


def _paths_have_suffix_match(path: str, task_path: str) -> bool:
    if not path or not task_path:
        return False
    return path.endswith(f"/{task_path}") or task_path.endswith(f"/{path}")


# ---------------------------------------------------------------------------
# EventRelayProgressTodo
# ---------------------------------------------------------------------------


class EventRelayProgressTodo:
    """Non-canonical progress/TODO overlay state and logic.

    Owns the progress-todo status dictionaries, model-todo-tasks list, and
    all the overlay/completion methods.  Delegates signal emission through
    the *emit_todo* callback so the owning relay can forward to Qt.
    """

    def __init__(
        self,
        suppress_todo_updates: bool = False,
        emit_todo: Callable[[str, list[Any]], None] | None = None,
    ) -> None:
        self._suppress_todo_updates = suppress_todo_updates
        self._emit_todo = emit_todo

        self._model_todo_tasks: list[Any] = []
        self._progress_todo_status: dict[str, str] = {}
        self._runtime_todo_status: dict[str, str] = {}
        self._runtime_todo_phase: dict[str, str] = {}
        self._last_emitted_todo_signature: tuple[tuple[str, str], ...] = ()

    def reset(self) -> None:
        """Clear all tracking fields so the helper can be reused."""
        self._model_todo_tasks.clear()
        self._progress_todo_status.clear()
        self._runtime_todo_status.clear()
        self._runtime_todo_phase.clear()
        self._last_emitted_todo_signature = ()

    def set_model_todo_tasks(self, tasks: list[Any]) -> None:
        """Set the canonical model-provided TODO tasks."""
        self._model_todo_tasks = list(tasks)

    # ------------------------------------------------------------------
    # Progress-tool lifecycle
    # ------------------------------------------------------------------

    def mark_progress_tool_started(self, tool_call_id: str, name: str) -> None:
        key = _progress_key_for_tool(name)
        if not key:
            return
        if key == "edit":
            self.mark_progress_done("inspect")
        elif key == "validate":
            self.mark_progress_done("inspect")
            self.mark_progress_done("edit")
        self._set_progress_status(key, "active")
        self._activate_model_todo_phase(
            key, _tool_progress_details_from_payload(name, {})
        )
        self.emit_todo_progress(tool_call_id)

    def mark_progress_tool_active(
        self,
        tool_call_id: str,
        name: str,
        details: dict[str, Any],
    ) -> None:
        key = _progress_key_for_tool(name)
        if not key:
            return
        self._activate_model_todo_phase(key, details)
        self.emit_todo_progress(tool_call_id)

    def mark_progress_tool_result(
        self,
        tool_call_id: str,
        name: str,
        ok: bool,
        parsed: Any,
        details: dict[str, Any],
    ) -> None:
        if name == "update_todo_list":
            return
        if isinstance(parsed, dict) and parsed.get("internal_recovery_steer"):
            self.mark_recovery_progress_active(details)
            self.emit_todo_progress(tool_call_id)
            return

        key = _progress_key_for_tool(name)
        if not key:
            return

        if ok and isinstance(parsed, dict):
            if _is_file_mutation_tool(name) and parsed.get("applied") is False:
                self._clear_active_model_todo_phase(key)
                self.mark_recovery_progress_active(details)
            elif name in VALIDATION_PROGRESS_TOOLS and parsed.get("ok") is False:
                self._clear_active_model_todo_phase(key)
                self.mark_recovery_progress_active(details)
            else:
                self._set_progress_status(key, "done")
                self._complete_model_todo_phase(key, details)
        elif ok:
            self._set_progress_status(key, "done")
            self._complete_model_todo_phase(key, details)
        else:
            self._clear_active_model_todo_phase(key)
            self.mark_recovery_progress_active(details)
        self.emit_todo_progress(tool_call_id)

    def mark_progress_finished(self, tool_call_id: str) -> None:
        for key, status in list(self._progress_todo_status.items()):
            if status == "active":
                self._progress_todo_status[key] = "done"
        for key, status in list(self._runtime_todo_status.items()):
            if status == "active":
                self._runtime_todo_status[key] = "done"
        self._set_progress_status("finish", "done")
        self._complete_model_todo_phase("finish", {})
        self.emit_todo_progress(tool_call_id)

    def mark_progress_done(self, key: str) -> None:
        if key in self._progress_todo_status:
            self._progress_todo_status[key] = "done"
        self._complete_active_model_todo_phase(key)

    def mark_recovery_progress_active(self, details: dict[str, Any]) -> None:
        self._set_progress_status("recover", "active")
        self._activate_model_todo_phase("recover", details)

    # ------------------------------------------------------------------
    # Internal status helpers
    # ------------------------------------------------------------------

    def _set_progress_status(self, key: str, status: str) -> None:
        if key in PROGRESS_TODO_LABELS and status in {"pending", "active", "done"}:
            self._progress_todo_status[key] = status

    # ------------------------------------------------------------------
    # TODO progress emission
    # ------------------------------------------------------------------

    def emit_todo_progress(self, tool_call_id: str, *, force: bool = False) -> None:
        if self._suppress_todo_updates:
            return
        tasks = self._combined_todo_tasks()
        signature = todo_signature(tasks)
        if not force and signature == self._last_emitted_todo_signature:
            return
        self._last_emitted_todo_signature = signature
        if self._emit_todo:
            self._emit_todo(tool_call_id, tasks)

    def _combined_todo_tasks(self) -> list[Any]:
        tasks = list(self._model_todo_tasks)
        if tasks:
            return [self._task_with_runtime_status(task) for task in tasks]

        existing_descriptions = {
            str(task.get("description") or task.get("content") or task.get("text") or task.get("task") or "")
            for task in tasks
            if isinstance(task, dict)
        }
        for key in PROGRESS_TODO_ORDER:
            status = self._progress_todo_status.get(key)
            if not status:
                continue
            description = PROGRESS_TODO_LABELS[key]
            if description in existing_descriptions:
                continue
            tasks.append({"description": description, "status": status})
        return tasks

    def _task_with_runtime_status(self, task: Any) -> Any:
        status = self._runtime_status_for_task(task)
        if not status:
            return task
        if isinstance(task, dict):
            updated = dict(task)
            updated["status"] = status
            return updated
        return {"description": str(task), "status": status}

    def _runtime_status_for_task(self, task: Any) -> str:
        keys = _todo_task_overlay_keys(task)
        statuses = [self._runtime_todo_status.get(key) for key in keys]
        if "done" in statuses:
            return "done"
        if "active" in statuses and todo_task_status(task) != "done":
            return "active"
        return ""

    def _effective_status_for_task(self, task: Any) -> str:
        return self._runtime_status_for_task(task) or todo_task_status(task)

    # ------------------------------------------------------------------
    # Model-todo phase overlay
    # ------------------------------------------------------------------

    def _activate_model_todo_phase(
        self,
        phase: str,
        details: dict[str, Any],
    ) -> None:
        index = self._find_model_todo_index(phase, details)
        if index is None:
            return
        task = self._model_todo_tasks[index]
        if self._effective_status_for_task(task) == "done":
            return
        self._clear_active_model_todo_phase(phase)
        self._set_model_todo_overlay(task, phase, "active", details)

    def _complete_model_todo_phase(
        self,
        phase: str,
        details: dict[str, Any],
    ) -> None:
        index = self._find_model_todo_index(phase, details)
        if index is None:
            index = self._find_active_model_todo_index(phase)
        if index is None:
            return
        task = self._model_todo_tasks[index]
        self._clear_active_model_todo_phase(phase)
        self._set_model_todo_overlay(task, phase, "done", details)

    def _complete_active_model_todo_phase(self, phase: str) -> None:
        index = self._find_active_model_todo_index(phase)
        if index is None:
            return
        task = self._model_todo_tasks[index]
        self._clear_active_model_todo_phase(phase)
        self._set_model_todo_overlay(task, phase, "done", {})

    def _find_model_todo_index(
        self,
        phase: str,
        details: dict[str, Any],
    ) -> int | None:
        if not self._model_todo_tasks:
            return None
        if phase == "edit":
            path_index = self._find_path_matched_model_todo_index(details)
            if path_index is not None:
                return path_index
            active_index = self._find_active_model_todo_index(phase)
            if active_index is not None:
                return active_index
            ordered_index = self._find_next_ordered_edit_todo_index()
            if ordered_index is not None:
                return ordered_index
        return self._find_action_matched_model_todo_index(phase, details)

    def _find_path_matched_model_todo_index(
        self,
        details: dict[str, Any],
    ) -> int | None:
        paths = _normalized_detail_paths(details)
        if not paths:
            return None

        exact_matches: list[int] = []
        suffix_matches: list[int] = []
        basename_matches: list[int] = []
        for index, task in enumerate(self._model_todo_tasks):
            task_paths = _todo_task_paths(task)
            description_path_text = _normalize_path(todo_task_description(task))
            for path in paths:
                basename = _path_basename(path)
                if path in task_paths:
                    exact_matches.append(index)
                    continue
                if any(_paths_have_suffix_match(path, task_path) for task_path in task_paths):
                    suffix_matches.append(index)
                    continue
                if path and path in description_path_text:
                    suffix_matches.append(index)
                    continue
                if basename and any(
                    _path_basename(task_path) == basename for task_path in task_paths
                ):
                    basename_matches.append(index)

        for matches in (exact_matches, suffix_matches):
            if matches:
                return matches[0]
        unique_basename_matches = list(dict.fromkeys(basename_matches))
        if len(unique_basename_matches) == 1:
            return unique_basename_matches[0]
        return None

    def _find_action_matched_model_todo_index(
        self,
        phase: str,
        details: dict[str, Any],
    ) -> int | None:
        words = _phase_action_words(phase, details)
        if not words:
            return None
        active_match: int | None = None
        pending_match: int | None = None
        for index, task in enumerate(self._model_todo_tasks):
            task_status = self._effective_status_for_task(task)
            if task_status == "done":
                continue
            text = _normalized_todo_text(task)
            if not any(word in text for word in words):
                continue
            if task_status == "active":
                active_match = index
                break
            if pending_match is None:
                pending_match = index
        return active_match if active_match is not None else pending_match

    def _find_next_ordered_edit_todo_index(self) -> int | None:
        fallback: int | None = None
        for index, task in enumerate(self._model_todo_tasks):
            if self._effective_status_for_task(task) == "done":
                continue
            text = _normalized_todo_text(task)
            if any(word in text for word in PHASE_ACTION_WORDS["validate"]):
                continue
            if any(word in text for word in PHASE_ACTION_WORDS["finish"]):
                continue
            if any(word in text for word in PHASE_ACTION_WORDS["recover"]):
                continue
            if any(word in text for word in PHASE_ACTION_WORDS["inspect"]):
                if fallback is None:
                    fallback = index
                continue
            return index
        return fallback

    def _find_active_model_todo_index(self, phase: str) -> int | None:
        for index, task in enumerate(self._model_todo_tasks):
            keys = _todo_task_overlay_keys(task)
            if any(
                self._runtime_todo_status.get(key) == "active"
                and self._runtime_todo_phase.get(key) == phase
                for key in keys
            ):
                return index
        return None

    def _set_model_todo_overlay(
        self,
        task: Any,
        phase: str,
        status: str,
        details: dict[str, Any],
    ) -> None:
        keys = set(_todo_task_overlay_keys(task))
        for path in _normalized_detail_paths(details):
            keys.add(f"path:{path}")
        for key in keys:
            if not key:
                continue
            if self._runtime_todo_status.get(key) == "done" and status != "done":
                continue
            self._runtime_todo_status[key] = status
            self._runtime_todo_phase[key] = phase

    def _clear_active_model_todo_phase(self, phase: str) -> None:
        for key, key_phase in list(self._runtime_todo_phase.items()):
            if (
                key_phase == phase
                and self._runtime_todo_status.get(key) == "active"
            ):
                self._runtime_todo_status.pop(key, None)
                self._runtime_todo_phase.pop(key, None)
