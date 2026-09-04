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
from collections.abc import Mapping
from enum import Enum
from pathlib import Path

from aura.agents.identity import is_valid_agent_id
from aura.conversation.tools.fs_write import atomic_write_bytes
from aura.paths import data_dir

logger = logging.getLogger(__name__)

#: Bumped when the former three-way grant became Read only / Read / Write.
_STATE_VERSION = 2


class AgentLocalStateError(RuntimeError):
    """A private roster or permission decision could not be persisted."""


class AgentPermission(str, Enum):
    """What a user has locally allowed one agent to do.

    There are two, and the gap between them is the whole authority model.
    ``READ_ONLY`` reads and nothing else, and is what every agent the user has
    not decided about starts as. ``READ_WRITE`` is the isolated writable
    capability: edits land in a runtime-owned linked Git worktree and terminal
    commands are available *there*. Neither ever writes the canonical
    workspace — a result reaches it only when the user approves applying the
    retained change set.

    Older private state named a third grant, splitting editing from running
    commands. Reading normalizes both of those to ``READ_WRITE``, in one
    direction, and the next write persists only these two values.
    """

    READ_ONLY = "read_only"
    READ_WRITE = "read_write"

    @property
    def label(self) -> str:
        return _PERMISSION_LABELS[self]

    @property
    def allows_edit(self) -> bool:
        return self is AgentPermission.READ_WRITE

    @property
    def allows_terminal(self) -> bool:
        """True exactly when editing is allowed — in the same worktree."""
        return self is AgentPermission.READ_WRITE

    @classmethod
    def parse(cls, raw: object) -> "AgentPermission | None":
        if isinstance(raw, AgentPermission):
            return raw
        if not isinstance(raw, str):
            return None
        text = raw.strip().lower()
        if text in _LEGACY_PERMISSIONS:
            return _LEGACY_PERMISSIONS[text]
        try:
            return cls(text)
        except ValueError:
            return None


_PERMISSION_LABELS: dict[AgentPermission, str] = {
    AgentPermission.READ_ONLY: "Read only",
    AgentPermission.READ_WRITE: "Read / Write",
}

#: The two grants an older local-state file could hold, and the single grant
#: each of them now means. Read-only compatibility: nothing writes these back.
_LEGACY_PERMISSIONS: dict[str, AgentPermission] = {
    "worktree_edit": AgentPermission.READ_WRITE,
    "worktree_edit_terminal": AgentPermission.READ_WRITE,
}

#: Display order for the permission control, least authority first.
PERMISSION_ORDER: tuple[AgentPermission, ...] = (
    AgentPermission.READ_ONLY,
    AgentPermission.READ_WRITE,
)

#: What every agent starts as, everywhere, for everyone.
DEFAULT_PERMISSION = AgentPermission.READ_ONLY


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
        return self.explicit_permission(agent_id) or DEFAULT_PERMISSION

    def explicit_permission(self, agent_id: str) -> AgentPermission | None:
        """Return the persisted grant, or None while the safe default applies.

        Most callers want :meth:`permission`. Retention also needs to tell a
        missing grant left by an interrupted multi-file save from a later,
        explicit permission change so retries stay safe without overwriting a
        newer user decision.
        """
        self._require_agent_id(agent_id)
        raw = self._load()["permissions"].get(agent_id)
        return AgentPermission.parse(raw)

    def set_permission(self, agent_id: str, permission: AgentPermission) -> None:
        self._require_agent_id(agent_id)
        data = self._load(for_write=True)
        data["permissions"][agent_id] = AgentPermission(permission).value
        self._save(data)

    def set_permissions(
        self, permissions: Mapping[str, AgentPermission]
    ) -> None:
        """Persist several exact grants in one local-state write."""
        checked: dict[str, AgentPermission] = {}
        for agent_id, permission in permissions.items():
            self._require_agent_id(agent_id)
            checked[agent_id] = AgentPermission(permission)
        if not checked:
            return
        data = self._load(for_write=True)
        changed = False
        for agent_id, permission in checked.items():
            if data["permissions"].get(agent_id) != permission.value:
                data["permissions"][agent_id] = permission.value
                changed = True
        if changed:
            self._save(data)

    def retain_available_agent(
        self, agent_id: str, permission: AgentPermission
    ) -> None:
        """Persist one retained Agent's grant and roster membership atomically."""
        self._require_agent_id(agent_id)
        checked = AgentPermission(permission)
        data = self._load(for_write=True)
        changed = False
        if data["permissions"].get(agent_id) != checked.value:
            data["permissions"][agent_id] = checked.value
            changed = True
        if agent_id not in data["available"]:
            data["available"].append(agent_id)
            changed = True
        if changed:
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
        # Parse, then keep the parsed value rather than the raw one: a grant
        # written under the older three-way vocabulary is normalized here,
        # once, and the next _save writes only the current two values.
        clean_permissions: dict[str, str] = {}
        for key, value in permissions.items():
            if not (isinstance(key, str) and is_valid_agent_id(key)):
                continue
            parsed = AgentPermission.parse(value)
            if parsed is not None:
                clean_permissions[key] = parsed.value
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
    "AgentLocalState",
    "AgentLocalStateError",
    "AgentPermission",
    "workspace_key",
]
