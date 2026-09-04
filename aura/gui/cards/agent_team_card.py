"""Compact live receipt for one automatically assembled Agent team."""
from __future__ import annotations

from collections import Counter, defaultdict

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from aura.agents.team_compiler import CompiledAgentTeam
from aura.agents.workflow_helper_execution import WorkflowStepState
from aura.agents.workflow_plan import WorkflowHelperPlan, WorkflowStepPlan
from aura.agents.workflow_runner import WorkflowRunResult, WorkflowRunStatus
from aura.gui.cards._collapsible import _CollapsibleSection
from aura.gui.theme import (
    BG_ALT,
    BG_TOOL_CARD,
    BORDER,
    DANGER,
    FG,
    FG_DIM,
    FG_MUTED,
    LABEL_AGENTS,
    SUCCESS,
    WARN,
)

_FINISHED_STATES = frozenset(
    {
        WorkflowStepState.SUCCEEDED,
        WorkflowStepState.FAILED,
        WorkflowStepState.CANCELLED,
    }
)

_STATE_PRESENTATION = {
    WorkflowStepState.RUNNING: ("●", "Working", LABEL_AGENTS),
    WorkflowStepState.SUCCEEDED: ("✓", "Done", SUCCESS),
    WorkflowStepState.FAILED: ("×", "Failed", DANGER),
    WorkflowStepState.CANCELLED: ("■", "Stopped", WARN),
    WorkflowStepState.SKIPPED: ("–", "Not run", FG_MUTED),
}

_RUN_PRESENTATION = {
    WorkflowRunStatus.COMPLETED: ("Team completed", "DONE", SUCCESS),
    WorkflowRunStatus.PARTIAL: ("Team finished with issues", "ISSUES", WARN),
    WorkflowRunStatus.FAILED: ("Team couldn’t finish", "FAILED", DANGER),
    WorkflowRunStatus.CANCELLED: ("Team stopped", "STOPPED", WARN),
}


def _bounded_text(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


class _AgentTeamOccurrenceRow(QFrame):
    """One solid Step or one actually invoked dashed helper occurrence."""

    def __init__(
        self,
        *,
        name: str,
        detail: str,
        helper_depth: int = 0,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("agentTeamOccurrence")
        self.setMinimumWidth(0)
        self.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        self._state: WorkflowStepState | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12 * helper_depth, 3, 0, 3)
        outer.setSpacing(2)

        headline = QWidget(self)
        headline.setStyleSheet("background: transparent;")
        headline_layout = QHBoxLayout(headline)
        headline_layout.setContentsMargins(0, 0, 0, 0)
        headline_layout.setSpacing(6)

        self._icon = QLabel("○", headline)
        self._icon.setObjectName("agentTeamOccurrenceIcon")
        self._icon.setFixedWidth(14)
        self._icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._icon.setStyleSheet(f"color: {FG_MUTED}; font-weight: 700;")
        headline_layout.addWidget(self._icon)

        prefix = "Sub-agent · " if helper_depth else ""
        self._name = QLabel(prefix + _bounded_text(name, 100), headline)
        self._name.setObjectName("agentTeamOccurrenceName")
        self._name.setTextFormat(Qt.TextFormat.PlainText)
        self._name.setStyleSheet(f"color: {FG}; font-weight: 600;")
        headline_layout.addWidget(self._name, 1)

        self._status = QLabel("Waiting", headline)
        self._status.setObjectName("agentTeamOccurrenceStatus")
        self._status.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self._status.setStyleSheet(f"color: {FG_MUTED}; font-size: 11px;")
        headline_layout.addWidget(self._status)
        outer.addWidget(headline)

        self._detail = QLabel(_bounded_text(detail, 240), self)
        self._detail.setObjectName("agentTeamOccurrenceDetail")
        self._detail.setTextFormat(Qt.TextFormat.PlainText)
        self._detail.setWordWrap(True)
        self._detail.setStyleSheet(
            f"color: {FG_DIM}; font-size: 11px; padding-left: 20px;"
        )
        self._detail.setToolTip(str(detail or ""))
        outer.addWidget(self._detail)

    @property
    def state(self) -> WorkflowStepState | None:
        return self._state

    @property
    def status_text(self) -> str:
        return self._status.text()

    def set_state(
        self,
        state: WorkflowStepState,
        *,
        status_text: str = "",
    ) -> None:
        self._state = state
        glyph, label, color = _STATE_PRESENTATION[state]
        self._icon.setText(glyph)
        self._icon.setStyleSheet(f"color: {color}; font-weight: 700;")
        self._status.setText(status_text or label)
        self._status.setStyleSheet(f"color: {color}; font-size: 11px;")

    def set_did_not_finish(self) -> None:
        self._state = WorkflowStepState.FAILED
        self._icon.setText("×")
        self._icon.setStyleSheet(f"color: {DANGER}; font-weight: 700;")
        self._status.setText("Didn’t finish")
        self._status.setStyleSheet(f"color: {DANGER}; font-size: 11px;")


class AgentTeamCard(QFrame):
    """Session-local, read-only view of one exact compiled team run."""

    layout_changed = Signal()

    def __init__(
        self,
        team: CompiledAgentTeam,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._team = team
        self._result: WorkflowRunResult | None = None
        self._finished = False
        self._rows: dict[str, _AgentTeamOccurrenceRow] = {}
        self._solid_node_ids = tuple(step.node_id for step in team.plan.steps)
        self._helper_node_ids: set[str] = set()

        self.setObjectName("agentTeamCard")
        self.setMinimumWidth(0)
        self.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        self.setStyleSheet(
            f"QFrame#agentTeamCard {{ background: {BG_TOOL_CARD}; "
            f"border: 1px solid {BORDER}; border-left: 3px solid {LABEL_AGENTS}; "
            "border-radius: 8px; }"
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 11, 14, 11)
        outer.setSpacing(5)

        header = QWidget(self)
        header.setStyleSheet("background: transparent;")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(8)
        self._header_label = QLabel("Aura assembled a team", header)
        self._header_label.setObjectName("agentTeamHeader")
        self._header_label.setStyleSheet(
            f"color: {LABEL_AGENTS}; font-weight: 700; font-size: 12px;"
        )
        header_layout.addWidget(self._header_label, 1)
        self._status_chip = QLabel("WORKING", header)
        self._status_chip.setObjectName("agentTeamStatusChip")
        self._set_status_chip("WORKING", LABEL_AGENTS)
        header_layout.addWidget(self._status_chip)
        outer.addWidget(header)

        self._name_label = QLabel(_bounded_text(team.plan.name, 100), self)
        self._name_label.setObjectName("agentTeamName")
        self._name_label.setTextFormat(Qt.TextFormat.PlainText)
        self._name_label.setStyleSheet(f"color: {FG}; font-weight: 600;")
        self._name_label.setWordWrap(True)
        outer.addWidget(self._name_label)

        distinct_agents = len(set(team.plan.agent_ids))
        steps = len(team.plan.steps)
        self._meta_label = QLabel(
            f"{steps} step{'s' if steps != 1 else ''} · "
            f"{distinct_agents} specialist{'s' if distinct_agents != 1 else ''}",
            self,
        )
        self._meta_label.setObjectName("agentTeamMeta")
        self._meta_label.setStyleSheet(f"color: {FG_DIM}; font-size: 11px;")
        outer.addWidget(self._meta_label)

        self._notice_label = QLabel("", self)
        self._notice_label.setObjectName("agentTeamNotice")
        self._notice_label.setTextFormat(Qt.TextFormat.PlainText)
        self._notice_label.setWordWrap(True)
        self._notice_label.setVisible(False)
        outer.addWidget(self._notice_label)

        details = QWidget(self)
        details.setStyleSheet("background: transparent;")
        details_layout = QVBoxLayout(details)
        details_layout.setContentsMargins(0, 3, 0, 0)
        details_layout.setSpacing(3)

        description = _bounded_text(team.plan.description, 240)
        if description:
            description_label = QLabel(description, details)
            description_label.setObjectName("agentTeamDescription")
            description_label.setTextFormat(Qt.TextFormat.PlainText)
            description_label.setWordWrap(True)
            description_label.setStyleSheet(
                f"color: {FG_DIM}; font-size: 11px; padding-bottom: 3px;"
            )
            details_layout.addWidget(description_label)

        occurrence_names = {step.node_id: step.agent_name for step in team.plan.steps}
        step_name_counts = Counter(step.agent_name for step in team.plan.steps)
        step_route_labels = {
            step.node_id: (
                step.agent_name
                if step_name_counts[step.agent_name] == 1
                else f"{step.agent_name} — {_bounded_text(step.assignment, 48)}"
            )
            for step in team.plan.steps
        }
        for step in team.plan.steps:
            for helper in step.helpers:
                occurrence_names.update(
                    (item.node_id, item.agent_name) for item in helper.preorder()
                )
        for step in team.plan.steps:
            row = self._make_step_row(step, step_route_labels, details)
            self._rows[step.node_id] = row
            details_layout.addWidget(row)
            for helper in step.helpers:
                self._add_helper_rows(
                    helper, occurrence_names, details, details_layout
                )

        self._details = _CollapsibleSection(
            f"{self._progress_title()} · Hide details",
            details,
            start_open=True,
            prominent=False,
            parent=self,
        )
        self._details.setObjectName("agentTeamDetails")
        self._details_prefix = self._progress_title()
        self._details._toggle.clicked.connect(self._on_details_toggled)
        outer.addWidget(self._details)

    @property
    def compiled_team(self) -> CompiledAgentTeam:
        """The exact immutable team retained for the later Keep action."""
        return self._team

    @property
    def graph_id(self) -> str:
        return self._team.plan.graph_id

    @property
    def result(self) -> WorkflowRunResult | None:
        return self._result

    def occurrence_status(self, node_id: str) -> str:
        row = self._rows.get(node_id)
        return row.status_text if row is not None else ""

    def occurrence_visible(self, node_id: str) -> bool:
        row = self._rows.get(node_id)
        return row is not None and not row.isHidden()

    def update_occurrence(self, node_id: str, raw_state: str) -> bool:
        """Apply one native Workflow state, ignoring unknown/stale facts."""
        if self._finished:
            return False
        row = self._rows.get(node_id)
        try:
            state = WorkflowStepState(raw_state)
        except ValueError:
            return False
        if row is None:
            return False
        if node_id in self._helper_node_ids:
            row.setVisible(True)
        row.set_state(state)
        self._set_details_prefix(self._progress_title())
        self.layout_changed.emit()
        return True

    def finish(self, result: WorkflowRunResult) -> bool:
        """Reconcile the card from the runner's complete terminal facts."""
        if result.graph_id != self.graph_id or self._finished:
            return False
        self._finished = True
        self._result = result

        outcomes = {outcome.node_id: outcome for outcome in result.steps}
        for node_id in self._solid_node_ids:
            row = self._rows[node_id]
            outcome = outcomes.get(node_id)
            if outcome is not None:
                row.set_state(outcome.state)
            elif row.state is WorkflowStepState.RUNNING:
                row.set_did_not_finish()
            elif row.state is None:
                row.set_state(WorkflowStepState.SKIPPED)
            # Preserve a terminal live fact when a late orchestration failure
            # returns no duplicate step outcome for that occurrence.

        invocations: dict[str, list[WorkflowStepState]] = defaultdict(list)
        for invocation in result.helper_invocations:
            invocations[invocation.helper_node_id].append(invocation.state)
        for node_id, states in invocations.items():
            row = self._rows.get(node_id)
            if row is None:
                continue
            row.setVisible(True)
            aggregate = _aggregate_helper_state(states)
            row.set_state(
                aggregate,
                status_text=_helper_status(states, aggregate),
            )
        for node_id in self._helper_node_ids:
            row = self._rows[node_id]
            if row.isHidden():
                continue
            if node_id not in invocations and row.state is WorkflowStepState.RUNNING:
                row.set_did_not_finish()

        title, chip, color = _RUN_PRESENTATION[result.status]
        self._header_label.setText(title)
        self._header_label.setStyleSheet(
            f"color: {color}; font-weight: 700; font-size: 12px;"
        )
        self._set_status_chip(chip, color)
        self._show_terminal_notice(result)

        total = len(self._solid_node_ids)
        # Once the run is terminal the rows are the truthful receipt. A
        # neutral count avoids presenting skipped/not-started work as work
        # that finished (notably when runner preflight fails before step 1).
        details_prefix = f"{total} step{'s' if total != 1 else ''}"
        self._details.set_open(result.status is not WorkflowRunStatus.COMPLETED)
        self._set_details_prefix(details_prefix)
        self.layout_changed.emit()
        return True

    def _make_step_row(
        self,
        step: WorkflowStepPlan,
        step_route_labels: dict[str, str],
        parent: QWidget,
    ) -> _AgentTeamOccurrenceRow:
        route: list[str] = []
        if step.predecessors:
            names = [
                step_route_labels.get(node_id, node_id)
                for node_id in step.predecessors
            ]
            route.append("After " + ", ".join(names))
        elif step.from_task:
            route.append("Receives the task")
        if step.to_result:
            route.append("Returns to Aura")
        if step.helpers:
            route.append(
                "Can ask " + ", ".join(helper.agent_name for helper in step.helpers)
            )
        detail = _occurrence_detail(
            step.assignment,
            step.resolved.provider,
            step.resolved.model,
            step.permission.label,
            route,
        )
        return _AgentTeamOccurrenceRow(
            name=step.agent_name,
            detail=detail,
            parent=parent,
        )

    def _add_helper_rows(
        self,
        helper: WorkflowHelperPlan,
        occurrence_names: dict[str, str],
        parent: QWidget,
        layout: QVBoxLayout,
    ) -> None:
        owner_name = occurrence_names.get(
            helper.immediate_parent_node_id, "its specialist"
        )
        detail = _occurrence_detail(
            helper.assignment,
            helper.resolved.provider,
            helper.resolved.model,
            helper.permission.label,
            [f"Available to {owner_name}"],
        )
        row = _AgentTeamOccurrenceRow(
            name=helper.agent_name,
            detail=detail,
            helper_depth=max(1, helper.depth),
            parent=parent,
        )
        row.setVisible(False)
        self._rows[helper.node_id] = row
        self._helper_node_ids.add(helper.node_id)
        layout.addWidget(row)
        for child in helper.children:
            self._add_helper_rows(child, occurrence_names, parent, layout)

    def _finished_step_count(self) -> int:
        return sum(
            1
            for node_id in self._solid_node_ids
            if self._rows[node_id].state in _FINISHED_STATES
        )

    def _progress_title(self) -> str:
        total = len(self._solid_node_ids)
        finished = self._finished_step_count()
        if finished == 0 and not any(
            self._rows[node_id].state is WorkflowStepState.RUNNING
            for node_id in self._solid_node_ids
        ):
            return f"Waiting to start · {total} step{'s' if total != 1 else ''}"
        return f"{finished} of {total} steps finished"

    def _set_status_chip(self, text: str, color: str) -> None:
        self._status_chip.setText(text)
        self._status_chip.setStyleSheet(
            f"color: {color}; background: {BG_ALT}; border: 1px solid {color}; "
            "border-radius: 4px; padding: 2px 6px; font-size: 9px; font-weight: 700;"
        )

    def _on_details_toggled(self) -> None:
        self._set_details_prefix(self._details_prefix)
        self.layout_changed.emit()

    def _set_details_prefix(self, prefix: str) -> None:
        self._details_prefix = prefix
        action = "Hide details" if self._details._open else "View details"
        self._details.set_title(f"{prefix} · {action}")

    def _show_terminal_notice(self, result: WorkflowRunResult) -> None:
        warning = result.extras.get("lifecycle_warning")
        warning_text = warning.get("error", "") if isinstance(warning, dict) else ""
        notices = []
        if result.error:
            notices.append(str(result.error))
        if warning_text and warning_text not in notices:
            notices.append(f"Cleanup warning: {warning_text}")
        if not notices:
            return
        color = DANGER if result.status is WorkflowRunStatus.FAILED else WARN
        self._notice_label.setText(_bounded_text(" ".join(notices), 400))
        self._notice_label.setStyleSheet(f"color: {color}; font-size: 11px;")
        self._notice_label.setVisible(True)


def _occurrence_detail(
    assignment: str,
    provider: str,
    model: str,
    permission: str,
    route: list[str],
) -> str:
    target = " / ".join(value for value in (provider, model) if value)
    facts = [_bounded_text(assignment, 160), target, permission, *route]
    return " · ".join(fact for fact in facts if fact)


def _aggregate_helper_state(states: list[WorkflowStepState]) -> WorkflowStepState:
    if any(state is WorkflowStepState.FAILED for state in states):
        return WorkflowStepState.FAILED
    if any(state is WorkflowStepState.CANCELLED for state in states):
        return WorkflowStepState.CANCELLED
    if all(state is WorkflowStepState.SUCCEEDED for state in states):
        return WorkflowStepState.SUCCEEDED
    return WorkflowStepState.SKIPPED


def _helper_status(
    states: list[WorkflowStepState], aggregate: WorkflowStepState
) -> str:
    base = _STATE_PRESENTATION[aggregate][1]
    if len(states) == 1:
        return base
    counts = Counter(states)
    parts = []
    for state in (
        WorkflowStepState.SUCCEEDED,
        WorkflowStepState.FAILED,
        WorkflowStepState.CANCELLED,
        WorkflowStepState.SKIPPED,
    ):
        count = counts.get(state, 0)
        if count:
            label = _STATE_PRESENTATION[state][1].lower()
            parts.append(f"{count} {label}")
    return f"{len(states)} calls · " + ", ".join(parts)


__all__ = ["AgentTeamCard"]
