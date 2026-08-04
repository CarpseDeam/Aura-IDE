from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from aura.context_gearbox.models import RuntimeRole

logger = logging.getLogger(__name__)


class PlannerRefreshState:
    """Holds mid-turn context-refresh configuration for the active runtime role.

    Historically planner-only; the production single-agent path uses the same
    machinery with ``RuntimeRole.SINGLE``.  The class name is retained as a
    compatibility alias.
    """

    def __init__(
        self,
        capabilities_provider: Callable[[], frozenset[str]] | None = None,
    ) -> None:
        # Asked again on every refresh rather than captured once: a mid-turn
        # recomposition must describe the surface as it is now, and a server
        # that dropped away mid-turn has to take its context block with it.
        self._capabilities_provider = capabilities_provider
        self._base_system_prompt: str | None = None
        self._workspace_root: Path | None = None
        # SINGLE is the normal product; the legacy Planner path opted in through
        # ``configure_for_planner`` and is gone. An unconfigured manager never
        # appends notices to a production turn.
        self._role: RuntimeRole = RuntimeRole.SINGLE
        self._model: str | None = None
        self._task_kind: str | None = None
        self._content: str | None = None
        self._target_files: tuple[str, ...] = ()

    def configure(
        self,
        base_prompt: str,
        workspace_root: Path,
        role: RuntimeRole | str = RuntimeRole.SINGLE,
        *,
        model: str | None = None,
        task_kind: str | None = None,
        content: str | None = None,
        target_files: tuple[str, ...] = (),
    ) -> None:
        """Store the base prompt, root, role, and this turn's live terrain.

        The terrain is kept so a mid-turn refresh reselects the same skills
        the turn started with instead of silently dropping them.
        """
        self._base_system_prompt = base_prompt
        self._workspace_root = workspace_root
        self._role = RuntimeRole.from_value(role)
        self._model = model
        self._task_kind = task_kind
        self._content = content
        self._target_files = tuple(target_files or ())

    @property
    def role(self) -> RuntimeRole:
        return self._role

    @property
    def workspace_root(self) -> Path | None:
        return self._workspace_root

    @property
    def model(self) -> str | None:
        return self._model

    @property
    def task_kind(self) -> str | None:
        return self._task_kind

    @property
    def content(self) -> str | None:
        return self._content

    @property
    def target_files(self) -> tuple[str, ...]:
        return self._target_files

    def _active_capabilities(self) -> frozenset[str]:
        if self._capabilities_provider is None:
            return frozenset()
        try:
            return frozenset(self._capabilities_provider())
        except Exception:
            # Withhold rather than assert: a capability block that cannot be
            # confirmed must not claim tools are available.
            logger.warning("Could not read active capabilities", exc_info=True)
            return frozenset()
