"""Private per-user, per-workspace workflow state.

A workflow file says what a workflow *is*. This file says what *this* user,
in *this* workspace, has decided about it: whether Aura may eventually call
the whole workflow, and which one they were last looking at. Neither is ever
written into the project, so a workflow committed to a repository arrives on
every other machine switched off until that machine's user says otherwise —
the same rule that keeps an agent definition from granting itself authority
(:mod:`aura.agents.local_state`).

State lives under the user's own Aura data directory, keyed by a digest of
the workspace path::

    <data_dir>/agents/workspaces/<digest>.workflows.json

It is a separate file from the agent roster on purpose: the two answer
different questions, are written by different surfaces, and a corrupt one
must never take the other down with it.

Availability is recorded now and nothing reads it as permission yet. Aura
does not call workflows in this phase; the switch exists so the decision is
the user's from the first moment they can express it.
"""
from __future__ import annotations

import json
import logging
import stat
from pathlib import Path

from aura.agents.graph_models import is_valid_graph_id
from aura.agents.local_state import workspace_key
from aura.conversation.tools.fs_write import atomic_write_bytes
from aura.paths import data_dir

logger = logging.getLogger(__name__)

_STATE_VERSION = 1

#: What the workflow-level switch is called wherever it is shown.
AVAILABILITY_LABEL = "Available to Aura"

#: What that switch means, said once, here, rather than in a widget.
AVAILABILITY_NOTE = (
    "Your choice, on this computer, for this project — it is never written "
    "into a workflow, so a workflow you share cannot switch itself on for "
    "anyone else."
)


class WorkflowLocalStateError(RuntimeError):
    """A private workflow decision could not be persisted."""


class WorkflowLocalState:
    """Reads and writes one workspace's private workflow decisions.

    ``state_root`` overrides the base directory purely for test isolation,
    exactly as :class:`aura.agents.local_state.AgentLocalState` does;
    production always uses ``data_dir()/agents``.
    """

    def __init__(
        self, workspace_root: Path | str, *, state_root: Path | None = None
    ) -> None:
        self._workspace_root = Path(workspace_root)
        base = Path(state_root) if state_root is not None else (data_dir() / "agents")
        self._path = (
            base / "workspaces" / f"{workspace_key(self._workspace_root)}.workflows.json"
        )

    @property
    def path(self) -> Path:
        """Where this workspace's private state is kept — never in the project."""
        return self._path

    # ---- available to Aura -------------------------------------------------

    def available_ids(self) -> tuple[str, ...]:
        """The ordered workflow ids this user has switched on."""
        return tuple(self._load()["available"])

    def is_available(self, graph_id: str) -> bool:
        self._require_graph_id(graph_id)
        return graph_id in self._load()["available"]

    def set_available(self, graph_id: str, available: bool) -> None:
        """Switch one workflow on at the end of the list, or off.

        Appending rather than inserting keeps the order the user built, the
        same way the agent roster does.
        """
        self._require_graph_id(graph_id)
        data = self._load(for_write=True)
        available_ids: list[str] = data["available"]
        if available and graph_id not in available_ids:
            available_ids.append(graph_id)
        elif not available and graph_id in available_ids:
            available_ids.remove(graph_id)
        else:
            return
        self._save(data)

    # ---- which one they were looking at ------------------------------------

    def selected_id(self) -> str:
        """The workflow this user last had open here, or ``""``."""
        return str(self._load()["selected"])

    def set_selected(self, graph_id: str) -> None:
        """Remember the open workflow. An empty id means none."""
        text = str(graph_id or "")
        if text:
            self._require_graph_id(text)
        data = self._load(for_write=True)
        if data["selected"] == text:
            return
        data["selected"] = text
        self._save(data)

    # ---- upkeep ------------------------------------------------------------

    def forget(self, graph_id: str) -> None:
        """Drop every local decision about *graph_id* — used when it is deleted."""
        self._require_graph_id(graph_id)
        data = self._load(for_write=True)
        changed = False
        if graph_id in data["available"]:
            data["available"].remove(graph_id)
            changed = True
        if data["selected"] == graph_id:
            data["selected"] = ""
            changed = True
        if changed:
            self._save(data)

    # ---- persistence -------------------------------------------------------

    def _load(self, *, for_write: bool = False) -> dict:
        blank: dict = {"available": [], "selected": ""}
        try:
            mode = self._path.stat().st_mode
        except FileNotFoundError:
            return blank
        except OSError as exc:
            logger.debug(
                "agents: could not inspect workflow state %s", self._path, exc_info=True
            )
            if for_write:
                raise self._corrupt() from exc
            return blank
        try:
            if not stat.S_ISREG(mode):
                raise OSError("the workflow-state path is not a regular file")
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("the workflow-state document is not an object")
            available = raw.get("available", [])
            selected = raw.get("selected", "")
            if not isinstance(available, list) or not isinstance(selected, str):
                raise ValueError("the workflow state has an invalid shape")
        except Exception as exc:
            logger.debug(
                "agents: could not read workflow state %s", self._path, exc_info=True
            )
            if for_write:
                raise self._corrupt() from exc
            return blank

        clean_available = [
            item for item in available if isinstance(item, str) and is_valid_graph_id(item)
        ]
        if for_write and len(clean_available) != len(available):
            raise self._corrupt()
        blank["available"] = clean_available
        blank["selected"] = selected if is_valid_graph_id(selected) else ""
        return blank

    def _save(self, data: dict) -> None:
        payload = {
            "version": _STATE_VERSION,
            "workspace": str(self._workspace_root),
            "available": list(data["available"]),
            "selected": str(data["selected"]),
        }
        try:
            atomic_write_bytes(
                self._path,
                json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8"),
            )
        except Exception as exc:
            raise WorkflowLocalStateError(
                f"Could not save your workflow choices: {exc}"
            ) from exc

    @staticmethod
    def _corrupt() -> WorkflowLocalStateError:
        return WorkflowLocalStateError(
            "Could not update your workflow choices because the existing "
            "local-state file is unreadable or corrupt."
        )

    @staticmethod
    def _require_graph_id(graph_id: object) -> str:
        if not is_valid_graph_id(graph_id):
            raise WorkflowLocalStateError(
                f"'{graph_id}' is not a valid immutable workflow id."
            )
        return str(graph_id)


__all__ = [
    "AVAILABILITY_LABEL",
    "AVAILABILITY_NOTE",
    "WorkflowLocalState",
    "WorkflowLocalStateError",
]
