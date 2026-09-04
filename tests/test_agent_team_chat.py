"""Automatic teams project into one compact, session-local chat card."""
from __future__ import annotations

import json
import sys
import threading
import time
from dataclasses import replace

import pytest

pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import QObject, Qt, Signal, Slot  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

from aura.agents.delegation import (  # noqa: E402
    DelegationFailure,
    DelegationResult,
    DelegationStatus,
)
from aura.agents.retention import (  # noqa: E402
    AgentRetentionError,
    AgentRetentionResult,
)
from aura.agents.roster import EMPTY_AGENT_ROSTER  # noqa: E402
from aura.agents.team_compiler import CompiledAgentTeam, compile_agent_team  # noqa: E402
from aura.agents.team_spec import (  # noqa: E402
    AgentTeamSpec,
    HandoffSpec,
    HelperSpec,
    NewAgentSpec,
    OccurrenceSpec,
    parse_agent_team_spec,
)
from aura.agents.turn_context import (  # noqa: E402
    AgentModelTargets,
    AgentTurnContext,
    AgentTurnMode,
)
from aura.agents.workflow_helper_execution import (  # noqa: E402
    WorkflowHelperInvocation,
    WorkflowStepState,
)
from aura.agents.workflow_runner import (  # noqa: E402
    WorkflowRunResult,
    WorkflowRunStatus,
    WorkflowStepOutcome,
)
from aura.bridge.qt_bridge import ConversationBridge, _ConversationRunner  # noqa: E402
from aura.client import (  # noqa: E402
    ContentDelta,
    Done,
    ToolCallArgsDelta,
    ToolCallEnd,
    ToolCallStart,
)
from aura.gui.agent_team_chat_controller import AgentTeamChatController  # noqa: E402
from aura.gui.cards.agent_team_card import AgentTeamCard  # noqa: E402
from aura.gui.chat_view import ChatView  # noqa: E402
from aura.gui.widgets.aura_glow import AuraPhaseDriver  # noqa: E402
from aura.model_streams import PRODUCTION_STREAM_HOOK, model_streams  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    yield app


@pytest.fixture(autouse=True)
def _configured_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "aura.config.has_usable_provider_configuration", lambda _provider: True
    )


def _new(alias: str) -> NewAgentSpec:
    return NewAgentSpec(
        alias=alias,
        name=alias.replace("_", " ").title(),
        description=f"Handles the {alias} role.",
        instructions=f"Private instructions for {alias} must never appear.",
        model_target="inherit",
        thinking="inherit",
        permission="read_only",
    )


def _compiled_team() -> CompiledAgentTeam:
    spec = AgentTeamSpec(
        task="Inspect two paths, combine the findings, and return one answer.",
        name="Dependency repair",
        description="Two focused investigations feed one final specialist.",
        new_agents=(
            _new("scout"),
            _new("builder"),
            _new("api_helper"),
            _new("deep_helper"),
        ),
        occurrences=(
            OccurrenceSpec("left", "scout", "Inspect the data path."),
            OccurrenceSpec("right", "scout", "Inspect the API path."),
            OccurrenceSpec("build", "builder", "Combine both reports."),
            OccurrenceSpec("help", "api_helper", "Answer a focused API question."),
            OccurrenceSpec("deep_help", "deep_helper", "Verify one API detail."),
        ),
        handoffs=(
            HandoffSpec("task", "left"),
            HandoffSpec("task", "right"),
            HandoffSpec("left", "build"),
            HandoffSpec("right", "build"),
            HandoffSpec("build", "result"),
        ),
        helpers=(
            HelperSpec("build", "help"),
            HelperSpec("help", "deep_help"),
        ),
    )
    compiled, errors = compile_agent_team(
        spec,
        roster=EMPTY_AGENT_ROSTER,
        model_targets=AgentModelTargets(),
        provider="deepseek",
        model="deepseek-chat",
        thinking="high",
    )
    assert errors == ()
    assert compiled is not None
    return compiled


def _single_agent_payload() -> dict:
    return {
        "task": "Inspect the dependency and return one answer.",
        "team_name": "Dependency review",
        "team_description": "One focused specialist checks the dependency.",
        "new_agents": [
            {
                "alias": "reviewer",
                "name": "Dependency Reviewer",
                "description": "Reviews one dependency path.",
                "instructions": "Inspect the dependency and report concrete evidence.",
                "model_target": "inherit",
                "thinking": "inherit",
                "permission": "read_only",
            }
        ],
        "occurrences": [
            {
                "alias": "review",
                "agent_ref": "reviewer",
                "assignment": "Inspect the dependency and return the finding.",
            }
        ],
        "handoffs": [
            {"source": "task", "target": "review"},
            {"source": "review", "target": "result"},
        ],
        "helpers": [],
    }


def _single_agent_team() -> CompiledAgentTeam:
    parsed = parse_agent_team_spec(_single_agent_payload())
    assert parsed.ok and parsed.spec is not None
    compiled, errors = compile_agent_team(
        parsed.spec,
        roster=EMPTY_AGENT_ROSTER,
        model_targets=AgentModelTargets(),
        provider="deepseek",
        model="deepseek-chat",
        thinking="off",
    )
    assert errors == () and compiled is not None
    return compiled


def _completed(step) -> WorkflowStepOutcome:
    result = DelegationResult(
        status=DelegationStatus.COMPLETED,
        agent_id=step.agent_id,
        agent_name=step.agent_name,
        result="private specialist result",
        provider=step.resolved.provider,
        model=step.resolved.model,
    )
    return WorkflowStepOutcome(step.node_id, WorkflowStepState.SUCCEEDED, result)


def _helper_invocation(team: CompiledAgentTeam) -> WorkflowHelperInvocation:
    helper = team.plan.steps[-1].helpers[0]
    result = DelegationResult(
        status=DelegationStatus.COMPLETED,
        agent_id=helper.agent_id,
        agent_name=helper.agent_name,
        result="private helper result",
        provider=helper.resolved.provider,
        model=helper.resolved.model,
    )
    return WorkflowHelperInvocation(
        invocation=1,
        root_step_node_id=helper.root_step_node_id,
        immediate_parent_node_id=helper.immediate_parent_node_id,
        parent_invocation=None,
        helper_node_id=helper.node_id,
        connection_id=helper.connection_id,
        depth=helper.depth,
        lineage=helper.lineage,
        agent_id=helper.agent_id,
        agent_name=helper.agent_name,
        permission=helper.permission.value,
        state=WorkflowStepState.SUCCEEDED,
        result=result,
    )


def test_card_shows_truthful_live_steps_and_only_invoked_helpers(qapp) -> None:
    team = _compiled_team()
    card = AgentTeamCard(team)
    left, right, build = team.plan.steps
    helper = build.helpers[0]

    assert card.compiled_team is team
    assert card.occurrence_status(left.node_id) == "Waiting"
    assert card.occurrence_status(right.node_id) == "Waiting"
    assert card.occurrence_visible(helper.node_id) is False

    assert card.update_occurrence(left.node_id, "running") is True
    assert card.update_occurrence(right.node_id, "running") is True
    assert card.occurrence_status(left.node_id) == "Working"
    assert card.occurrence_status(right.node_id) == "Working"
    assert card.update_occurrence(helper.node_id, "running") is True
    assert card.occurrence_visible(helper.node_id) is True
    assert card.update_occurrence("stale-node", "running") is False
    assert card.update_occurrence(build.node_id, "not-a-state") is False

    rendered = " ".join(label.text() for label in card.findChildren(QLabel))
    assert "Inspect the data path." in rendered
    assert "deepseek / deepseek-chat" in rendered
    assert "Read only" in rendered
    assert "Private instructions" not in rendered
    assert "private specialist result" not in rendered
    assert "Available to Api Helper" in rendered
    assert "After Scout — Inspect the data path., Scout — Inspect the API path." in rendered


def test_completed_card_collapses_and_reveals_working_retention_only_after_root(qapp) -> None:
    team = _compiled_team()
    card = AgentTeamCard(team)
    helper = team.plan.steps[-1].helpers[0]
    first_helper_call = _helper_invocation(team)
    result = WorkflowRunResult(
        status=WorkflowRunStatus.COMPLETED,
        graph_id=team.plan.graph_id,
        workflow_name=team.plan.name,
        result="one Aura Result",
        steps=tuple(_completed(step) for step in team.plan.steps),
        helper_invocations=(first_helper_call, replace(first_helper_call, invocation=2)),
    )

    assert card.finish(result) is True
    assert card.result is result
    assert card._header_label.text() == "Team completed"
    assert card._status_chip.text() == "DONE"
    assert card._details._open is False
    assert "3 steps · View details" in card._details._toggle.text()
    assert card.occurrence_visible(helper.node_id) is True
    assert card.occurrence_status(helper.node_id) == "2 calls · 2 done"
    assert card.retention_actions_visible is False
    card.settle_root_turn()
    assert card.retention_actions_visible is True
    invoked_agent_ids = {step.agent_id for step in team.plan.steps}
    invoked_agent_ids.add(first_helper_call.agent_id)
    assert set(card.save_agent_ids) == invoked_agent_ids
    deep_helper = helper.children[0]
    assert card._save_rows[deep_helper.agent_id].isHidden()
    assert card._keep_button.text() == "Keep Team"
    assert card.finish(result) is False


def test_generated_helper_becomes_saveable_only_after_it_is_invoked(qapp) -> None:
    team = _compiled_team()
    card = AgentTeamCard(team)
    helper = team.plan.steps[-1].helpers[0]
    deep_helper = helper.children[0]

    assert helper.agent_id not in card.save_agent_ids
    assert deep_helper.agent_id not in card.save_agent_ids
    assert card._save_rows[helper.agent_id].isHidden()
    assert card._save_rows[deep_helper.agent_id].isHidden()

    assert card.update_occurrence(helper.node_id, "running") is True

    assert helper.agent_id in card.save_agent_ids
    assert deep_helper.agent_id not in card.save_agent_ids
    assert not card._save_rows[helper.agent_id].isHidden()
    assert card._save_rows[deep_helper.agent_id].isHidden()

    assert card.update_occurrence(deep_helper.node_id, "failed") is True

    assert deep_helper.agent_id in card.save_agent_ids
    assert not card._save_rows[deep_helper.agent_id].isHidden()


def test_completed_run_keeps_failed_helper_fact_in_collapsed_receipt(qapp) -> None:
    team = _compiled_team()
    card = AgentTeamCard(team)
    helper = team.plan.steps[-1].helpers[0]
    invocation = _helper_invocation(team)
    failed = DelegationResult.failure(
        helper.agent_id,
        DelegationFailure.PROVIDER_ERROR,
        "The optional helper was unavailable.",
        agent_name=helper.agent_name,
    )
    result = WorkflowRunResult(
        status=WorkflowRunStatus.COMPLETED,
        graph_id=team.plan.graph_id,
        workflow_name=team.plan.name,
        result="The solid workflow still completed.",
        steps=tuple(_completed(step) for step in team.plan.steps),
        helper_invocations=(
            replace(invocation, state=WorkflowStepState.FAILED, result=failed),
        ),
    )

    assert card.finish(result) is True
    assert card._header_label.text() == "Team completed"
    assert card._details._open is False
    assert card.occurrence_visible(helper.node_id) is True
    assert card.occurrence_status(helper.node_id) == "Failed"


def test_partial_card_stays_open_and_uses_runner_status(qapp) -> None:
    team = _compiled_team()
    card = AgentTeamCard(team)
    first, second, third = team.plan.steps
    failed = DelegationResult.failure(
        second.agent_id,
        DelegationFailure.PROVIDER_ERROR,
        "The review provider stopped responding.",
        agent_name=second.agent_name,
    )
    skipped = DelegationResult.failure(
        third.agent_id,
        DelegationFailure.DEPENDENCY_NOT_MET,
        "A required branch did not succeed.",
        agent_name=third.agent_name,
    )
    result = WorkflowRunResult(
        status=WorkflowRunStatus.PARTIAL,
        graph_id=team.plan.graph_id,
        workflow_name=team.plan.name,
        result="one branch still returned useful work",
        steps=(
            _completed(first),
            WorkflowStepOutcome(second.node_id, WorkflowStepState.FAILED, failed),
            WorkflowStepOutcome(third.node_id, WorkflowStepState.SKIPPED, skipped),
        ),
        error="The review provider stopped responding.",
    )

    assert card.finish(result) is True
    assert card._header_label.text() == "Team finished with issues"
    assert card._status_chip.text() == "ISSUES"
    assert card._details._open is True
    assert "3 steps · Hide details" in card._details._toggle.text()
    assert card.occurrence_status(second.node_id) == "Failed"
    assert card.occurrence_status(third.node_id) == "Not run"
    assert card._notice_label.text() == "The review provider stopped responding."


@pytest.mark.parametrize(
    ("status", "header", "chip"),
    [
        (WorkflowRunStatus.FAILED, "Team couldn’t finish", "FAILED"),
        (WorkflowRunStatus.CANCELLED, "Team stopped", "STOPPED"),
    ],
)
def test_unsuccessful_run_with_no_outcomes_is_truthful(
    qapp, status: WorkflowRunStatus, header: str, chip: str
) -> None:
    team = _compiled_team()
    card = AgentTeamCard(team)
    result = WorkflowRunResult(
        status=status,
        graph_id=team.plan.graph_id,
        workflow_name=team.plan.name,
        error="The team did not start.",
    )

    assert card.finish(result) is True
    assert card._header_label.text() == header
    assert card._status_chip.text() == chip
    assert card._details._open is True
    assert "3 steps · Hide details" in card._details._toggle.text()
    assert "3 of 3 steps finished" not in card._details._toggle.text()
    assert all(
        card.occurrence_status(step.node_id) == "Not run"
        for step in team.plan.steps
    )


def test_terminal_result_preserves_live_facts_and_cleanup_warning(qapp) -> None:
    team = _compiled_team()
    card = AgentTeamCard(team)
    first, second, _third = team.plan.steps
    card.update_occurrence(first.node_id, "succeeded")
    card.update_occurrence(second.node_id, "running")
    result = WorkflowRunResult(
        status=WorkflowRunStatus.FAILED,
        graph_id=team.plan.graph_id,
        workflow_name=team.plan.name,
        error="The coordinator stopped.",
        extras={"lifecycle_warning": {"error": "Temporary worktree remains."}},
    )

    assert card.finish(result) is True
    assert card.occurrence_status(first.node_id) == "Done"
    assert card.occurrence_status(second.node_id) == "Didn’t finish"
    assert card._notice_label.text() == (
        "The coordinator stopped. Cleanup warning: Temporary worktree remains."
    )


@pytest.mark.parametrize(
    ("status", "header"),
    [
        (WorkflowRunStatus.COMPLETED, "Agent completed"),
        (WorkflowRunStatus.PARTIAL, "Agent finished with issues"),
        (WorkflowRunStatus.FAILED, "Agent couldn’t finish"),
        (WorkflowRunStatus.CANCELLED, "Agent stopped"),
    ],
)
def test_one_occurrence_uses_agent_wording_without_team_ceremony(
    qapp, status: WorkflowRunStatus, header: str
) -> None:
    team = _single_agent_team()
    card = AgentTeamCard(team)

    assert card.is_agent_run is True
    assert card._header_label.text() == "Aura used an Agent"
    assert card._name_label.text() == "Dependency Reviewer"
    assert card._meta_label is None
    initial_copy = " ".join(label.text() for label in card.findChildren(QLabel))
    assert "team" not in initial_copy.lower()
    assert "1 step" not in initial_copy
    result = WorkflowRunResult(
        status=status,
        graph_id=team.plan.graph_id,
        workflow_name=team.plan.name,
        steps=(
            (_completed(team.plan.steps[0]),)
            if status is WorkflowRunStatus.COMPLETED
            else ()
        ),
        error="The Agent stopped." if status is not WorkflowRunStatus.COMPLETED else "",
    )
    # Flatten the conditional tuple used above for the successful receipt.
    if result.steps and isinstance(result.steps[0], tuple):
        result = replace(result, steps=result.steps[0])
    assert card.finish(result) is True
    assert card._header_label.text() == header
    assert "Agent details" in card._details._toggle.text()
    card.settle_root_turn()
    assert card.save_agent_ids == (team.generated_definitions[0].agent_id,)
    assert card.can_keep_team is False


def test_card_actions_map_to_exact_team_retry_errors_and_ignore_stale_cards(qapp) -> None:
    bridge = _FakeBridge()
    retention = _Retention()
    driver = AuraPhaseDriver(qapp)
    chat = ChatView(driver)
    chat.begin_assistant()
    controller = AgentTeamChatController(
        bridge=bridge,
        chat=chat,
        retention_owner=retention,
    )
    team = _compiled_team()
    result = WorkflowRunResult(
        status=WorkflowRunStatus.CANCELLED,
        graph_id=team.plan.graph_id,
        workflow_name=team.plan.name,
        error="Stopped by user.",
    )

    bridge.agentTeamAccepted.emit(team)
    bridge.agentTeamFinished.emit(result)
    qapp.processEvents()
    card = controller.cards[0]
    assert card.retention_actions_visible is False

    bridge.finished.emit()
    qapp.processEvents()
    assert card.retention_actions_visible is True

    agent_id = card.save_agent_ids[0]
    retention.error = "Agent id collision; nothing was overwritten."
    card._save_buttons[agent_id].click()
    assert retention.saved == []
    assert not card._save_buttons[agent_id].isHidden()
    assert "collision" in card._retention_error.text()

    retention.error = ""
    card._save_buttons[agent_id].click()
    assert retention.saved == [(team, agent_id)]
    assert card._save_buttons[agent_id].isHidden()
    assert not card._save_statuses[agent_id].isHidden()

    card._keep_button.click()
    assert retention.kept == [team]
    assert card._keep_button.isHidden()
    assert not card._keep_status.isHidden()

    stale_keep = card._keep_button
    chat.reset()
    stale_keep.click()
    assert retention.kept == [team]


def test_root_failure_alone_does_not_reveal_actions_until_team_settles(qapp) -> None:
    bridge = _FakeBridge()
    driver = AuraPhaseDriver(qapp)
    chat = ChatView(driver)
    chat.begin_assistant()
    controller = AgentTeamChatController(
        bridge=bridge, chat=chat, retention_owner=_Retention()
    )
    team = _compiled_team()
    bridge.agentTeamAccepted.emit(team)
    bridge.finished.emit()
    qapp.processEvents()

    card = controller.cards[0]
    assert card.retention_actions_visible is False
    bridge.agentTeamFinished.emit(
        WorkflowRunResult(
            status=WorkflowRunStatus.FAILED,
            graph_id=team.plan.graph_id,
            workflow_name=team.plan.name,
            error="API failure.",
        )
    )
    qapp.processEvents()
    assert card.retention_actions_visible is True


class _FakeBridge(QObject):
    agentTeamAccepted = Signal(object)
    agentTeamStepChanged = Signal(str, str, str)
    agentTeamFinished = Signal(object)
    finished = Signal()


class _Retention:
    def __init__(self) -> None:
        self.saved: list[tuple[CompiledAgentTeam, str]] = []
        self.kept: list[CompiledAgentTeam] = []
        self.error = ""

    def retain_generated_agent(self, team, agent_id):
        if self.error:
            raise AgentRetentionError(self.error)
        self.saved.append((team, agent_id))
        return AgentRetentionResult("Saved", agent_ids=(agent_id,))

    def retain_generated_team(self, team):
        if self.error:
            raise AgentRetentionError(self.error)
        self.kept.append(team)
        return AgentRetentionResult("Kept", workflow_id=team.plan.graph_id)


def test_controller_keeps_card_session_local_and_ignores_stale_events(qapp) -> None:
    bridge = _FakeBridge()
    driver = AuraPhaseDriver(qapp)
    chat = ChatView(driver)
    chat.set_compact_tools(True)
    chat.begin_assistant()
    controller = AgentTeamChatController(bridge=bridge, chat=chat)
    team = _compiled_team()
    first = team.plan.steps[0]

    bridge.agentTeamAccepted.emit(team)
    bridge.agentTeamStepChanged.emit(team.plan.graph_id, first.node_id, "running")
    bridge.agentTeamStepChanged.emit("stale-graph", first.node_id, "failed")
    qapp.processEvents()

    assert len(controller.cards) == 1
    card = controller.cards[0]
    assert card.compiled_team is team
    assert card.occurrence_status(first.node_id) == "Working"
    assert chat.chat_items == []

    terminal = WorkflowRunResult(
        status=WorkflowRunStatus.FAILED,
        graph_id=team.plan.graph_id,
        workflow_name=team.plan.name,
        failure_class="delegation_busy",
        error="Another workflow is already running.",
    )
    bridge.agentTeamFinished.emit(terminal)
    qapp.processEvents()
    assert card.result is terminal
    assert card._status_chip.text() == "FAILED"

    # The dedicated card wholly replaces the false "Reading files" counter.
    assistant = chat.current_assistant()
    chat.add_tool_call("team-tool", "run_agent_team")
    chat.set_tool_result("team-tool", True, "{}")
    assert assistant._compact_tool_active == 0
    assert assistant._compact_tool_names == []

    # The exception is deliberately narrow; an ordinary compact tool keeps
    # the existing status behavior.
    chat.add_tool_call("read-tool", "read_file")
    assert assistant._compact_tool_active == 1
    chat.set_tool_result("read-tool", True, "{}")
    assert assistant._compact_tool_active == 0
    assert assistant._compact_tool_names == ["read_file"]

    chat.reset()
    assert controller.cards == ()


def test_team_card_is_between_preamble_and_later_aura_result(qapp) -> None:
    bridge = _FakeBridge()
    driver = AuraPhaseDriver(qapp)
    chat = ChatView(driver)
    assistant = chat.begin_assistant()
    controller = AgentTeamChatController(bridge=bridge, chat=chat)
    chat.append_content("I’ll assemble the right specialists.\n\n")
    preamble_label = assistant._content_label

    bridge.agentTeamAccepted.emit(_compiled_team())
    qapp.processEvents()
    card = controller.cards[0]
    result_label = assistant._content_label
    chat.append_content("Aura Result: the dependency is repaired.")

    assert preamble_label is not result_label
    assert assistant._outer.indexOf(preamble_label) < assistant._outer.indexOf(card)
    assert assistant._outer.indexOf(card) < assistant._outer.indexOf(result_label)
    assert result_label.text_buffer() == "Aura Result: the dependency is repaired."

    chat.assistant_done()
    assert len(chat.chat_items) == 1
    assert chat.chat_items[0]["text"] == (
        "I’ll assemble the right specialists.\n\n"
        "Aura Result: the dependency is repaired."
    )
    assert controller.cards == (card,)


def test_team_card_follows_fenced_preamble_and_precedes_result_stream(qapp) -> None:
    bridge = _FakeBridge()
    driver = AuraPhaseDriver(qapp)
    chat = ChatView(driver)
    assistant = chat.begin_assistant()
    chat.append_content("Here is the probe:\n\n```python\nprint('ready')\n```\n")
    stream_label = assistant._content_label
    before_count = assistant._outer.count()
    controller = AgentTeamChatController(bridge=bridge, chat=chat)

    bridge.agentTeamAccepted.emit(_compiled_team())
    qapp.processEvents()
    card = controller.cards[0]

    # Fenced Markdown becomes a static rich container and leaves the existing
    # stream label empty for the later root answer.
    assert assistant._content_label is stream_label
    assert stream_label.text_buffer() == ""
    assert assistant._outer.count() == before_count + 2
    assert assistant._outer.indexOf(card) < assistant._outer.indexOf(stream_label)
    rich_container = assistant._outer.itemAt(
        assistant._outer.indexOf(card) - 1
    ).widget()
    assert rich_container is not None
    assert rich_container is not stream_label

    chat.append_content("Aura Result follows the team.")
    assert stream_label.text_buffer() == "Aura Result follows the team."


def test_conversation_runner_tags_exact_team_objects_with_generation(qapp) -> None:
    team = _compiled_team()
    first = team.plan.steps[0]
    result = WorkflowRunResult(
        status=WorkflowRunStatus.FAILED,
        graph_id=team.plan.graph_id,
        workflow_name=team.plan.name,
        failure_class="delegation_busy",
        error="Another workflow is already running.",
    )
    runner = _ConversationRunner(
        manager=object(),
        approval_proxy=object(),
        cancel_event=threading.Event(),
        model="deepseek-chat",
        thinking="off",
        agent_team_generation=23,
    )
    events: list[tuple] = []
    runner.agentTeamAccepted.connect(
        lambda generation, value: events.append(("accepted", generation, value))
    )
    runner.agentTeamStepChanged.connect(
        lambda *values: events.append(("step", *values))
    )
    runner.agentTeamFinished.connect(
        lambda generation, value: events.append(("finished", generation, value))
    )

    runner.team_accepted(team)
    runner.step_changed(team.plan.graph_id, first.node_id, WorkflowStepState.RUNNING)
    runner.team_finished(result)

    assert events == [
        ("accepted", 23, team),
        ("step", 23, team.plan.graph_id, first.node_id, "running"),
        ("finished", 23, result),
    ]


def test_bridge_drops_queued_team_facts_after_conversation_reset(qapp) -> None:
    bridge = ConversationBridge(parent_widget=None, provider="test")
    team = _compiled_team()
    first = team.plan.steps[0]
    result = WorkflowRunResult.failure(
        team.plan.graph_id,
        DelegationFailure.DELEGATION_BUSY,
        "Another workflow is already running.",
        workflow_name=team.plan.name,
    )
    generation = bridge._agent_team_generation
    runner = _ConversationRunner(
        manager=object(),
        approval_proxy=object(),
        cancel_event=threading.Event(),
        model="deepseek-chat",
        thinking="off",
        agent_team_generation=generation,
    )
    runner.agentTeamAccepted.connect(
        bridge._on_agent_team_accepted,
        Qt.ConnectionType.QueuedConnection,
    )
    runner.agentTeamStepChanged.connect(
        bridge._on_agent_team_step_changed,
        Qt.ConnectionType.QueuedConnection,
    )
    runner.agentTeamFinished.connect(
        bridge._on_agent_team_finished,
        Qt.ConnectionType.QueuedConnection,
    )
    public_events: list[str] = []
    bridge.agentTeamAccepted.connect(lambda _team: public_events.append("accepted"))
    bridge.agentTeamStepChanged.connect(lambda *_args: public_events.append("step"))
    bridge.agentTeamFinished.connect(lambda _result: public_events.append("finished"))

    runner.team_accepted(team)
    runner.step_changed(team.plan.graph_id, first.node_id, WorkflowStepState.RUNNING)
    runner.team_finished(result)
    bridge.reset_history()
    qapp.processEvents()

    assert bridge._agent_team_generation != generation
    assert public_events == []
    assert bridge.registry._agent_team_run_observer is None
    bridge.shutdown()


def test_request_cancel_keeps_current_team_generation_settleable(qapp) -> None:
    bridge = ConversationBridge(parent_widget=None, provider="test")
    team = _compiled_team()
    result = WorkflowRunResult(
        status=WorkflowRunStatus.CANCELLED,
        graph_id=team.plan.graph_id,
        workflow_name=team.plan.name,
    )
    generation = bridge._agent_team_generation
    finished: list[object] = []
    bridge.agentTeamFinished.connect(finished.append)

    bridge.request_cancel()
    bridge._on_agent_team_finished(generation, result)

    assert bridge._agent_team_generation == generation
    assert finished == [result]
    bridge.shutdown()


class _BridgeWorkflowRunner:
    def __init__(self) -> None:
        self.plan = None
        self.result = None
        self.thread_id: int | None = None

    def run(self, plan, task, *, cancel_event=None, on_step=None):
        self.plan = plan
        self.thread_id = threading.get_ident()
        step = plan.steps[0]
        if on_step is not None:
            on_step(step.node_id, WorkflowStepState.RUNNING)
            on_step(step.node_id, WorkflowStepState.SUCCEEDED)
        self.result = WorkflowRunResult(
            status=WorkflowRunStatus.COMPLETED,
            graph_id=plan.graph_id,
            workflow_name=plan.name,
            result="dependency verified",
            steps=(_completed(step),),
        )
        return self.result


class _BridgeTeamCapture(QObject):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[tuple] = []

    @Slot(object)
    def accepted(self, team: object) -> None:
        self.events.append(("accepted", team, threading.get_ident()))

    @Slot(str, str, str)
    def step(self, graph_id: str, node_id: str, state: str) -> None:
        self.events.append(
            ("step", graph_id, node_id, state, threading.get_ident())
        )

    @Slot(object)
    def finished(self, result: object) -> None:
        self.events.append(("finished", result, threading.get_ident()))


def _wait_for_bridge_finished(bridge: ConversationBridge, qapp) -> None:
    finished: list[bool] = []
    bridge.finished.connect(lambda: finished.append(True))
    deadline = time.monotonic() + 5
    while not finished and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    assert finished, "ConversationBridge did not finish within the test timeout"
    qapp.processEvents()


@pytest.mark.parametrize("read_only_turn", [False, True])
def test_real_bridge_turn_projects_team_facts_on_gui_thread_and_cleans_up(
    qapp, tmp_path, read_only_turn: bool
) -> None:
    bridge = ConversationBridge(parent_widget=None, provider="test")
    bridge.set_workspace_root(tmp_path)
    bridge.set_read_only(read_only_turn)
    bridge.set_submitted_agent_context(
        AgentTurnContext.enabled(
            root_provider="deepseek",
            root_model="deepseek-chat",
            root_thinking="high",
        )
    )
    bridge.history.append_user_text("Check the dependency with a focused team.")
    workflow_runner = _BridgeWorkflowRunner()
    bridge.registry.set_agent_workflow_runner(workflow_runner)
    capture = _BridgeTeamCapture()
    bridge.agentTeamAccepted.connect(capture.accepted)
    bridge.agentTeamStepChanged.connect(capture.step)
    bridge.agentTeamFinished.connect(capture.finished)

    payload = _single_agent_payload()
    arguments = json.dumps(payload)
    catalogs: list[set[str]] = []

    def stream(**kwargs):
        round_index = len(catalogs)
        catalogs.append(
            {
                tool["function"]["name"]
                for tool in kwargs.get("tools", [])
                if isinstance(tool, dict) and isinstance(tool.get("function"), dict)
            }
        )
        if round_index == 0:
            yield ToolCallStart(index=0, id="team-1", name="run_agent_team")
            yield ToolCallArgsDelta(index=0, args_chunk=arguments)
            yield ToolCallEnd(index=0)
            yield Done(
                finish_reason="tool_calls",
                full_message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "team-1",
                            "type": "function",
                            "function": {
                                "name": "run_agent_team",
                                "arguments": arguments,
                            },
                        }
                    ],
                },
            )
            return
        yield ContentDelta(text="Aura Result: the dependency is verified.")
        yield Done(
            finish_reason="stop",
            full_message={
                "role": "assistant",
                "content": "Aura Result: the dependency is verified.",
            },
        )

    previous = model_streams.get_handler(PRODUCTION_STREAM_HOOK)
    model_streams.unregister(PRODUCTION_STREAM_HOOK)
    model_streams.register(PRODUCTION_STREAM_HOOK, stream)
    main_thread_id = threading.get_ident()
    observer_after_finish = object()
    turn_mode_after_finish = None
    try:
        bridge.send(model="test-model", thinking="off")
        _wait_for_bridge_finished(bridge, qapp)
        observer_after_finish = bridge.registry._agent_team_run_observer
        turn_mode_after_finish = bridge.registry.turn_agent_context.mode
    finally:
        model_streams.unregister(PRODUCTION_STREAM_HOOK)
        if previous is not None:
            model_streams.register(PRODUCTION_STREAM_HOOK, previous)
        bridge.shutdown()

    assert "run_agent_team" in catalogs[0]
    assert [event[0] for event in capture.events] == [
        "accepted",
        "step",
        "step",
        "finished",
    ]
    accepted = capture.events[0][1]
    assert accepted.plan is workflow_runner.plan
    assert capture.events[1][1:4] == (
        accepted.plan.graph_id,
        accepted.plan.steps[0].node_id,
        "running",
    )
    assert capture.events[2][1:4] == (
        accepted.plan.graph_id,
        accepted.plan.steps[0].node_id,
        "succeeded",
    )
    assert capture.events[3][1] is workflow_runner.result
    assert workflow_runner.thread_id != main_thread_id
    assert all(event[-1] == main_thread_id for event in capture.events)
    assert observer_after_finish is None
    assert turn_mode_after_finish is AgentTurnMode.OFF
