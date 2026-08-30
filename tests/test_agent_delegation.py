"""The first delegation path: one root, one read-only child, one result.

What this file holds Aura to:

* an empty roster changes nothing — no tool, no prompt block, no metadata on
  the wire, and the same catalog single-agent Aura has always sent;
* the root is told an agent's id, name, and one-line description, and never
  its instructions;
* the child starts from nothing — a private History, its own brief, and a
  frozen read/search/read-only-Git catalog it cannot step outside of;
* it cannot delegate again, cannot write, and cannot reach a skill, an MCP
  tool, or the root's transcript;
* provider and model are either fully inherited or fully named, and a target
  that cannot be resolved fails the *delegation*, not the conversation;
* the user's Stop stops the child, through the root turn's own event;
* exactly one paired tool result reaches canonical root History, and it is
  the structured result — never the child's messages.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from aura.agents.child_prompt import compose_child_system_prompt
from aura.agents.delegation import DelegationFailure, DelegationResult, DelegationStatus
from aura.agents.identity import AgentScope
from aura.agents.local_state import AgentLocalState, AgentPermission
from aura.agents.models import AgentDefinition, AgentThinking, ModelTarget
from aura.agents.prompt import format_agent_roster_block
from aura.agents.roster import (
    AgentRosterEntry,
    AgentTurnRoster,
    resolve_agent_turn_roster,
)
from aura.agents.runtime import AgentDelegationRunner, resolve_model_target
from aura.agents.store import AgentStore
from aura.client import ApiError, ContentDelta, Done, Event, ToolResult, Usage
from aura.conversation.history import AVAILABLE_AGENT_IDS_KEY, History
from aura.conversation.manager_tool_round import ToolRoundRunner
from aura.conversation.tool_runner import ToolRunner
from aura.conversation.tools.catalog import ToolCatalog, child_agent_tool_defs
from aura.conversation.tools.effects import BUILTIN_TOOL_EFFECTS, ToolEffect
from aura.conversation.tools.registry import ToolRegistry

INSTRUCTIONS = "SECRET-CHILD-BRIEF: read the diff and report only what you can show."


# ── fixtures and doubles ─────────────────────────────────────────────────────


def _definition(
    agent_id: str = "reviewer000",
    *,
    name: str = "Reviewer",
    description: str = "Reviews a change for defects.",
    instructions: str = INSTRUCTIONS,
    target: ModelTarget | None = None,
    thinking: AgentThinking = AgentThinking.INHERIT,
) -> AgentDefinition:
    return AgentDefinition(
        agent_id=agent_id,
        scope=AgentScope.PROJECT,
        name=name,
        description=description,
        instructions=instructions,
        target=target or ModelTarget.inherited(),
        thinking=thinking,
    )


def _entry(definition: AgentDefinition | None = None) -> AgentRosterEntry:
    return AgentRosterEntry(definition=definition or _definition())


def _roster(*definitions: AgentDefinition) -> AgentTurnRoster:
    return AgentTurnRoster(
        entries=tuple(AgentRosterEntry(definition=d) for d in (definitions or (_definition(),)))
    )


def _call(call_id: str, name: str, **args: Any) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def _answer(text: str) -> list[Event]:
    return [
        ContentDelta(text=text),
        Done(finish_reason="stop", full_message={"role": "assistant", "content": text}),
    ]


class _ScriptedBackend:
    """A backend whose stream is a list of pre-written rounds."""

    def __init__(self, rounds: list[list[Event]]) -> None:
        self._rounds = rounds
        self.requests: list[dict[str, Any]] = []

    def stream(self, **kwargs: Any):
        self.requests.append(kwargs)
        index = len(self.requests) - 1
        events = self._rounds[index] if index < len(self._rounds) else []
        yield from events


def _runner(
    tmp_path: Path,
    rounds: list[list[Event]],
    *,
    provider: str = "deepseek",
    model: str = "deepseek-chat",
    thinking: str = "off",
) -> tuple[AgentDelegationRunner, _ScriptedBackend]:
    backend = _ScriptedBackend(rounds)
    runner = AgentDelegationRunner(
        workspace_root=tmp_path,
        inherited_provider=provider,
        inherited_model=model,
        inherited_thinking=thinking,
        backend_factory=lambda _provider: backend,
    )
    return runner, backend


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "note.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    return root


@pytest.fixture(autouse=True)
def _configured_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test runs as if the hosted provider is configured.

    The one test about an unconfigured provider overrides this itself.
    """
    monkeypatch.setattr(
        "aura.config.has_usable_provider_configuration", lambda provider=None: True
    )


# ── an empty roster changes nothing ──────────────────────────────────────────


def test_no_agents_means_no_delegation_tool(workspace: Path) -> None:
    registry = ToolRegistry(workspace_root=workspace)

    names = [tool["function"]["name"] for tool in registry.tool_defs()]

    assert "delegate_agent" not in names
    assert names == ["read_file", "grep_search", "update_task_checklist", "apply_patch", "shell"]


def test_no_agents_means_no_agent_block_in_the_prompt() -> None:
    assert format_agent_roster_block(()) == ""
    assert format_agent_roster_block(({"agent_id": "", "name": "x"},)) == ""


def test_an_empty_roster_leaves_the_catalog_byte_for_byte_unchanged() -> None:
    catalog = ToolCatalog()

    assert catalog.build_tool_defs(read_only=False, agents=None) == catalog.build_tool_defs(
        read_only=False
    )
    assert catalog.build_tool_defs(read_only=True, agents=()) == catalog.build_tool_defs(
        read_only=True
    )


def test_a_turn_with_no_roster_puts_no_agent_metadata_on_the_wire() -> None:
    history = History()
    history.append_user_text("do the thing")

    assert AVAILABLE_AGENT_IDS_KEY not in history.messages[0]
    assert history.for_api() == [{"role": "user", "content": "do the thing"}]


# ── what the root is told ────────────────────────────────────────────────────


def test_the_root_sees_only_id_name_and_description(workspace: Path) -> None:
    registry = ToolRegistry(workspace_root=workspace)
    registry.set_turn_agent_roster(_roster())

    tool = next(
        t for t in registry.tool_defs() if t["function"]["name"] == "delegate_agent"
    )
    rendered = json.dumps(tool)

    assert "reviewer000" in rendered
    assert "Reviewer" in rendered
    assert "Reviews a change for defects." in rendered
    assert INSTRUCTIONS not in rendered
    assert "SECRET-CHILD-BRIEF" not in rendered


def test_the_prompt_block_carries_the_roster_but_not_the_briefs() -> None:
    block = format_agent_roster_block(_roster().catalog_rows())

    assert "reviewer000" in block
    assert "Reviewer" in block
    assert "SECRET-CHILD-BRIEF" not in block


def test_the_prompt_block_states_the_grant_it_actually_froze() -> None:
    """The block may not make a blanket claim the frozen grant contradicts.

    A writable agent's row and Aura's guidance are read together by the root
    model; guidance that says every agent "cannot edit anything" would tell it
    the opposite of what `delegate_agent` and the change-set tools offer.
    """
    writable = AgentTurnRoster(
        entries=(
            AgentRosterEntry(
                definition=_definition(),
                permission=AgentPermission.WORKTREE_EDIT,
            ),
        )
    )

    block = format_agent_roster_block(writable.catalog_rows())

    assert AgentPermission.WORKTREE_EDIT.label in block
    assert "cannot edit anything" not in block
    # Read-only remains stated as a per-agent grant, never as a global fact.
    read_only_block = format_agent_roster_block(_roster().catalog_rows())
    assert AgentPermission.READ_ONLY.label in read_only_block


def test_the_roster_keeps_the_users_own_order(workspace: Path) -> None:
    store = AgentStore(workspace, personal_dir=workspace / "personal")
    state = AgentLocalState(workspace, state_root=workspace / "state")
    first = store.create(
        AgentScope.PROJECT, name="Alpha", description="a", instructions="a"
    )
    second = store.create(
        AgentScope.PROJECT, name="Beta", description="b", instructions="b"
    )
    state.set_permission(second.agent_id, AgentPermission.WORKTREE_EDIT)

    roster = resolve_agent_turn_roster(
        (second.agent_id, first.agent_id), definitions=store, permissions=state
    )

    assert roster.ids == (second.agent_id, first.agent_id)
    assert roster.entries[0].permission is AgentPermission.WORKTREE_EDIT
    assert roster.entries[1].permission is AgentPermission.READ_ONLY


def test_an_unresolvable_id_is_simply_not_on_the_roster(workspace: Path) -> None:
    store = AgentStore(workspace, personal_dir=workspace / "personal")
    state = AgentLocalState(workspace, state_root=workspace / "state")
    real = store.create(AgentScope.PROJECT, name="Alpha", description="a", instructions="a")

    roster = resolve_agent_turn_roster(
        ("gone000000", real.agent_id, real.agent_id),
        definitions=store,
        permissions=state,
    )

    assert roster.ids == (real.agent_id,)


# ── the frozen ids ride on the user message and never reach a provider ───────


def test_the_frozen_ids_ride_on_the_user_message_and_are_stripped_for_the_api() -> None:
    history = History()
    history.append_user_text("review this", available_agent_ids=("a1", "a2"))

    assert history.messages[0][AVAILABLE_AGENT_IDS_KEY] == ["a1", "a2"]
    assert history.latest_real_user_available_agent_ids() == ("a1", "a2")
    assert all(AVAILABLE_AGENT_IDS_KEY not in msg for msg in history.for_api())


def test_the_ids_survive_a_follow_up_turn_and_a_retry() -> None:
    history = History()
    history.append_user_text("first", available_agent_ids=("a1",))
    history.append_assistant({"role": "assistant", "content": "done"})
    history.append_user_text("second", available_agent_ids=("a2",))
    history.append_assistant({"role": "assistant", "content": "done again"})

    assert history.latest_real_user_available_agent_ids() == ("a2",)

    history.rewind_to_last_user_turn()

    assert history.latest_real_user_available_agent_ids() == ("a2",)


def test_a_multimodal_turn_carries_the_ids_too() -> None:
    history = History()
    history.append_user_multimodal(
        [{"type": "text", "text": "look"}], available_agent_ids=("a1",)
    )

    assert history.latest_real_user_available_agent_ids() == ("a1",)
    assert all(AVAILABLE_AGENT_IDS_KEY not in msg for msg in history.for_api())


# ── the child's context is its own ───────────────────────────────────────────


def test_the_child_starts_from_a_private_history_holding_only_its_task(
    workspace: Path,
) -> None:
    runner, backend = _runner(workspace, [_answer("found two defects")])

    result = runner.run(_entry(), "Review the diff in note.txt.")

    assert result.status is DelegationStatus.COMPLETED
    messages = backend.requests[0]["messages"]
    assert [m["role"] for m in messages] == ["system", "user"]
    assert messages[1]["content"] == "Review the diff in note.txt."
    assert INSTRUCTIONS in messages[0]["content"]


def test_the_child_never_receives_the_root_conversation(workspace: Path) -> None:
    runner, backend = _runner(workspace, [_answer("ok")])

    runner.run(_entry(), "look at note.txt")

    rendered = json.dumps(backend.requests[0]["messages"])
    for leaked in ("ROOT-SYSTEM-PROMPT", "ROOT-USER-TURN", "ROOT-TOOL-RESULT"):
        assert leaked not in rendered
    # Only the two messages the child was built with — no room for anything else.
    assert len(backend.requests[0]["messages"]) == 2


def test_the_child_prompt_carries_only_host_brief_and_workspace_facts(
    workspace: Path,
) -> None:
    prompt = compose_child_system_prompt(_definition(), workspace_root=workspace)

    assert INSTRUCTIONS in prompt
    assert str(workspace) in prompt
    assert "read-only" in prompt
    assert "cannot delegate" in prompt
    # No Skills, no context packs, no production prompt.
    assert "load_skills" not in prompt
    assert "### Skills" not in prompt


def test_a_second_run_starts_exactly_where_the_first_did(workspace: Path) -> None:
    runner, backend = _runner(workspace, [_answer("first"), _answer("second")])

    runner.run(_entry(), "same task")
    runner.run(_entry(), "same task")

    assert backend.requests[0]["messages"] == backend.requests[1]["messages"]


def test_the_childs_transcript_never_reaches_the_result(workspace: Path) -> None:
    rounds = [
        [
            Done(
                finish_reason="tool_calls",
                full_message={
                    "role": "assistant",
                    "content": "CHILD-PRIVATE-PLAN",
                    "reasoning_content": "CHILD-PRIVATE-REASONING",
                    "tool_calls": [_call("c1", "read_file", path="note.txt")],
                },
            )
        ],
        _answer("note.txt has two lines"),
    ]
    runner, _backend = _runner(workspace, rounds)

    result = runner.run(_entry(), "read note.txt")

    assert result.status is DelegationStatus.COMPLETED
    assert result.result == "note.txt has two lines"
    payload = json.dumps(result.payload())
    assert "CHILD-PRIVATE-PLAN" not in payload
    assert "CHILD-PRIVATE-REASONING" not in payload
    assert "tool_calls" not in payload


# ── the child's tool surface is fixed ────────────────────────────────────────


def test_the_child_catalog_is_read_search_and_read_only_git() -> None:
    names = [tool["function"]["name"] for tool in child_agent_tool_defs()]

    assert names == [
        "read_file",
        "glob",
        "grep_search",
        "git_status",
        "git_diff",
        "git_log",
        "git_show",
        "git_log_file",
        "git_branch_list",
        "git_stash_list",
        "git_stash_show",
    ]


def test_the_child_catalog_offers_nothing_that_changes_anything() -> None:
    names = {tool["function"]["name"] for tool in child_agent_tool_defs()}

    for forbidden in (
        "apply_patch",
        "shell",
        "delegate_agent",
        "load_skills",
        "read_skill_resource",
        "update_task_checklist",
        "save_to_project_memory",
        "review_implementation_plan",
    ):
        assert forbidden not in names
    for name in names:
        assert BUILTIN_TOOL_EFFECTS[name] is ToolEffect.OBSERVATION


def test_the_child_runs_a_read_but_a_call_outside_its_catalog_is_refused(
    workspace: Path,
) -> None:
    rounds = [
        [
            Done(
                finish_reason="tool_calls",
                full_message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        _call("c1", "read_file", path="note.txt"),
                        _call("c2", "apply_patch", operation="write"),
                    ],
                },
            )
        ],
        _answer("done"),
    ]
    runner, backend = _runner(workspace, rounds)

    result = runner.run(_entry(), "read and edit")

    assert result.status is DelegationStatus.COMPLETED
    # The whole batch was refused before execution: the second round replays
    # two rejection results and note.txt was never even read for the model.
    second_round = backend.requests[1]["messages"]
    tool_messages = [m for m in second_round if m["role"] == "tool"]
    assert len(tool_messages) == 2
    rendered = json.dumps(tool_messages)
    assert "not exposed in this request" in rendered
    assert (workspace / "note.txt").read_text(encoding="utf-8") == "alpha\nbeta\n"


def test_a_child_registry_cannot_delegate_even_if_asked(workspace: Path) -> None:
    # The production owner of the child's registry, not a stand-in: the runner
    # builds exactly one, and it is the thing that must be unable to delegate.
    runner = AgentDelegationRunner(workspace_root=workspace)
    registry = runner._child_registry(workspace, AgentPermission.READ_ONLY)

    assert registry.read_only is True
    assert registry.turn_agent_roster.is_empty
    result = registry.execute(
        name="delegate_agent",
        args={"agent_id": "reviewer000", "task": "go deeper"},
        approval_cb=lambda request: None,
    )

    assert result.ok is False
    assert result.payload["failure_class"] == DelegationFailure.AGENT_NOT_AVAILABLE.value


def test_no_workspace_refuses_instead_of_rooting_a_child_at_home() -> None:
    """A child is never given a root the user did not open.

    The root turn already resolves an empty roster without a workspace; this
    is the runner's own refusal, so no route can hand a child the user's home
    directory as its read surface.
    """
    backend = _ScriptedBackend([_answer("should never run")])
    runner = AgentDelegationRunner(
        workspace_root=None,
        inherited_provider="deepseek",
        inherited_model="deepseek-chat",
        backend_factory=lambda _provider: backend,
    )

    result = runner.run(_entry(), "look around")

    assert result.status is DelegationStatus.FAILED
    assert result.failure_class == DelegationFailure.WORKSPACE_REQUIRED.value
    assert backend.requests == []


def test_delegation_is_serialized_never_run_in_parallel() -> None:
    assert BUILTIN_TOOL_EFFECTS["delegate_agent"] is ToolEffect.COMMAND
    assert BUILTIN_TOOL_EFFECTS["delegate_agent"] is not ToolEffect.OBSERVATION


def test_a_concurrent_delegation_is_refused_rather_than_run(workspace: Path) -> None:
    entered = threading.Event()
    release = threading.Event()
    second: dict[str, DelegationResult] = {}

    runner, _backend = _runner(workspace, [_answer("first"), _answer("second")])
    original = runner._run_child

    def blocking(*args: Any, **kwargs: Any):
        entered.set()
        release.wait(timeout=5)
        return original(*args, **kwargs)

    runner._run_child = blocking  # type: ignore[method-assign]

    def other() -> None:
        entered.wait(timeout=5)
        second["result"] = runner.run(_entry(), "second task")
        release.set()

    thread = threading.Thread(target=other)
    thread.start()
    first = runner.run(_entry(), "first task")
    thread.join(timeout=5)

    assert first.status is DelegationStatus.COMPLETED
    assert second["result"].status is DelegationStatus.FAILED
    assert second["result"].failure_class == DelegationFailure.DELEGATION_BUSY.value


# ── provider and model resolution ────────────────────────────────────────────


def test_an_inheriting_agent_runs_the_turns_own_provider_and_model() -> None:
    resolved, failure, _message = resolve_model_target(
        ModelTarget.inherited(),
        AgentThinking.INHERIT,
        inherited_provider="anthropic",
        inherited_model="claude-x",
        inherited_thinking="high",
    )

    assert failure is None
    assert (resolved.provider, resolved.model, resolved.thinking) == (
        "anthropic",
        "claude-x",
        "high",
    )


def test_an_explicit_pair_overrides_inheritance_including_thinking() -> None:
    resolved, failure, _message = resolve_model_target(
        ModelTarget.explicit("openai", "gpt-x"),
        AgentThinking.MAX,
        inherited_provider="deepseek",
        inherited_model="deepseek-chat",
        inherited_thinking="off",
    )

    assert failure is None
    assert (resolved.provider, resolved.model, resolved.thinking) == (
        "openai",
        "gpt-x",
        "max",
    )


def test_every_hosted_api_provider_can_back_an_agent() -> None:
    from aura.providers.registry import provider_registry

    hosted = [
        pid for pid in provider_registry.ids() if provider_registry.get(pid).kind == "api_key"
    ]
    assert hosted, "there must be at least one hosted provider"
    for provider in hosted:
        resolved, failure, message = resolve_model_target(
            ModelTarget.explicit(provider, "some-model"),
            AgentThinking.INHERIT,
            inherited_provider="",
            inherited_model="",
            inherited_thinking="off",
        )
        assert failure is None, f"{provider}: {message}"
        assert resolved.provider == provider


@pytest.mark.parametrize(
    "target, expected",
    [
        (ModelTarget(provider="openai", model=""), DelegationFailure.MODEL_TARGET_INCOMPLETE),
        (ModelTarget(provider="", model="gpt-x"), DelegationFailure.MODEL_TARGET_INCOMPLETE),
        (ModelTarget.explicit("not_a_provider", "m"), DelegationFailure.PROVIDER_UNKNOWN),
    ],
)
def test_an_unresolvable_target_refuses_rather_than_falling_back(
    target: ModelTarget, expected: DelegationFailure
) -> None:
    resolved, failure, message = resolve_model_target(
        target,
        AgentThinking.INHERIT,
        inherited_provider="deepseek",
        inherited_model="deepseek-chat",
        inherited_thinking="off",
    )

    assert resolved is None
    assert failure is expected
    assert message


def test_nothing_to_inherit_is_a_refusal_not_a_default() -> None:
    resolved, failure, _message = resolve_model_target(
        ModelTarget.inherited(),
        AgentThinking.INHERIT,
        inherited_provider="",
        inherited_model="",
        inherited_thinking="off",
    )

    assert resolved is None
    assert failure is DelegationFailure.MODEL_TARGET_INCOMPLETE


def test_a_local_or_cli_provider_is_refused_for_now(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aura.providers.registry import provider_registry

    cli = [
        pid for pid in provider_registry.ids() if provider_registry.get(pid).kind != "api_key"
    ]
    if not cli:
        pytest.skip("this build registers only hosted providers")
    resolved, failure, _message = resolve_model_target(
        ModelTarget.explicit(cli[0], "m"),
        AgentThinking.INHERIT,
        inherited_provider="",
        inherited_model="",
        inherited_thinking="off",
    )

    assert resolved is None
    assert failure is DelegationFailure.PROVIDER_UNSUPPORTED


def test_an_unconfigured_provider_fails_the_delegation_not_the_conversation(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "aura.config.has_usable_provider_configuration", lambda provider=None: False
    )
    runner, backend = _runner(workspace, [_answer("never runs")])

    result = runner.run(_entry(), "review this")

    assert result.status is DelegationStatus.FAILED
    assert result.failure_class == DelegationFailure.PROVIDER_NOT_CONFIGURED.value
    assert backend.requests == []


def test_a_definition_carries_no_credential_or_url() -> None:
    definition = _definition(target=ModelTarget.explicit("openai", "gpt-x"))
    fields = set(definition.__dataclass_fields__)

    assert fields == {
        "agent_id",
        "scope",
        "name",
        "description",
        "instructions",
        "target",
        "thinking",
    }
    assert set(definition.target.__dataclass_fields__) == {"provider", "model"}


# ── failure, cancellation, and the shape that comes back ─────────────────────


def test_a_provider_failure_mid_run_comes_back_as_a_delegation_result(
    workspace: Path,
) -> None:
    runner, _backend = _runner(
        workspace, [[ApiError(status_code=500, message="upstream exploded")]]
    )

    result = runner.run(_entry(), "review this")

    assert result.status is DelegationStatus.FAILED
    assert result.failure_class == DelegationFailure.PROVIDER_ERROR.value
    assert "upstream exploded" in result.error


def test_a_failed_run_never_retries_or_switches_provider(workspace: Path) -> None:
    runner, backend = _runner(
        workspace, [[ApiError(status_code=500, message="boom")], _answer("recovered")]
    )

    result = runner.run(_entry(), "review this")

    assert result.status is DelegationStatus.FAILED
    assert len(backend.requests) == 1


def test_text_before_a_provider_failure_is_reported_as_partial(workspace: Path) -> None:
    runner, _backend = _runner(
        workspace,
        [[ContentDelta(text="I found one thing"), ApiError(status_code=500, message="boom")]],
    )

    result = runner.run(_entry(), "review this")

    assert result.status is DelegationStatus.PARTIAL
    assert result.result == "I found one thing"
    assert result.failure_class == DelegationFailure.PROVIDER_ERROR.value


def test_an_answerless_run_makes_no_claim(workspace: Path) -> None:
    runner, _backend = _runner(
        workspace,
        [[Done(finish_reason="stop", full_message={"role": "assistant", "content": ""})]],
    )

    result = runner.run(_entry(), "review this")

    assert result.status is DelegationStatus.FAILED
    assert result.failure_class == DelegationFailure.EMPTY_RESULT.value
    assert result.result == ""


def test_an_empty_task_is_refused_before_any_provider_call(workspace: Path) -> None:
    runner, backend = _runner(workspace, [_answer("never")])

    result = runner.run(_entry(), "   ")

    assert result.status is DelegationStatus.FAILED
    assert result.failure_class == DelegationFailure.TASK_MISSING.value
    assert backend.requests == []


def test_the_root_turns_cancel_event_stops_the_child(workspace: Path) -> None:
    cancel = threading.Event()
    cancel.set()
    runner, backend = _runner(workspace, [_answer("never runs")])

    result = runner.run(_entry(), "review this", cancel_event=cancel)

    assert result.status is DelegationStatus.CANCELLED
    assert backend.requests == []


def test_the_child_is_handed_the_same_cancel_event_not_a_new_one(
    workspace: Path,
) -> None:
    cancel = threading.Event()
    runner, backend = _runner(workspace, [_answer("done")])

    runner.run(_entry(), "review this", cancel_event=cancel)

    assert backend.requests[0]["cancel_event"] is cancel


def test_reported_usage_and_timing_ride_along_when_the_provider_reports_them(
    workspace: Path,
) -> None:
    rounds = [
        [
            Usage(
                prompt_tokens=120,
                completion_tokens=30,
                cache_hit_tokens=10,
                cache_miss_tokens=110,
            ),
            *_answer("all clear"),
        ]
    ]
    runner, _backend = _runner(workspace, rounds)

    payload = runner.run(_entry(), "review this").payload()

    assert payload["usage"] == {
        "prompt_tokens": 120,
        "completion_tokens": 30,
        "cache_hit_tokens": 10,
        "cache_miss_tokens": 110,
    }
    assert payload["duration_ms"] >= 0


def test_the_result_payload_names_the_status_agent_and_answer(workspace: Path) -> None:
    runner, _backend = _runner(workspace, [_answer("two defects")])

    payload = runner.run(_entry(), "review this").payload()

    assert payload["ok"] is True
    assert payload["status"] == "completed"
    assert payload["agent_id"] == "reviewer000"
    assert payload["result"] == "two defects"
    assert payload["provider"] == "deepseek"
    assert "failure_class" not in payload


# ── the root's own tool round ────────────────────────────────────────────────


def _root_round(workspace: Path, roster: AgentTurnRoster, runner: Any):
    history = History()
    history.append_user_text("review the change", available_agent_ids=roster.ids)
    registry = ToolRegistry(workspace_root=workspace)
    registry.set_turn_agent_roster(roster)
    registry.set_agent_delegation_runner(runner)
    round_runner = ToolRoundRunner(
        history=history,
        tools=registry,
        tool_runner=ToolRunner(history=history, workspace_root=workspace),
    )
    round_runner.begin_turn()
    return history, registry, round_runner


def test_only_the_paired_tool_result_reaches_canonical_root_history(
    workspace: Path,
) -> None:
    roster = _roster()
    runner, _backend = _runner(workspace, [_answer("two defects")])
    history, registry, round_runner = _root_round(workspace, roster, runner)
    tool_calls = [_call("t1", "delegate_agent", agent_id="reviewer000", task="review it")]
    history.append_assistant(
        {"role": "assistant", "content": None, "tool_calls": tool_calls}
    )
    events: list[Event] = []

    round_runner.run(
        tool_calls=tool_calls,
        on_event=events.append,
        approval_cb=lambda request: None,
        cancel_event=threading.Event(),
        cleanup_cancelled=lambda _cb: None,
        tool_defs=registry.tool_defs(),
    )

    tool_messages = [m for m in history.messages if m["role"] == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0]["tool_call_id"] == "t1"
    payload = json.loads(tool_messages[0]["content"])
    assert payload["status"] == "completed"
    assert payload["result"] == "two defects"
    assert [e for e in events if isinstance(e, ToolResult)][0].ok is True
    # The child's own conversation left no trace in the root's.
    assert [m["role"] for m in history.messages] == ["user", "assistant", "tool"]


def test_an_unknown_agent_id_is_a_structured_result_not_a_crash(
    workspace: Path,
) -> None:
    roster = _roster()
    runner, backend = _runner(workspace, [_answer("never")])
    history, registry, round_runner = _root_round(workspace, roster, runner)
    tool_calls = [_call("t1", "delegate_agent", agent_id="ghost00000", task="go")]
    history.append_assistant(
        {"role": "assistant", "content": None, "tool_calls": tool_calls}
    )

    round_runner.run(
        tool_calls=tool_calls,
        on_event=lambda _e: None,
        approval_cb=lambda request: None,
        cancel_event=threading.Event(),
        cleanup_cancelled=lambda _cb: None,
        tool_defs=registry.tool_defs(),
    )

    payload = json.loads(history.messages[-1]["content"])
    assert payload["ok"] is False
    assert payload["status"] == "failed"
    assert payload["failure_class"] == DelegationFailure.AGENT_NOT_AVAILABLE.value
    assert backend.requests == []


def test_a_registry_with_no_runner_refuses_truthfully(workspace: Path) -> None:
    registry = ToolRegistry(workspace_root=workspace)
    registry.set_turn_agent_roster(_roster())

    result = registry.execute(
        name="delegate_agent",
        args={"agent_id": "reviewer000", "task": "go"},
        approval_cb=lambda request: None,
    )

    assert result.ok is False
    assert result.payload["failure_class"] == DelegationFailure.DELEGATION_UNAVAILABLE.value


def test_the_turns_roster_is_frozen_for_the_whole_turn(workspace: Path) -> None:
    registry = ToolRegistry(workspace_root=workspace)
    registry.set_turn_agent_roster(_roster())

    first = [t["function"]["name"] for t in registry.tool_defs()]
    second = [t["function"]["name"] for t in registry.tool_defs()]

    assert first == second
    assert "delegate_agent" in first

    registry.set_turn_agent_roster(None)

    assert "delegate_agent" not in [
        t["function"]["name"] for t in registry.tool_defs()
    ]
