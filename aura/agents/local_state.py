"""Private per-user, per-workspace agent state.

A definition says what an agent is. This file says what *this* user, in
*this* workspace, has decided about it: whether Aura may use it at all, in
what order, and what it is allowed to touch. None of that is ever written
into the project, so a definition someone commits to a repository arrives on
every other machine inactive and read-only until that machine's user says
otherwise.

State lives under the user's own Aura data directory, keyed by a digest of
the workspace path::

    <data_dir>/agents/workspaces/<digest>.json

The digest keys the file; the workspace path is recorded inside it so the
file is identifiable when read by a human. Two workspaces never share a
file, and nothing here is discoverable by, or writable from, a project.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
from enum import Enum
from pathlib import Path

from aura.agents.identity import is_valid_agent_id
from aura.conversation.tools.fs_write import atomic_write_bytes
from aura.paths import data_dir

logger = logging.getLogger(__name__)

_STATE_VERSION = 1


class AgentLocalStateError(RuntimeError):
    """A private roster or permission decision could not be persisted."""


class AgentPermission(str, Enum):
    """What a user has locally allowed one agent to do.

    The three grants are ordered, least to most: reading is always allowed,
    editing happens only inside an isolated worktree, and running terminal
    commands is a separate, deliberate step beyond editing. ``READ_ONLY`` is
    the default for every agent the user has not explicitly decided about.
    """

    READ_ONLY = "read_only"
    WORKTREE_EDIT = "worktree_edit"
    WORKTREE_EDIT_TERMINAL = "worktree_edit_terminal"

    @property
    def label(self) -> str:
        return _PERMISSION_LABELS[self]

    @property
    def allows_edit(self) -> bool:
        return self is not AgentPermission.READ_ONLY

    @property
    def allows_terminal(self) -> bool:
        return self is AgentPermission.WORKTREE_EDIT_TERMINAL

    @classmethod
    def parse(cls, raw: object) -> "AgentPermission | None":
        if isinstance(raw, AgentPermission):
            return raw
        if not isinstance(raw, str):
            return None
        try:
            return cls(raw.strip().lower())
        except ValueError:
            return None


_PERMISSION_LABELS: dict[AgentPermission, str] = {
    AgentPermission.READ_ONLY: "Read only",
    AgentPermission.WORKTREE_EDIT: "Edit in isolated worktree",
    AgentPermission.WORKTREE_EDIT_TERMINAL: "Edit in isolated worktree + terminal",
}

#: Display order for the permission control, least authority first.
PERMISSION_ORDER: tuple[AgentPermission, ...] = (
    AgentPermission.READ_ONLY,
    AgentPermission.WORKTREE_EDIT,
    AgentPermission.WORKTREE_EDIT_TERMINAL,
)

#: What every agent starts as, everywhere, for everyone.
DEFAULT_PERMISSION = AgentPermission.READ_ONLY

#: The warning the management surface must show wherever terminal authority
#: can be granted. Terminal access is not a sandbox boundary and must never
#: be presented as one.
TERMINAL_WARNING = (
    "Terminal commands run as you, on this machine, with your own account's "
    "access. Aura does not sandbox them at the OS level — an isolated worktree "
    "separates an agent's edits, not what a command it runs can reach."
)


def workspace_key(workspace_root: Path | str) -> str:
    """The stable digest that names one workspace's private state file."""
    try:
        resolved = Path(workspace_root).expanduser().resolve()
    except OSError:
        resolved = Path(workspace_root).expanduser()
    canonical = os.path.normcase(str(resolved))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


class AgentLocalState:
    """Reads and writes one workspace's private roster and permission grants.

    ``state_root`` overrides the base directory purely for test isolation,
    the same way :class:`aura.skills.manifest.SkillManifest` takes a personal
    state directory; production always uses ``data_dir()/agents``.
    """

    def __init__(self, workspace_root: Path | str, *, state_root: Path | None = None) -> None:
        self._workspace_root = Path(workspace_root)
        base = Path(state_root) if state_root is not None else (data_dir() / "agents")
        self._path = base / "workspaces" / f"{workspace_key(self._workspace_root)}.json"

    @property
    def path(self) -> Path:
        """Where this workspace's private state is kept — never inside the project."""
        return self._path

    # ---- roster ------------------------------------------------------------

    def available_ids(self) -> tuple[str, ...]:
        """The ordered ids the user has made available to Aura."""
        return tuple(self._load()["available"])

    def is_available(self, agent_id: str) -> bool:
        self._require_agent_id(agent_id)
        return agent_id in self._load()["available"]

    def set_available(self, agent_id: str, available: bool) -> None:
        """Add *agent_id* to the end of the roster, or take it off.

        Appending rather than inserting keeps the order the user built: a
        newly activated agent joins the list where they just put it.
        """
        self._require_agent_id(agent_id)
        data = self._load(for_write=True)
        roster: list[str] = data["available"]
        if available and agent_id not in roster:
            roster.append(agent_id)
        elif not available and agent_id in roster:
            roster.remove(agent_id)
        else:
            return
        self._save(data)

    def set_available_ids(self, agent_ids: tuple[str, ...] | list[str]) -> None:
        """Replace the roster outright, preserving the given order."""
        data = self._load(for_write=True)
        seen: list[str] = []
        for agent_id in agent_ids:
            text = str(agent_id)
            self._require_agent_id(text)
            if text and text not in seen:
                seen.append(text)
        data["available"] = seen
        self._save(data)

    # ---- permission --------------------------------------------------------

    def permission(self, agent_id: str) -> AgentPermission:
        """What *agent_id* may do here. Unknown agents are read-only."""
        self._require_agent_id(agent_id)
        raw = self._load()["permissions"].get(agent_id)
        return AgentPermission.parse(raw) or DEFAULT_PERMISSION

    def set_permission(self, agent_id: str, permission: AgentPermission) -> None:
        self._require_agent_id(agent_id)
        data = self._load(for_write=True)
        data["permissions"][agent_id] = AgentPermission(permission).value
        self._save(data)

    # ---- upkeep ------------------------------------------------------------

    def forget(self, agent_id: str) -> None:
        """Drop every local decision about *agent_id* — used when it is deleted."""
        self._require_agent_id(agent_id)
        data = self._load(for_write=True)
        changed = False
        if agent_id in data["available"]:
            data["available"].remove(agent_id)
            changed = True
        if agent_id in data["permissions"]:
            del data["permissions"][agent_id]
            changed = True
        if changed:
            self._save(data)

    # ---- persistence -------------------------------------------------------

    def _load(self, *, for_write: bool = False) -> dict:
        blank: dict = {"available": [], "permissions": {}}
        try:
            mode = self._path.stat().st_mode
        except FileNotFoundError:
            return blank
        except OSError as exc:
            logger.debug("agents: could not inspect local state %s", self._path, exc_info=True)
            if for_write:
                raise AgentLocalStateError(
                    "Could not update Agent roster and permissions because the existing "
                    "local-state file is unreadable or corrupt."
                ) from exc
            return blank
        try:
            if not stat.S_ISREG(mode):
                raise OSError("the local-state path is not a regular file")
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("the local-state document is not an object")
            available = raw.get("available", [])
            permissions = raw.get("permissions", {})
            if not isinstance(available, list) or not isinstance(permissions, dict):
                raise ValueError("the local-state roster or permissions have an invalid shape")
        except Exception as exc:
            logger.debug("agents: could not read local state %s", self._path, exc_info=True)
            if for_write:
                raise AgentLocalStateError(
                    "Could not update Agent roster and permissions because the existing "
                    "local-state file is unreadable or corrupt."
                ) from exc
            return blank

        clean_available = [
            item
            for item in available
            if isinstance(item, str) and is_valid_agent_id(item)
        ]
        clean_permissions = {
            str(key): str(value)
            for key, value in permissions.items()
            if (
                isinstance(key, str)
                and is_valid_agent_id(key)
                and AgentPermission.parse(value) is not None
            )
        }
        if for_write and (
            len(clean_available) != len(available)
            or len(clean_permissions) != len(permissions)
        ):
            raise AgentLocalStateError(
                "Could not update Agent roster and permissions because the existing "
                "local-state file is unreadable or corrupt."
            )
        blank["available"] = clean_available
        blank["permissions"] = clean_permissions
        return blank

    def _save(self, data: dict) -> None:
        payload = {
            "version": _STATE_VERSION,
            "workspace": str(self._workspace_root),
            "available": list(data["available"]),
            "permissions": dict(data["permissions"]),
        }
        try:
            atomic_write_bytes(
                self._path,
                json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8"),
            )
        except Exception as exc:
            raise AgentLocalStateError(
                f"Could not save Agent roster and permissions: {exc}"
            ) from exc

    @staticmethod
    def _require_agent_id(agent_id: object) -> str:
        if not is_valid_agent_id(agent_id):
            raise AgentLocalStateError(
                f"'{agent_id}' is not a valid immutable agent id."
            )
        return str(agent_id)


__all__ = [
    "DEFAULT_PERMISSION",
    "PERMISSION_ORDER",
    "TERMINAL_WARNING",
    "AgentLocalState",
    "AgentLocalStateError",
    "AgentPermission",
    "workspace_key",
]
