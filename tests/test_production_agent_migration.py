"""Architecture proofs for the direct production-agent migration.

Normal Aura coding must run one continuous production model that receives the
user's original conversation and owns inspection through validation. The
Planner/Worker dispatch path must remain unreachable.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path

import pytest

from aura.conversation.history import History
from aura.conversation.manager import ConversationManager
from aura.conversation.tools.catalog import ToolCatalog
from aura.conversation.tools.registry import ToolRegistry
from aura.model_streams import (
    PLANNER_STREAM_HOOK,
    PRODUCTION_STREAM_HOOK,
    WORKER_STREAM_HOOK,
    ModelStreamRegistry,
)
from aura.settings import AppSettings
from aura.worker_todo import UPDATE_WORKER_TODO_TOOL

DISPATCH_TOOL = "dispatch_to_worker"


def _tool_names(defs: list[dict]) -> set[str]:
    names: set[str] = set()
    for item in defs:
        fn = item.get("function")
        if isinstance(fn, dict) and fn.get("name"):
            names.add(str(fn["name"]))
    return names


# ── 1 & 2: production tool catalog ──────────────────────────────────────────


class TestProductionToolCatalog:
    def test_single_mode_includes_todo_tool(self) -> None:
        """Proof 1: production `single` mode exposes the canonical TODO tool."""
        defs = ToolCatalog().build_tool_defs(mode="single", read_only=False)
        assert UPDATE_WORKER_TODO_TOOL in _tool_names(defs)

    def test_single_mode_excludes_dispatch_to_worker(self) -> None:
        """Proof 2: production `single` mode never exposes dispatch_to_worker."""
        defs = ToolCatalog().build_tool_defs(mode="single", read_only=False)
        assert DISPATCH_TOOL not in _tool_names(defs)

    def test_registry_in_single_mode_matches_catalog(self, tmp_path: Path) -> None:
        registry = ToolRegistry(workspace_root=tmp_path, mode="single")
        names = _tool_names(registry.tool_defs())
        assert UPDATE_WORKER_TODO_TOOL in names
        assert DISPATCH_TOOL not in names

    @pytest.mark.parametrize(
        "tool_name",
        [
            # repository reads and searches
            "read_file",
            "glob",
            "grep_search",
            "search_codebase",
            # writes / patches / deletes
            "write_file",
            "patch_file",
            "delete_file",
            # git
            "git_status",
            "git_diff",
            "git_log",
            # terminal + run-and-watch
            "run_terminal_command",
            "run_and_watch",
            # diagnostics + snapshots/undo support
            "run_diagnostic_command",
            "get_workspace_snapshot",
            # godot
            "edit_godot_scene",
            "inspect_godot_assets",
            "inspect_godot_editor",
            "inspect_godot_api",
            "edit_godot_editor",
            "install_godot_editor_bridge",
        ],
    )
    def test_production_mode_retains_capability(self, tool_name: str) -> None:
        defs = ToolCatalog().build_tool_defs(mode="single", read_only=False)
        assert tool_name in _tool_names(defs)

    @pytest.mark.parametrize(
        "tool_name",
        [
            "read_files",
            "read_file_range",
            "read_file_outline",
            "list_directory",
            "find_usages",
            "code_intel_outline",
            "code_intel_references",
            "code_intel_dependents",
            "code_intel_audit",
        ],
    )
    def test_superseded_tools_leave_single_mode_but_stay_registered(
        self, tool_name: str, tmp_path: Path
    ) -> None:
        """The superseded reads are gone from the production catalog, but their
        handlers stay registered (a replayed historical call still executes)
        and Planner, Worker, and read-only single mode keep them."""
        from aura.conversation.tools.registry import TOOL_HANDLERS

        single = _tool_names(
            ToolCatalog().build_tool_defs(mode="single", read_only=False)
        )
        assert tool_name not in single, f"{tool_name} should not be in single mode"

        # The replayed-call path must stay alive even though the schema is gone.
        assert tool_name in TOOL_HANDLERS, f"{tool_name} lost its handler entirely"

        for mode in ("planner", "worker"):
            names = _tool_names(
                ToolCatalog().build_tool_defs(mode=mode, read_only=False)
            )
            assert tool_name in names, f"{tool_name} missing from {mode} catalog"
        read_only = _tool_names(
            ToolCatalog().build_tool_defs(mode="single", read_only=True)
        )
        assert tool_name in read_only, f"{tool_name} missing from read-only catalog"

    def test_replayed_read_files_call_still_executes(self, tmp_path: Path) -> None:
        """A historical tool call from an old conversation still runs: the
        schema left single mode, the handler did not."""
        (tmp_path / "f0.py").write_text("x = 1\n", encoding="utf-8")
        registry = ToolRegistry(workspace_root=tmp_path, mode="single")
        result = registry.execute(
            "read_files",
            {"paths": ["f0.py"]},
            approval_cb=lambda request: None,
            cancel_event=threading.Event(),
        )
        assert result.ok

    def test_single_mode_catalog_is_exactly_the_expected_set(self) -> None:
        """A literal set: any addition or removal is a deliberate, visible
        diff, instead of silent drift between the capsule and the catalog."""
        defs = ToolCatalog().build_tool_defs(mode="single", read_only=False)
        assert _tool_names(defs) == {
            # reads and searches
            "read_file", "glob", "grep_search", "search_codebase",
            "inspect_godot_assets", "inspect_godot_asset_preview",
            "capture_godot_asset_preview", "inspect_godot_api",
            "inspect_godot_editor",
            # the live TODO tool
            "update_worker_todo",
            # writes / patches / deletes
            "write_file", "patch_file", "delete_file",
            "edit_godot_scene", "edit_godot_editor", "edit_godot_asset_preview",
            "install_godot_editor_bridge",
            # terminal + run-and-watch
            "run_terminal_command", "run_and_watch",
            # git
            "git_status", "git_diff", "git_log", "git_show", "git_log_file",
            "git_branch_list", "git_stash_list", "git_stash_show",
            # diagnostics + snapshots
            "run_diagnostic_command", "get_workspace_snapshot",
            # web, drones
            "web_search", "run_read_only_drone", "register_drone_folder",
        }

    def test_every_catalog_name_has_a_registered_handler(self, tmp_path: Path) -> None:
        """A schema and its handler must never drift apart: every name any
        catalog advertises resolves to a live handler — either the static
        TOOL_HANDLERS table or a round runner that intercepts the tool before
        the executor (manager_tool_round.py)."""
        from aura.conversation.tools.registry import TOOL_HANDLERS

        # Intercepted in manager_tool_round._execute_tool_call before the
        # executor is reached; keep this list in sync with those branches.
        round_runner_dispatch = {
            "dispatch_to_worker",
            "run_and_watch",
            "run_terminal_command",
        }
        handled = set(TOOL_HANDLERS) | round_runner_dispatch
        for mode in ("single", "planner", "worker"):
            names = _tool_names(
                ToolCatalog().build_tool_defs(mode=mode, read_only=False)
            )
            unhandled = sorted(names - handled)
            assert not unhandled, f"{mode} exposes tools with no handler: {unhandled}"

    def test_no_capsule_tool_name_is_missing_from_the_catalog(self) -> None:
        """The SINGLE capsule must not tell the model to use a tool the catalog
        no longer offers — the exact schema/capsule drift that shipped in §1.3."""
        from aura.context_gearbox.models import RuntimeRole
        from aura.conversation.tools.capability_groups import CAPABILITY_TOOLS
        from aura.roles import load_bundled_role_capsule

        capsule = load_bundled_role_capsule(RuntimeRole.SINGLE)
        assert capsule is not None
        known = {t for tools in CAPABILITY_TOOLS.values() for t in tools}
        mentioned = set(re.findall(r"`([A-Za-z_][A-Za-z0-9_]*)`", capsule.content))
        single = _tool_names(
            ToolCatalog().build_tool_defs(mode="single", read_only=False)
        )
        for name in sorted(mentioned & known):
            assert name in single, (
                f"capsule tells the model to use {name}, which single mode removed"
            )

    def test_production_mode_extends_dynamic_and_mcp_tools(self) -> None:
        dynamic = [{"type": "function", "function": {"name": "project_build"}}]
        mcp = [{"type": "function", "function": {"name": "mcp_thing"}}]
        defs = ToolCatalog().build_tool_defs(
            mode="single",
            read_only=False,
            dynamic_schemas=dynamic,
            mcp_schemas=mcp,
        )
        names = _tool_names(defs)
        assert "project_build" in names
        assert "mcp_thing" in names


# ── 3, 4, 5, 20: one production backend owns the request ────────────────────


class _ScriptedBackend:
    """Records every invocation and returns a scripted, finite event stream."""

    def __init__(self, events=()) -> None:
        self.calls: list[dict] = []
        self._events = list(events)

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        return iter(self._events)


@pytest.fixture
def isolated_streams(monkeypatch) -> ModelStreamRegistry:
    """Swap the module-level stream registry for an isolated one."""
    registry = ModelStreamRegistry()
    import aura.conversation.manager as manager_module

    monkeypatch.setattr(manager_module, "model_streams", registry)
    return registry


class TestProductionBackendOwnership:
    def _manager(self, tmp_path: Path, user_text: str) -> ConversationManager:
        history = History()
        history.set_system("system")
        history.append_user_text(user_text)
        registry = ToolRegistry(workspace_root=tmp_path, mode="single")
        return ConversationManager(history, registry)

    def test_production_hook_owns_the_normal_request(
        self, tmp_path: Path, isolated_streams
    ) -> None:
        """Proofs 3 + 4: the production backend runs; role hooks never do."""
        from aura.client import Done

        production = _ScriptedBackend([
            Done(finish_reason="stop", full_message={"role": "assistant", "content": "done"}),
        ])
        planner = _ScriptedBackend()
        worker = _ScriptedBackend()
        isolated_streams.register(PRODUCTION_STREAM_HOOK, production.stream)
        isolated_streams.register(PLANNER_STREAM_HOOK, planner.stream)
        isolated_streams.register(WORKER_STREAM_HOOK, worker.stream)

        manager = self._manager(tmp_path, "add a health endpoint")
        manager.send(
            on_event=lambda ev: None,
            approval_cb=lambda req: None,
            cancel_event=threading.Event(),
            model="test-model",
            thinking="off",
        )

        assert len(production.calls) == 1
        assert planner.calls == []
        assert worker.calls == []

    def test_selected_model_and_temperature_reach_the_backend(
        self, tmp_path: Path, isolated_streams
    ) -> None:
        """Proof 3: the visible production model selection owns the run."""
        from aura.client import Done

        production = _ScriptedBackend([
            Done(finish_reason="stop", full_message={"role": "assistant", "content": "ok"}),
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, production.stream)

        manager = self._manager(tmp_path, "hello")
        manager.send(
            on_event=lambda ev: None,
            approval_cb=lambda req: None,
            cancel_event=threading.Event(),
            model="chosen-production-model",
            thinking="high",
            temperature=0.25,
        )

        call = production.calls[0]
        assert call["model"] == "chosen-production-model"
        assert call["thinking"] == "high"
        assert call["temperature"] == 0.25

    def test_original_conversation_and_request_reach_the_model(
        self, tmp_path: Path, isolated_streams
    ) -> None:
        """Proof 5: no SpecCard or capsule replaces the real conversation."""
        from aura.client import Done

        production = _ScriptedBackend([
            Done(finish_reason="stop", full_message={"role": "assistant", "content": "ok"}),
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, production.stream)

        history = History()
        history.set_system("SYSTEM PROMPT")
        history.append_user_text("first turn: explain the persistence layer")
        history.append_assistant({"role": "assistant", "content": "earlier answer"})
        history.append_user_text("now make save_settings atomic, preserving legacy fields")
        registry = ToolRegistry(workspace_root=tmp_path, mode="single")
        manager = ConversationManager(history, registry)

        manager.send(
            on_event=lambda ev: None,
            approval_cb=lambda req: None,
            cancel_event=threading.Event(),
            model="m",
            thinking="off",
        )

        messages = production.calls[0]["messages"]
        texts = [str(m.get("content") or "") for m in messages]
        assert any("SYSTEM PROMPT" in t for t in texts)
        assert any("explain the persistence layer" in t for t in texts)
        assert any("earlier answer" in t for t in texts)
        assert any("make save_settings atomic" in t for t in texts)
        # The latest original request is the final user message verbatim.
        user_messages = [m for m in messages if m.get("role") == "user"]
        assert user_messages[-1]["content"] == (
            "now make save_settings atomic, preserving legacy fields"
        )

    def test_production_tool_defs_offered_exclude_dispatch(
        self, tmp_path: Path, isolated_streams
    ) -> None:
        """Proof 20: the model is never offered the dispatch tool."""
        from aura.client import Done

        production = _ScriptedBackend([
            Done(finish_reason="stop", full_message={"role": "assistant", "content": "ok"}),
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, production.stream)

        manager = self._manager(tmp_path, "do the thing")
        manager.send(
            on_event=lambda ev: None,
            approval_cb=lambda req: None,
            cancel_event=threading.Event(),
            model="m",
            thinking="off",
        )
        offered = _tool_names(production.calls[0]["tools"])
        assert DISPATCH_TOOL not in offered
        assert UPDATE_WORKER_TODO_TOOL in offered


# ── role-neutral runtime configuration ──────────────────────────────────────


class TestRuntimeContextConfiguration:
    def test_configure_runtime_context_defaults_to_single_role(
        self, tmp_path: Path
    ) -> None:
        from aura.context_gearbox.models import RuntimeRole

        manager = ConversationManager(
            History(), ToolRegistry(workspace_root=tmp_path, mode="single")
        )
        manager.configure_runtime_context("base", tmp_path)
        assert manager._planner_refresh.role == RuntimeRole.SINGLE

    def test_configure_for_planner_remains_a_compatibility_alias(
        self, tmp_path: Path
    ) -> None:
        from aura.context_gearbox.models import RuntimeRole

        manager = ConversationManager(
            History(), ToolRegistry(workspace_root=tmp_path, mode="single")
        )
        manager.configure_for_planner("base", tmp_path)
        assert manager._planner_refresh.role == RuntimeRole.PLANNER


# ── production role prompt contract ─────────────────────────────────────────


class TestProductionRolePrompt:
    def test_single_capsule_states_the_production_contract(self) -> None:
        from aura.context_gearbox.models import RuntimeRole
        from aura.roles import load_bundled_role_capsule

        capsule = load_bundled_role_capsule(RuntimeRole.SINGLE)
        assert capsule is not None
        text = capsule.content.lower()

        # Inspection, TODO, iteration, validation, repair, reporting.
        for token in (
            "inspect",
            "update_worker_todo",
            "active",
            "validat",
            "repair",
            "rerun",
        ):
            assert token in text, f"production capsule missing '{token}'"
        # Never delegates implementation.
        assert "never dispatch implementation" in text
        # Strong but not ceremonial.
        assert len(capsule.content) < 6000


# ── 17: settings migration ──────────────────────────────────────────────────


class TestSettingsMigration:
    def test_old_planner_only_config_migrates_to_production(self) -> None:
        """Proof 17: legacy planner values become the production configuration."""
        settings = AppSettings.from_dict({
            "planner_provider": "deepseek",
            "default_planner_model": "deepseek-v4-flash",
            "default_planner_thinking": "max",
            "planner_worker_mode": True,
            "worker_provider": "deepseek",
            "default_worker_model": "deepseek-v4-pro",
            "temperature": 0.4,
        })
        assert settings.provider == "deepseek"
        assert settings.default_model == "deepseek-v4-flash"
        assert settings.default_thinking == "max"
        assert settings.temperature == 0.4

    def test_generic_production_values_win_over_planner_values(self) -> None:
        settings = AppSettings.from_dict({
            "provider": "deepseek",
            "default_model": "deepseek-v4-pro",
            "default_thinking": "off",
            "planner_provider": "deepseek",
            "default_planner_model": "deepseek-v4-flash",
            "default_planner_thinking": "max",
        })
        assert settings.default_model == "deepseek-v4-pro"
        assert settings.default_thinking == "off"

    def test_planner_worker_mode_is_never_forced_true(self) -> None:
        assert AppSettings.from_dict({}).planner_worker_mode is False
        # An explicitly saved legacy value is preserved, not destroyed...
        assert (
            AppSettings.from_dict({"planner_worker_mode": True}).planner_worker_mode
            is True
        )

    def test_legacy_fields_survive_load(self) -> None:
        settings = AppSettings.from_dict({
            "planner_provider": "deepseek",
            "worker_provider": "deepseek",
            "default_planner_model": "deepseek-v4-flash",
            "default_worker_model": "deepseek-v4-pro",
            "default_worker_thinking": "high",
            "worker_temperature": 0.2,
            "planner_system_prompt": "legacy planner prompt",
            "worker_system_prompt": "legacy worker prompt",
        })
        assert settings.planner_provider == "deepseek"
        assert settings.worker_provider == "deepseek"
        assert settings.default_worker_model == "deepseek-v4-pro"
        assert settings.default_worker_thinking == "high"
        assert settings.worker_temperature == 0.2
        assert settings.planner_system_prompt == "legacy planner prompt"
        assert settings.worker_system_prompt == "legacy worker prompt"

    def test_invalid_provider_and_model_fall_back_safely(self) -> None:
        settings = AppSettings.from_dict({
            "provider": "no_such_provider",
            "default_model": "no-such-model",
            "default_thinking": "supercharged",
        })
        from aura.providers.registry import provider_registry

        assert provider_registry.has(settings.provider)
        assert settings.default_model in provider_registry.get(settings.provider).models
        assert settings.default_thinking in ("off", "high", "max")

    def test_removed_google_providers_migrate(self) -> None:
        settings = AppSettings.from_dict({"provider": "vertex_ai"})
        assert settings.provider == "deepseek"

    def test_empty_config_loads(self) -> None:
        settings = AppSettings.from_dict({})
        from aura.providers.registry import provider_registry

        assert provider_registry.has(settings.provider)
        assert settings.default_model


# ── 18: persistence of older conversations and dispatch records ─────────────


class TestLegacyConversationLoading:
    def test_v2_conversation_with_dispatch_records_loads(self, tmp_path: Path) -> None:
        """Proof 18: old records containing dispatch metadata still load."""
        import json

        from aura.conversation.persistence import load_conversation

        payload = {
            "version": 2,
            "planner_worker_mode": True,
            "model": "deepseek-v4-flash",
            "thinking": "high",
            "planner_model": "deepseek-v4-flash",
            "worker_model": "deepseek-v4-pro",
            "planner_thinking": "high",
            "worker_thinking": "high",
            "provider": "deepseek",
            "planner_provider": "deepseek",
            "worker_provider": "deepseek",
            "system_prompt": "old system prompt",
            "messages": [
                {"role": "user", "content": "build the thing"},
                {"role": "assistant", "content": "dispatching"},
            ],
            "worker_dispatches": [
                {
                    "after_message_index": 1,
                    "tool_call_id": "call_legacy_1",
                    "spec": {
                        "goal": "implement the thing",
                        "files": ["a.py"],
                        "spec": "old spec",
                        "acceptance": "old acceptance",
                        "summary": "old summary",
                    },
                    "worker_history": [
                        {"role": "user", "content": "old worker capsule"}
                    ],
                    "result_summary": "done",
                }
            ],
        }
        path = tmp_path / "legacy.json"
        path.write_text(json.dumps(payload), encoding="utf-8")

        loaded = load_conversation(path)
        assert loaded.planner_worker_mode is True
        assert len(loaded.history.messages) == 2
        assert loaded.history.messages[0]["content"] == "build the thing"
        assert len(loaded.worker_dispatches) == 1
        record = loaded.worker_dispatches[0]
        assert record.tool_call_id == "call_legacy_1"
        assert record.spec["goal"] == "implement the thing"
        assert record.result_summary == "done"
