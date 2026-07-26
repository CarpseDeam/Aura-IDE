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
        self._role: RuntimeRole = RuntimeRole.PLANNER

    def configure(
        self,
        base_prompt: str,
        workspace_root: Path,
        role: RuntimeRole | str = RuntimeRole.PLANNER,
    ) -> None:
        """Store the base system prompt, workspace root, and role for mid-turn refresh."""
        self._base_system_prompt = base_prompt
        self._workspace_root = workspace_root
        self._role = RuntimeRole.from_value(role)

    @property
    def role(self) -> RuntimeRole:
        return self._role

    def refresh_tier1_after_writes(self, history) -> None:
        """Rebuild Tier 1 context with force-refreshed repo map and update system prompt.

        Called after file writes land. Forces repo map regeneration so the next
        model round sees updated code structure. Composes against the configured
        runtime role, so the production single-agent path never re-injects a
        Planner posture. Does nothing if configure was not called.
        """
        if self._base_system_prompt is None or self._workspace_root is None:
            return
        try:
            composed = compose_system_prompt(
                self._role,
                self._base_system_prompt,
                self._workspace_root,
                force=True,
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
        """Handle all post-Writer-write notices in one call.

        1. If modified_files is empty, return.
        2. Append stale-read notice to history.
        3. Refresh Tier 1 context.
        4. Append dependent planner notice (with force_graph=True) if applicable.
        """
        if not modified_files:
            return

        history.append_user_text(stale_read_notice(modified_files))
        self.refresh_tier1_after_writes(history)

        if self._workspace_root is not None:
            from aura.dependency_context import build_dependent_planner_notice

            notice = build_dependent_planner_notice(
                self._workspace_root,
                modified_files,
                force_graph=True,
            )
            if notice:
                history.append_user_text(notice)
