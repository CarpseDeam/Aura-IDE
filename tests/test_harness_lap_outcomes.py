from __future__ import annotations

import os
import threading

from aura.bridge.harness_lap_bridge import _LapConversationRunner
from aura.bridge.production_execution import ProductionExecutionSession
from aura.conversation import ConversationManager, History
from aura.conversation.execution_outcome import ExecutionOutcomeStatus
from aura.conversation.tools import ToolRegistry

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_SCAN_DIRS = [os.path.join(_REPO_ROOT, "aura"), os.path.join(_REPO_ROOT, "tests")]
_SKIP_DIRS = {".git", ".venv", "build", "__pycache__", "node_modules"}


class _ApprovalProxy:
    def consume_last_event(self):
        return None

    def request_approval(self, *_args, **_kwargs):
        return None


def _make_runner(tmp_path, send_impl):
    history = History()
    history.append_user_text("Do the thing.")
    manager = ConversationManager(history, ToolRegistry(tmp_path))
    manager.send = send_impl  # type: ignore[method-assign]

    session = ProductionExecutionSession(_ApprovalProxy())
    runner = _LapConversationRunner(
        manager=manager,
        approval_proxy=_ApprovalProxy(),
        production_session=session,
        cancel_event=threading.Event(),
        model="test-model",
        thinking="off",
        workspace_root=tmp_path,
    )
    return manager, session, runner


def test_normal_lap_runner_never_touches_removed_provider_property(tmp_path) -> None:
    """A plain successful send() must not raise AttributeError and must finish cleanly."""

    def send(**_kwargs):
        return None

    manager, _session, runner = _make_runner(tmp_path, send)
    assert not hasattr(manager, "last_turn_provider_contract_failure")

    runner.run()

    assert runner.execution_ok is True
    assert runner.execution_status == ExecutionOutcomeStatus.completed.value


def test_blocked_evidence_propagates(tmp_path) -> None:
    def send(**_kwargs):
        manager._last_turn_blocked_reason = "waiting on user clarification"

    manager, _session, runner = _make_runner(tmp_path, send)
    runner.run()

    assert runner.execution_ok is False
    assert runner.execution_status == ExecutionOutcomeStatus.harness_error.value


def test_unexpected_runner_exception_yields_single_sanitized_harness_error(tmp_path) -> None:
    def send(**_kwargs):
        raise RuntimeError("boom: the model backend blew up")

    manager, session, runner = _make_runner(tmp_path, send)
    runner.run()

    assert runner.execution_ok is False
    assert runner.execution_status == ExecutionOutcomeStatus.harness_error.value

    api_errors = session.relay.api_errors
    assert len(api_errors) == 1
    assert "boom: the model backend blew up" in api_errors[0]


def test_no_provider_contract_failure_references_remain_repo_wide() -> None:
    needle = "provider" + "_contract_failure"
    this_file = os.path.abspath(__file__)
    hits: list[str] = []
    for scan_root in _SCAN_DIRS:
        for root, dirs, files in os.walk(scan_root):
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
            for name in files:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(root, name)
                if os.path.abspath(path) == this_file:
                    continue
                try:
                    with open(path, "r", encoding="utf-8") as fh:
                        text = fh.read()
                except (UnicodeDecodeError, OSError):
                    continue
                if needle in text.lower():
                    hits.append(path)

    assert hits == []
