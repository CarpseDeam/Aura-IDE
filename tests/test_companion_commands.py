"""Tests for companion command handlers — projects, conversations, drones, receipts."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from aura.companion.commands import CommandContext
from aura.companion.commands.conversations import (
    handle_conversation_create,
    handle_conversation_list,
    handle_conversation_select,
)
from aura.companion.commands.drones import handle_drone_list_recent, handle_drone_status
from aura.companion.commands.projects import handle_project_list_recent
from aura.companion.commands.receipts import handle_receipt_list_recent
from aura.companion.state import CompanionState
from aura.settings import AppSettings


def _make_context(
    state: CompanionState | None = None,
    *,
    bridge: any = None,
    drone_runner: any = None,
    project_store: any = None,
) -> tuple[CommandContext, list[dict]]:
    sent: list[dict] = []

    def send_fn(env: dict) -> None:
        sent.append(env)

    ctx = CommandContext(
        state=state or CompanionState(),
        settings=AppSettings(),
        send_fn=send_fn,
        bridge=bridge,
        drone_runner=drone_runner,
        project_store=project_store,
    )
    return ctx, sent


def _msg(type_: str, **overrides: any) -> dict:
    m = {
        "id": "msg_1",
        "sender_device_id": "phone_abc",
        "type": type_,
        "payload": {},
    }
    m.update(overrides)
    return m


# ── project.list_recent ────────────────────────────────────


class TestHandleProjectListRecent:
    def test_empty_workspace_root_returns_empty_list(self) -> None:
        ctx, sent = _make_context()
        handle_project_list_recent(_msg("project.list_recent"), ctx)
        assert len(sent) == 1
        assert sent[0]["type"] == "project.list_result"
        assert sent[0]["payload"]["projects"] == []

    def test_returns_project_dtos_with_thread_counts(self) -> None:
        state = CompanionState(workspace_root="/tmp/fake")
        ctx, sent = _make_context(state)

        fake_project = MagicMock()
        fake_project.id = "p1"
        fake_project.name = "Test Project"
        fake_project.updated_at = "2025-01-01T00:00:00"
        fake_project.archived = False

        fake_thread = MagicMock()
        fake_thread.id = "t1"

        with patch("aura.companion.commands.projects.ProjectStore") as MockStore:
            store = MagicMock()
            MockStore.return_value = store
            store.list_projects.return_value = [fake_project]
            store.list_threads.return_value = [fake_thread]

            handle_project_list_recent(_msg("project.list_recent"), ctx)

        assert len(sent) == 1
        env = sent[0]
        assert env["type"] == "project.list_result"
        dtos = env["payload"]["projects"]
        assert len(dtos) == 1
        dto = dtos[0]
        assert dto["id"] == "p1"
        assert dto["name"] == "Test Project"
        assert dto["thread_count"] == 1


# ── conversation.list ──────────────────────────────────────


class TestHandleConversationList:
    def test_missing_project_returns_empty_threads(self) -> None:
        state = CompanionState(workspace_root="/tmp/fake", current_project_id="p1")
        ctx, sent = _make_context(state)

        with patch("aura.companion.commands.conversations.ProjectStore") as MockStore:
            store = MagicMock()
            MockStore.return_value = store
            store.load_project.return_value = None

            handle_conversation_list(_msg("conversation.list"), ctx)

        assert len(sent) == 1
        assert sent[0]["type"] == "conversation.list_result"
        assert sent[0]["payload"]["threads"] == []
        assert "error" in sent[0]["payload"]

    def test_returns_thread_dtos(self) -> None:
        state = CompanionState(
            workspace_root="/tmp/fake",
            current_project_id="p1",
            current_conversation_id="t2",
        )
        ctx, sent = _make_context(state)

        fake_project = MagicMock()
        fake_project.id = "p1"

        fake_thread = MagicMock()
        fake_thread.id = "t2"
        fake_thread.title = "My Thread"
        fake_thread.updated_at = "2025-02-02T00:00:00"
        fake_thread.is_current = False

        with patch("aura.companion.commands.conversations.ProjectStore") as MockStore:
            store = MagicMock()
            MockStore.return_value = store
            store.load_project.return_value = fake_project
            store.list_threads.return_value = [fake_thread]

            handle_conversation_list(_msg("conversation.list"), ctx)

        assert len(sent) == 1
        env = sent[0]
        assert env["type"] == "conversation.list_result"
        dtos = env["payload"]["threads"]
        assert len(dtos) == 1
        assert dtos[0]["id"] == "t2"
        assert dtos[0]["title"] == "My Thread"
        assert dtos[0]["is_current"] is True  # matches current_conversation_id


# ── conversation.select ────────────────────────────────────


class TestHandleConversationSelect:
    def test_missing_thread_id_returns_error(self) -> None:
        ctx, sent = _make_context()
        handle_conversation_select(_msg("conversation.select", payload={}), ctx)
        assert sent[0]["type"] == "conversation.selected"
        assert "error" in sent[0]["payload"]

    def test_busy_bridge_returns_error(self) -> None:
        state = CompanionState(workspace_root="/tmp/fake", current_project_id="p1")
        bridge = MagicMock()
        bridge.is_running.return_value = True
        ctx, sent = _make_context(state, bridge=bridge)

        fake_project = MagicMock()
        fake_project.root_path = "/tmp/fake/project"
        fake_thread = MagicMock()
        fake_thread.conversation_path = "/tmp/fake/project/.aura/conversations/chat.json"

        with patch("aura.companion.commands.conversations.ProjectStore") as MockStore:
            store = MagicMock()
            MockStore.return_value = store
            store.load_project.return_value = fake_project
            store.load_thread.return_value = fake_thread

            handle_conversation_select(
                _msg("conversation.select", payload={"thread_id": "t1"}),
                ctx,
            )

        assert sent[0]["type"] == "conversation.selected"
        assert sent[0]["payload"]["error"] == "Desktop is busy"

    def test_success_triggers_on_conversation_selected_callback(self) -> None:
        state = CompanionState(workspace_root="/tmp/fake", current_project_id="p1")
        bridge = MagicMock()
        bridge.is_running.return_value = False
        captured_args = []

        def on_selected(root: Path, conv_path: Path) -> None:
            captured_args.append((root, conv_path))

        ctx, sent = _make_context(state, bridge=bridge)
        ctx.on_conversation_selected = on_selected

        fake_project = MagicMock()
        fake_project.root_path = Path("/tmp/fake/project")
        fake_thread = MagicMock()
        fake_thread.conversation_path = Path("/tmp/fake/project/.aura/conversations/chat.json")

        with patch("aura.companion.commands.conversations.ProjectStore") as MockStore:
            store = MagicMock()
            MockStore.return_value = store
            store.load_project.return_value = fake_project
            store.load_thread.return_value = fake_thread

            handle_conversation_select(
                _msg("conversation.select", payload={"thread_id": "t1"}),
                ctx,
            )

        assert len(captured_args) == 1
        assert captured_args[0][0] == fake_project.root_path
        assert captured_args[0][1] == fake_thread.conversation_path
        assert state.pending_select_msg is not None


# ── drone.list_recent ──────────────────────────────────────


class TestHandleDroneListRecent:
    def test_empty_workspace_returns_empty_runs(self) -> None:
        ctx, sent = _make_context()
        handle_drone_list_recent(_msg("drone.list_recent"), ctx)
        assert sent[0]["type"] == "drone.list_result"
        assert sent[0]["payload"]["runs"] == []

    def test_returns_run_summaries(self) -> None:
        state = CompanionState(workspace_root="/tmp/fake")
        ctx, sent = _make_context(state)

        with patch("aura.companion.commands.drones.RunHistoryStore.list_runs") as mock_list:
            mock_list.return_value = [
                {"run_id": "r1", "drone_name": "Test Drone", "status": "completed", "started_at": "2025-03-03T00:00:00"},
            ]

            handle_drone_list_recent(_msg("drone.list_recent"), ctx)

        assert len(sent) == 1
        env = sent[0]
        assert env["type"] == "drone.list_result"
        runs = env["payload"]["runs"]
        assert len(runs) == 1
        assert runs[0]["run_id"] == "r1"
        assert runs[0]["label"] == "Test Drone"
        assert runs[0]["kind"] == "drone"


# ── drone.status ───────────────────────────────────────────


class TestHandleDroneStatus:
    def test_no_drone_runner_returns_not_running(self) -> None:
        ctx, sent = _make_context()
        handle_drone_status(_msg("drone.status"), ctx)
        assert sent[0]["type"] == "drone.status_result"
        assert sent[0]["payload"]["running"] is False

    def test_running_drone_returns_run_summary(self) -> None:
        state = CompanionState()
        runner = MagicMock()
        run_state = MagicMock()
        run_state.run_id = "r2"
        run_state.status = "running"
        run_state.started_at = 1710000000.0
        run_state.drone.name = "My Drone"
        runner.run_state.return_value = run_state

        ctx, sent = _make_context(state, drone_runner=runner)
        handle_drone_status(_msg("drone.status"), ctx)

        assert sent[0]["type"] == "drone.status_result"
        assert sent[0]["payload"]["running"] is True
        assert sent[0]["payload"]["run"]["run_id"] == "r2"
        assert sent[0]["payload"]["run"]["status"] == "running"


# ── receipt.list_recent ────────────────────────────────────


class TestHandleReceiptListRecent:
    def test_empty_workspace_returns_empty_receipts(self) -> None:
        ctx, sent = _make_context()
        handle_receipt_list_recent(_msg("receipt.list_recent"), ctx)
        assert sent[0]["type"] == "receipt.list_result"
        assert sent[0]["payload"]["receipts"] == []

    def test_returns_receipt_summaries(self) -> None:
        state = CompanionState(workspace_root="/tmp/fake")
        ctx, sent = _make_context(state)

        with patch("aura.companion.commands.receipts.RunHistoryStore.list_runs") as mock_list:
            mock_list.return_value = [
                {
                    "run_id": "r1",
                    "drone_name": "Test Drone",
                    "status": "completed",
                    "ended_at": "2025-04-04T00:00:00",
                    "summary": "All good",
                },
            ]

            handle_receipt_list_recent(_msg("receipt.list_recent"), ctx)

        assert len(sent) == 1
        env = sent[0]
        assert env["type"] == "receipt.list_result"
        receipts = env["payload"]["receipts"]
        assert len(receipts) == 1
        assert receipts[0]["run_id"] == "r1"
        assert receipts[0]["kind"] == "drone"
        assert receipts[0]["summary"] == "All good"


# ── conversation.create ────────────────────────────────────


class TestHandleConversationCreate:
    def test_missing_project_id_returns_error(self) -> None:
        ctx, sent = _make_context()
        handle_conversation_create(_msg("conversation.create", payload={}), ctx)
        assert sent[0]["type"] == "conversation.selected"
        assert sent[0]["payload"]["error"] == "Missing project_id"

    def test_missing_project_returns_error(self) -> None:
        ctx, sent = _make_context()
        with patch("aura.companion.commands.conversations.ProjectStore") as MockStore:
            store = MagicMock()
            MockStore.return_value = store
            store.load_project.return_value = None

            handle_conversation_create(
                _msg("conversation.create", payload={"project_id": "p1"}),
                ctx,
            )

        assert sent[0]["type"] == "conversation.selected"
        assert sent[0]["payload"]["error"] == "Project not found"

    def test_busy_bridge_returns_error(self) -> None:
        bridge = MagicMock()
        bridge.is_running.return_value = True
        ctx, sent = _make_context(bridge=bridge)

        fake_project = MagicMock()
        fake_project.root_path = Path("/tmp/fake/project")

        with patch("aura.companion.commands.conversations.ProjectStore") as MockStore:
            store = MagicMock()
            MockStore.return_value = store
            store.load_project.return_value = fake_project

            handle_conversation_create(
                _msg("conversation.create", payload={"project_id": "p1"}),
                ctx,
            )

        assert sent[0]["type"] == "conversation.selected"
        assert sent[0]["payload"]["error"] == "Desktop is busy"

    def test_success_creates_thread_and_triggers_selection(self) -> None:
        bridge = MagicMock()
        bridge.is_running.return_value = False
        ctx, sent = _make_context(bridge=bridge)
        ctx.settings.provider = "deepseek"
        ctx.settings.planner_provider = "deepseek"
        ctx.settings.worker_provider = "deepseek"

        fake_project = MagicMock()
        fake_project.root_path = Path("/tmp/fake/project")
        fake_project.last_thread_id = None

        fake_thread = MagicMock()
        fake_thread.id = "new_thread_1"
        fake_thread.conversation_path = None

        captured_args = []

        def on_selected(root: Path, conv_path: Path) -> None:
            captured_args.append((root, conv_path))

        ctx.on_conversation_selected = on_selected

        with (
            patch("aura.companion.commands.conversations.ProjectStore") as MockStore,
            patch("aura.companion.commands.conversations.save_conversation") as mock_save,
        ):
            store = MagicMock()
            MockStore.return_value = store
            store.load_project.return_value = fake_project
            store.create_thread.return_value = fake_thread

            fake_conv_path = Path("/tmp/fake/project/.aura/conversations/2025-01-01-new-chat.json")
            mock_save.return_value = fake_conv_path

            handle_conversation_create(
                _msg("conversation.create", payload={"project_id": "p1"}),
                ctx,
            )

        # Verify save_conversation was called
        mock_save.assert_called_once()

        # Verify thread was created with title
        store.create_thread.assert_called_once_with(fake_project, title="New chat")

        # Verify thread.conversation_path was set and saved
        assert fake_thread.conversation_path == fake_conv_path
        store.save_thread.assert_called_once_with(fake_project, fake_thread)

        # Verify project.last_thread_id was set
        assert fake_project.last_thread_id == "new_thread_1"
        store.save_project.assert_called_once_with(fake_project)

        # Verify on_conversation_selected was called
        assert len(captured_args) == 1
        assert captured_args[0][0] == fake_project.root_path
        assert captured_args[0][1] == fake_conv_path

        # Verify no error was sent
        assert len(sent) == 0

    def test_pending_select_msg_contains_new_ids(self) -> None:
        bridge = MagicMock()
        bridge.is_running.return_value = False
        ctx, sent = _make_context(bridge=bridge)
        ctx.settings.provider = "deepseek"
        ctx.settings.planner_provider = "deepseek"
        ctx.settings.worker_provider = "deepseek"

        fake_project = MagicMock()
        fake_project.root_path = Path("/tmp/fake/project")
        fake_project.last_thread_id = None

        fake_thread = MagicMock()
        fake_thread.id = "new_thread_1"
        fake_thread.conversation_path = None

        with (
            patch("aura.companion.commands.conversations.ProjectStore") as MockStore,
            patch("aura.companion.commands.conversations.save_conversation") as mock_save,
        ):
            store = MagicMock()
            MockStore.return_value = store
            store.load_project.return_value = fake_project
            store.create_thread.return_value = fake_thread
            mock_save.return_value = Path("/tmp/fake/project/.aura/conversations/chat.json")

            msg = _msg("conversation.create", payload={"project_id": "p1"})
            handle_conversation_create(msg, ctx)

        pending = ctx.state.pending_select_msg
        assert pending is not None
        assert pending["id"] == msg["id"]
        assert pending["sender_device_id"] == msg["sender_device_id"]
        assert pending["payload"]["project_id"] == "p1"
        assert pending["payload"]["thread_id"] == "new_thread_1"
