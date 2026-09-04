"""Submission-time Agent authority and root-turn capability policy."""
from __future__ import annotations

import json
import threading
from dataclasses import replace
from pathlib import Path

import pytest

pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtWidgets import QApplication  # noqa: E402

from aura.agents.delegation import DelegationResult, DelegationStatus  # noqa: E402
from aura.agents.identity import AgentScope  # noqa: E402
from aura.agents.local_state import AgentLocalState, AgentPermission  # noqa: E402
from aura.agents.models import AgentDefinition  # noqa: E402
from aura.agents.roster import AgentRosterEntry, AgentTurnRoster  # noqa: E402
from aura.agents.store import AgentStore  # noqa: E402
from aura.agents.turn_context import AgentTurnContext  # noqa: E402
from aura.context_gearbox.runtime import compose_system_prompt  # noqa: E402
from aura.conversation.history import History  # noqa: E402
from aura.conversation.tools._types import ApprovalDecision  # noqa: E402
from aura.conversation.tools.effects import BUILTIN_TOOL_EFFECTS, ToolEffect  # noqa: E402
from aura.conversation.tools.registry import ToolRegistry  # noqa: E402
from aura.gui.input_panel import SendPayload  # noqa: E402
from aura.gui.send_handler import SendHandler  # noqa: E402


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


class _Input:
    def __init__(self) -> None:
        self.queued = 0

    def set_queued_messages(self, count: int) -> None:
        self.queued = count


class _Chat:
    def add_user(self, *_args, **_kwargs) -> None:
        pass

    def scroll_to_bottom(self, **_kwargs) -> None:
        pass

    def begin_assistant(self) -> None:
        pass

    def add_error(self, *_args, **_kwargs) -> None:
        pass


class _Bridge:
    def __init__(self) -> None:
        self.history = History()
        self.running = True
        self.agent_contexts: list[AgentTurnContext] = []
        self.sends: list[tuple[str, str]] = []

    def is_running(self) -> bool:
        return self.running

    def authorize_external_reads(self, paths):
        return tuple(paths or ())

    def set_turn_target_files(self, _paths) -> None:
        pass

    def set_submitted_agent_context(self, context: AgentTurnContext) -> None:
        self.agent_contexts.append(context)

    def send(self, *, model, thinking) -> None:
        self.sends.append((model, thinking))


def _definition(
    *, name: str = "Original", permission: AgentPermission = AgentPermission.READ_ONLY
) -> AgentRosterEntry:
    return AgentRosterEntry(
        AgentDefinition(
            agent_id="reviewer000",
            scope=AgentScope.PROJECT,
            name=name,
            description="Reviews one focused change.",
            instructions="PRIVATE CHILD INSTRUCTIONS",
        ),
        permission=permission,
    )


def test_queued_send_freezes_definition_and_effective_grant_at_submission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, qapp
) -> None:
    monkeypatch.setenv("AURA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(
        "aura.gui.send_handler.has_usable_provider_configuration",
        lambda _provider: True,
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = AgentStore(workspace)
    created = store.create(
        AgentScope.PROJECT,
        name="Original",
        description="Reviews one focused change.",
        instructions="ORIGINAL PRIVATE INSTRUCTIONS",
    )
    state = AgentLocalState(workspace)
    state.set_available(created.agent_id, True)
    bridge = _Bridge()
    handler = SendHandler(
        bridge=bridge,
        chat=_Chat(),
        input_panel=_Input(),
        settings=type("Settings", (), {"provider": "deepseek"})(),
        workspace_root=workspace,
        agent_context_provider=lambda **_kwargs: AgentTurnContext.enabled(
            roster=AgentTurnRoster(
                entries=(
                    AgentRosterEntry(
                        store.get(created.agent_id),
                        permission=state.permission(created.agent_id),
                    ),
                )
            ),
            root_provider="deepseek",
            root_model="model-a",
            root_thinking="off",
        ),
    )

    handler.handle_send(SendPayload("Review this.", []), "model-a", "off")
    queued = handler._message_queue[0]
    assert queued.agent_context.roster.entries[0].name == "Original"
    assert (
        queued.agent_context.roster.entries[0].permission
        is AgentPermission.READ_ONLY
    )

    store.update(replace(created, name="Changed later", instructions="CHANGED PRIVATE"))
    state.set_permission(created.agent_id, AgentPermission.READ_WRITE)
    bridge.running = False
    handler.process_message_queue("ignored", "high")

    frozen = bridge.agent_contexts[0].roster.entries[0]
    assert frozen.name == "Original"
    assert frozen.permission is AgentPermission.READ_ONLY
    assert bridge.agent_contexts[0].root_provider == "deepseek"
    assert bridge.agent_contexts[0].root_model == "model-a"
    assert bridge.agent_contexts[0].root_thinking == "off"
    assert bridge.sends == [("model-a", "off")]
    canonical = json.dumps(bridge.history.messages)
    assert "ORIGINAL PRIVATE INSTRUCTIONS" not in canonical
    assert "CHANGED PRIVATE" not in canonical
    assert bridge.history.latest_real_user_available_agent_ids() == (created.agent_id,)


def test_schema_is_the_only_model_facing_roster_projection(tmp_path: Path) -> None:
    roster = AgentTurnRoster(
        entries=(_definition(permission=AgentPermission.READ_WRITE),)
    )
    registry = ToolRegistry(tmp_path)
    registry.set_turn_agent_roster(roster)
    schema = json.dumps(
        next(
            tool
            for tool in registry.tool_defs()
            if tool["function"]["name"] == "delegate_agent"
        )
    )
    prompt = compose_system_prompt(tmp_path).system_prompt

    assert "reviewer000" in schema
    assert "Original" in schema
    assert "Reviews one focused change." in schema
    assert AgentPermission.READ_WRITE.label in schema
    assert "PRIVATE CHILD INSTRUCTIONS" not in schema
    assert "reviewer000" not in prompt
    assert "Reviews one focused change." not in prompt


def test_no_agents_and_no_retained_work_add_zero_agent_tools(tmp_path: Path) -> None:
    names = [tool["function"]["name"] for tool in ToolRegistry(tmp_path).tool_defs()]
    assert not any("agent" in name for name in names)
    assert "delegate_agent" not in names


class _Runner:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, entry, task, *, cancel_event):
        self.calls += 1
        return DelegationResult(
            status=DelegationStatus.COMPLETED,
            agent_id=entry.agent_id,
            agent_name=entry.name,
            result="Research complete.",
        )


@pytest.mark.parametrize("read_only", [False, True])
def test_read_only_research_delegation_is_allowed_during_restricted_turns(
    tmp_path: Path, read_only: bool
) -> None:
    runner = _Runner()
    registry = ToolRegistry(tmp_path, read_only=read_only)
    registry.set_turn_agent_roster(AgentTurnRoster(entries=(_definition(),)))
    registry.set_agent_delegation_runner(runner)
    registry.plan_review.begin_turn(required=True)

    result = registry.execute(
        name="delegate_agent",
        args={"agent_id": "reviewer000", "task": "Research this."},
        approval_cb=lambda _request: ApprovalDecision(action="approve"),
        cancel_event=threading.Event(),
    )

    assert result.ok is True
    assert result.payload["result"] == "Research complete."
    assert runner.calls == 1


@pytest.mark.parametrize("read_only", [False, True])
def test_writable_delegation_is_refused_not_downgraded_when_root_forbids_mutation(
    tmp_path: Path, read_only: bool
) -> None:
    runner = _Runner()
    registry = ToolRegistry(tmp_path, read_only=read_only)
    roster = AgentTurnRoster(
        entries=(_definition(permission=AgentPermission.READ_WRITE),)
    )
    registry.set_turn_agent_roster(roster)
    registry.set_agent_delegation_runner(runner)
    registry.plan_review.begin_turn(required=True)

    result = registry.execute(
        name="delegate_agent",
        args={"agent_id": "reviewer000", "task": "Edit this."},
        approval_cb=lambda _request: ApprovalDecision(action="approve"),
        cancel_event=threading.Event(),
    )

    assert result.ok is False
    assert result.payload["failure_class"] == "root_mutation_forbidden"
    assert result.payload.get("permission") != AgentPermission.READ_ONLY.value
    assert runner.calls == 0


def test_delegate_agent_is_serial_bookkeeping_not_a_shell_command() -> None:
    assert BUILTIN_TOOL_EFFECTS["delegate_agent"] is ToolEffect.BOOKKEEPING
