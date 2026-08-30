"""Private per-user, per-workspace workflow state.

A workflow file says what a workflow *is*. This file says what *this* user,
in *this* workspace, has decided about it: which one they have open, and
whether Aura may invoke it during an ordinary conversation. Neither is ever
written into the project, so a workflow committed to a repository arrives on
every other machine switched off until that machine's user says otherwise —
the same rule that keeps an agent definition from granting itself authority
(:mod:`aura.agents.local_state`).

There is exactly one enabled bit, and it belongs to the selected workflow.
That is deliberate: an enabled flag per workflow *and* a master switch in the
toolbar would be two answers to one question, and the day they disagreed the
user would have no way to tell which one Aura believed. So the toolbar switch
is a view of this bit, selecting another workflow moves the question rather
than the answer, and there is nowhere else availability is recorded.

State lives under the user's own Aura data directory, keyed by a digest of
the workspace path::

    <data_dir>/agents/workspaces/<digest>.workflows.json

It is a separate file from the agent roster on purpose: the two answer
different questions, are written by different surfaces, and a corrupt one
must never take the other down with it.
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

#: Bumped when the per-workflow availability list became one enabled bit.
_STATE_VERSION = 2

#: What the master switch is called wherever it is shown.
ENABLED_LABEL = "Agents"

#: What that switch means, said once, here, rather than in a widget.
ENABLED_NOTE = (
    "Agents: when ON, Aura may run the selected workflow during an ordinary "
    "conversation. Your choice, on this computer, for this project — it is "
    "never written into a workflow, so a workflow you share cannot switch "
    "itself on for anyone else. Run in the Agents window works either way."
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

    # ---- which one they were looking at ------------------------------------

    def selected_id(self) -> str:
        """The workflow this user last had open here, or an empty id."""
        return str(self._load()["selected"])

    def set_selected(self, graph_id: str) -> None:
        """Remember the open workflow. An empty id means none.

        Selecting a different workflow switches the master gate off. The bit
        was the user's decision about the workflow they were looking at, and
        carrying it silently onto the next one would grant an authority they
        never gave.
        """
        text = str(graph_id or "")
        if text:
            self._require_graph_id(text)
        data = self._load(for_write=True)
        if data["selected"] == text:
            return
        data["selected"] = text
        data["enabled"] = False
        self._save(data)

    # ---- the master gate ---------------------------------------------------

    def is_enabled(self) -> bool:
        """Whether Aura may invoke the selected workflow during a conversation."""
        data = self._load()
        return bool(data["enabled"]) and bool(data["selected"])

    def set_enabled(self, enabled: bool) -> None:
        """Switch the selected workflow on or off for Aura, privately.

        With nothing selected there is nothing to enable, so this records
        ``False`` rather than an authority pointing at no workflow.
        """
        data = self._load(for_write=True)
        wanted = bool(enabled) and bool(data["selected"])
        if data["enabled"] == wanted:
            return
        data["enabled"] = wanted
        self._save(data)

    # ---- upkeep ------------------------------------------------------------

    def forget(self, graph_id: str) -> None:
        """Drop every private decision about *graph_id* — used when it is deleted."""
        self._require_graph_id(graph_id)
        data = self._load(for_write=True)
        if data["selected"] != graph_id:
            return
        data["selected"] = ""
        data["enabled"] = False
        self._save(data)

    # ---- persistence -------------------------------------------------------

    def _load(self, *, for_write: bool = False) -> dict:
        blank: dict = {"selected": "", "enabled": False}
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
            selected = raw.get("selected", "")
            version = raw.get("version", 1)
            enabled = raw.get("enabled", False)
            available = raw.get("available", [])
            if not isinstance(selected, str) or not isinstance(version, int):
                raise ValueError("the workflow state has an invalid shape")
            if version >= 2 and not isinstance(enabled, bool):
                raise ValueError("the workflow state has an invalid enabled value")
            if version < 2 and not isinstance(available, list):
                raise ValueError("the legacy workflow availability is invalid")
        except Exception as exc:
            logger.debug(
                "agents: could not read workflow state %s", self._path, exc_info=True
            )
            if for_write:
                raise self._corrupt() from exc
            return blank

        blank["selected"] = selected if is_valid_graph_id(selected) else ""
        if version < 2:
            # The old graph-local checkbox was also a private user decision.
            # Preserve that decision only for the workflow which was selected;
            # the other per-workflow bits have no representation in one master
            # gate. The next write persists only selected+enabled.
            clean_available = {
                item
                for item in available
                if isinstance(item, str) and is_valid_graph_id(item)
            }
            blank["enabled"] = blank["selected"] in clean_available
        else:
            blank["enabled"] = bool(enabled) and bool(blank["selected"])
        return blank

    def _save(self, data: dict) -> None:
        payload = {
            "version": _STATE_VERSION,
            "workspace": str(self._workspace_root),
            "selected": str(data["selected"]),
            "enabled": bool(data["enabled"]),
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
    "ENABLED_LABEL",
    "ENABLED_NOTE",
    "WorkflowLocalState",
    "WorkflowLocalStateError",
]
