from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)


class ContextRefreshState:
    """Holds the live terrain used when production context is recomposed."""

    def __init__(
        self,
        capabilities_provider: Callable[[], frozenset[str]] | None = None,
    ) -> None:
        # Asked again on every refresh rather than captured once: a mid-turn
        # recomposition must describe the surface as it is now, and a server
        # that dropped away mid-turn has to take its context block with it.
        self._capabilities_provider = capabilities_provider
        self._workspace_root: Path | None = None
        self._model: str | None = None
        self._task_kind: str | None = None
        self._content: str | None = None
        self._target_files: tuple[str, ...] = ()
        self._explicit_install_ids: tuple[str, ...] = ()

    def configure(
        self,
        workspace_root: Path,
        *,
        model: str | None = None,
        task_kind: str | None = None,
        content: str | None = None,
        target_files: tuple[str, ...] = (),
        explicit_install_ids: tuple[str, ...] = (),
    ) -> None:
        """Store the root and this turn's live terrain.

        The terrain is kept so a mid-turn refresh reselects the same skills
        the turn started with instead of silently dropping them.
        """
        self._workspace_root = workspace_root
        self._model = model
        self._task_kind = task_kind
        self._content = content
        self._target_files = tuple(target_files or ())
        self._explicit_install_ids = tuple(explicit_install_ids or ())

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

    @property
    def explicit_install_ids(self) -> tuple[str, ...]:
        return self._explicit_install_ids

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
