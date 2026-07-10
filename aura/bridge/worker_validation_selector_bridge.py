"""Owns the Worker validation selector refresh/build lifecycle.

This module owns:
- refreshing/building the Worker validation selector plan after Worker execution
- handling selector failure state
- returning the validation_selector, validation_selector_key, validation_selector_failed tuple
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from aura.bridge.validation_selector_runtime import refresh_validation_selector_plan
from aura.validation.selector import ValidationPlan

_log = logging.getLogger(__name__)


def refresh_worker_validation_selector_plan(
    *,
    relay,
    task_spec,
    task_kind: str,
    context_gearbox: dict[str, Any],
    workspace_root: Path | None,
    final_validation_commands: list[Any],
    validation_selector: ValidationPlan | None,
    validation_selector_key: tuple[str, ...] | None,
    validation_selector_failed: bool,
) -> tuple[ValidationPlan | None, tuple[str, ...] | None, bool]:
    """One-shot refresh of the Worker validation selector plan.

    Called after the worker conversation completes to produce the final
    validation selector state.  Returns the updated three-tuple that
    dispatch.py already consumes.
    """
    return refresh_validation_selector_plan(
        relay=relay,
        task_spec_validation_commands=task_spec.validation_commands,
        task_kind=task_kind,
        context_gearbox=context_gearbox,
        workspace_root=workspace_root,
        final_validation_commands=final_validation_commands,
        validation_selector=validation_selector,
        validation_selector_key=validation_selector_key,
        validation_selector_failed=validation_selector_failed,
    )


class _WorkerValidationSelectorBridge:
    """Structured owner of Worker validation selector state and refresh.

    Tracks validation_selector, validation_selector_key, and
    validation_selector_failed across refresh() calls and exposes the
    three-tuple dispatch.py already consumes.
    """

    def __init__(
        self,
        *,
        task_spec,
        task_kind: str,
        context_gearbox: dict[str, Any],
        workspace_root: Path | None,
        final_validation_commands: list[Any],
    ) -> None:
        self._task_spec = task_spec
        self._task_kind = task_kind
        self._context_gearbox = context_gearbox
        self._workspace_root = workspace_root
        self._final_validation_commands = final_validation_commands

        self.validation_selector: ValidationPlan | None = None
        self.validation_selector_key: tuple[str, ...] | None = None
        self.validation_selector_failed: bool = False

    def refresh(self, relay: Any) -> None:
        """Refresh the validation selector plan based on current relay state.

        Delegates to validation_selector_runtime.refresh_validation_selector_plan
        and updates the owned three-state tuple.
        """
        self.validation_selector, self.validation_selector_key, self.validation_selector_failed = (
            refresh_validation_selector_plan(
                relay=relay,
                task_spec_validation_commands=self._task_spec.validation_commands,
                task_kind=self._task_kind,
                context_gearbox=self._context_gearbox,
                workspace_root=self._workspace_root,
                final_validation_commands=self._final_validation_commands,
                validation_selector=self.validation_selector,
                validation_selector_key=self.validation_selector_key,
                validation_selector_failed=self.validation_selector_failed,
            )
        )
