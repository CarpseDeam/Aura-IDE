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
    """Holds planner refresh configuration and provides mid-turn methods."""

    def __init__(self) -> None:
        self._base_system_prompt: str | None = None
        self._workspace_root: Path | None = None

    def configure(self, base_prompt: str, workspace_root: Path) -> None:
        """Store the base system prompt template and workspace root for mid-turn refresh."""
        self._base_system_prompt = base_prompt
        self._workspace_root = workspace_root

    def refresh_tier1_after_writes(self, history) -> None:
        """Rebuild Tier 1 context with force-refreshed repo map and update system prompt.

        Called after a worker dispatch completes with file writes. Forces repo map
        regeneration so the planner's next LLM round sees updated code structure.
        Does nothing if configure was not called.
        """
        if self._base_system_prompt is None or self._workspace_root is None:
            return
        try:
            composed = compose_system_prompt(
                RuntimeRole.PLANNER,
                self._base_system_prompt,
                self._workspace_root,
                force=True,
            )
            metadata = context_gearbox_metadata(composed.ledger)
            logger.info(
                "planner_context_refresh_summary %s",
                metadata["summary"]["display"],
            )
            history.set_system(composed.system_prompt)
        except Exception:
            logger.warning(
                "Failed to refresh Tier 1 context after worker writes", exc_info=True
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
