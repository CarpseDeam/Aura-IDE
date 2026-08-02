from __future__ import annotations

import logging
from pathlib import Path

from aura.context_gearbox.models import RuntimeRole
from aura.context_gearbox.runtime import context_gearbox_metadata, compose_system_prompt

logger = logging.getLogger(__name__)


def stale_read_notice(modified_files: list[str]) -> str:
    """Return a planner stale-read invalidation notice.

    Inlines path normalization (backslash→slash, strip "./", collapse "//",
    dedup) formerly provided by manager's _unique_worker_paths / _normalize_worker_path.
    """
    unique: list[str] = []
    seen: set[str] = set()
    for path in modified_files:
        normalized = str(path).replace("\\", "/")
        if normalized.startswith("./"):
            normalized = normalized[2:]
        while "//" in normalized:
            normalized = normalized.replace("//", "/")
        normalized = normalized.strip()
        if not normalized or normalized in seen:
            continue
        unique.append(normalized)
        seen.add(normalized)

    bullet_list = "\n".join(f"- {p}" for p in unique)
    return (
        "Planner stale-read invalidation:\n"
        "The Worker modified these files:\n"
        f"{bullet_list}\n\n"
        "Any prior Planner reads of those paths are stale. "
        "Re-read the modified files before planning, dispatching, or reasoning "
        "about further edits involving them. "
        "If the Worker completed successfully, summarize or finish normally; "
        "do not redispatch because of this notice unless the user asks for more."
    )


class PlannerRefreshState:
    """Holds mid-turn context-refresh configuration for the active runtime role.

    Historically planner-only; the production single-agent path uses the same
    machinery with ``RuntimeRole.SINGLE``.  The class name is retained as a
    compatibility alias.
    """

    def __init__(self) -> None:
        self._base_system_prompt: str | None = None
        self._workspace_root: Path | None = None
        # SINGLE is the normal product; the legacy Planner path opts in through
        # ``configure_for_planner``. An unconfigured manager must never append
        # Planner notices to a production turn.
        self._role: RuntimeRole = RuntimeRole.SINGLE
        self._model: str | None = None
        self._task_kind: str | None = None
        self._content: str | None = None
        self._target_files: tuple[str, ...] = ()

    def configure(
        self,
        base_prompt: str,
        workspace_root: Path,
        role: RuntimeRole | str = RuntimeRole.PLANNER,
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

    def refresh_tier1_after_writes(
        self,
        history,
        target_files: tuple[str, ...] | list[str] = (),
    ) -> None:
        """Rebuild Tier 1 context with force-refreshed repo map and update system prompt.

        Called after file writes land. Forces repo map regeneration so the next
        model round sees updated code structure. Composes against the configured
        runtime role, so the production single-agent path never re-injects a
        Planner posture. Does nothing if configure was not called.
        """
        if self._base_system_prompt is None or self._workspace_root is None:
            return
        known_targets = tuple(dict.fromkeys((*self._target_files, *tuple(target_files or ()))))
        try:
            composed = compose_system_prompt(
                self._role,
                self._base_system_prompt,
                self._workspace_root,
                force=True,
                model=self._model,
                task_kind=self._task_kind,
                target_files=known_targets,
                content=self._content,
            )
            metadata = context_gearbox_metadata(
                composed.ledger, workspace_root=self._workspace_root,
            )
            logger.info(
                "%s_context_refresh_summary %s",
                self._role.value,
                metadata["summary"]["display"],
            )
            history.set_system(composed.system_prompt)
        except Exception:
            logger.warning(
                "Failed to refresh Tier 1 context after writes", exc_info=True
            )

    def handle_post_write_notices(
        self, history, modified_files: list[str]
    ) -> None:
        """Handle all post-write context updates, owned by the active role.

        Production ``SINGLE`` refreshes Tier 1 context *silently*: the repo map
        and system prompt recompose after a write, but no ``Planner stale-read
        invalidation`` message and no dependency notice are appended, so the
        turn keeps exactly one user boundary — the real request. Nothing here
        touches the pre-edit guard or edit-recovery state; those live in the
        tool round and are unchanged.

        Legacy ``PLANNER`` keeps the historical notices: the stale-read
        invalidation user message, then the Tier 1 refresh, then the dependent
        planner notice.

        1. If modified_files is empty, return.
        2. Refresh Tier 1 context.
        3. (PLANNER only) Append stale-read notice.
        4. (PLANNER only) Append dependent planner notice (force_graph=True).
        """
        if not modified_files:
            return

        # Files the run just wrote are the target files we now know about.
        if self._role == RuntimeRole.SINGLE:
            self.refresh_tier1_after_writes(history, tuple(modified_files))
            return

        history.append_user_text(stale_read_notice(modified_files))
        self.refresh_tier1_after_writes(history, tuple(modified_files))

        if self._workspace_root is not None:
            from aura.dependency_context import build_dependent_planner_notice

            notice = build_dependent_planner_notice(
                self._workspace_root,
                modified_files,
                force_graph=True,
            )
            if notice:
                history.append_user_text(notice)
