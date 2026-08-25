"""Enable/disable persistence for installed skills.

State is stored beside Aura's existing config/data ownership split, never by
rewriting an imported ``SKILL.md``: a project skill's disabled flag lives in
that workspace's ``.aura/skills/state.json`` (it travels with the project),
while personal and bundled skills — both user-level, not workspace-specific —
share ``data_dir()/skills/state.json``.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from aura.conversation.tools.fs_write import atomic_write_bytes
from aura.paths import data_dir
from aura.skills.identity import InstalledSkillId, InstallScope

logger = logging.getLogger(__name__)

_STATE_FILENAME = "state.json"


def _load_disabled(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.debug("Failed to read skill state %s", path, exc_info=True)
        return set()
    raw = data.get("disabled", []) if isinstance(data, dict) else []
    if not isinstance(raw, list):
        return set()
    return {str(item) for item in raw if isinstance(item, str)}


def _save_disabled(path: Path, disabled: set[str]) -> None:
    payload = json.dumps({"disabled": sorted(disabled)}, indent=2, ensure_ascii=False)
    atomic_write_bytes(path, payload.encode("utf-8"))


class SkillManifest:
    """Reads and writes enable/disable state for one workspace's skills.

    ``personal_state_dir`` overrides where the shared personal+bundled state
    file lives (default ``data_dir()/skills``) purely for test isolation —
    :class:`aura.skills.library.SkillLibrary` always passes its own
    ``personal_dir``'s parent, so a test that overrides ``personal_dir``
    never touches the developer's real personal-skill state.
    """

    def __init__(self, workspace_root: Path, *, personal_state_dir: Path | None = None) -> None:
        self._workspace_root = Path(workspace_root)
        self._personal_state_dir = Path(personal_state_dir) if personal_state_dir is not None else (data_dir() / "skills")

    def _state_path(self, scope: InstallScope) -> Path:
        if scope == InstallScope.PROJECT:
            return self._workspace_root / ".aura" / "skills" / _STATE_FILENAME
        return self._personal_state_dir / _STATE_FILENAME

    def disabled_ids(self, scope: InstallScope) -> set[str]:
        """Every disabled ``scope:name`` id persisted for *scope*'s state file."""
        try:
            return _load_disabled(self._state_path(scope))
        except Exception:
            logger.debug("disabled_ids failed for scope %s", scope, exc_info=True)
            return set()

    def is_disabled(self, installed_id: InstalledSkillId) -> bool:
        return str(installed_id) in self.disabled_ids(installed_id.scope)

    def set_enabled(self, installed_id: InstalledSkillId, enabled: bool) -> None:
        path = self._state_path(installed_id.scope)
        disabled = _load_disabled(path)
        key = str(installed_id)
        if enabled:
            disabled.discard(key)
        else:
            disabled.add(key)
        _save_disabled(path, disabled)

    def forget(self, installed_id: InstalledSkillId) -> None:
        """Drop any stored state for *installed_id* (used by uninstall)."""
        path = self._state_path(installed_id.scope)
        disabled = _load_disabled(path)
        key = str(installed_id)
        if key in disabled:
            disabled.discard(key)
            _save_disabled(path, disabled)
