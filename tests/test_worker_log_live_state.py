"""Worker Log Live-state and terminal-visibility tests.

Covers the proven failure from the 2026-08-03 production run: the backend
finished the turn truthfully (``status=no_authoritative_change``, receipt built and
emitted) while the GUI threw the receipt away and left the Worker Log chip
reading ``Live`` forever.

Two owners are exercised:

- :class:`aura.gui.info_hub_pane.InfoHubPane` — the only owner of the Live
  claim. Stopping must clear it, from any path, without clobbering a terminal
  outcome already rendered.
- :class:`aura.gui.worker_finish_presenter.WorkerFinishPresenter` — a direct
  production run's receipt is its only visible outcome, so a truthful
  non-success status must still be rendered, while dispatch flows keep their
  existing spec-card contract.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

from aura.gui.worker_finish_presenter import WorkerFinishPresenter


# ── Fakes ───────────────────────────────────────────────────────────────


class FakeChat:
    def __init__(self) -> None:
        self.worker_summaries: list[tuple] = []
        self.mismatch_cards: list[tuple] = []
        self.resolution_auras = 0

    def add_worker_summary(self, *args, **kwargs) -> None:
        self.worker_summaries.append((args, kwargs))

    def add_mismatch_resolution_card(self, *args, **kwargs) -> None:
        self.mismatch_cards.append((args, kwargs))

    def begin_planner_resolution_aura(self) -> None:
        self.resolution_auras += 1

    def mark_mismatch_resolved(self, *_args, **_kwargs) -> None:
        pass


class FakePlayground:
    def __init__(self) -> None:
        self.events: list[tuple] = []

    def stop_aura(self) -> None:
        self.events.append(("stop_aura",))

    def worker_finished(self, *args, **kwargs) -> None:
        self.events.append(("worker_finished", args, kwargs))

    def worker_cancelled(self) -> None:
        self.events.append(("worker_cancelled",))

    def set_worker_running(self, value: bool) -> None:
        self.events.append(("set_worker_running", value))


class FakeSpecCard:
    def __init__(self) -> None:
        self.finished: list[tuple] = []

    def current_spec(self) -> tuple[str, list, str, str, str]:
        return "Fix the bug", [], "", "", ""

    def worker_finished(self, ok: bool, summary: str, status: str | None = None) -> None:
        self.finished.append((ok, summary, status))


def _present(
    *,
    ok: bool,
    summary: str,
    needs_followup: bool | None,
    status: str | None,
    metadata: dict | None = None,
    spec_card=None,
) -> tuple[FakeChat, FakePlayground]:
    chat = FakeChat()
    playground = FakePlayground()
    presenter = WorkerFinishPresenter(chat, playground)
    presenter.present(
        tool_call_id="prod-b50a75f2b2ef",
        ok=ok,
        summary=summary,
        needs_followup=needs_followup,
        status=status,
        metadata=metadata or {},
        active_workflow=None,
        spec_card=spec_card,
    )
    return chat, playground


def _receipts(playground: FakePlayground) -> list[tuple]:
    return [e for e in playground.events if e[0] == "worker_finished"]


# ── InfoHubPane: the Live-state owner ───────────────────────────────────


@pytest.fixture(scope="module")
def qt_app():
    """Offscreen QApplication so the real Live-state owner can be exercised."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


@pytest.fixture
def pane(qt_app):
    from aura.gui.info_hub_pane import InfoHubPane

    return InfoHubPane()


def _chip(pane) -> str:
    return pane._status_chip.text()


def test_running_marks_live(pane) -> None:
    pane.set_worker_running(True)
    from aura.gui.info_hub_pane import _CHIP_LIVE

    assert _chip(pane) == _CHIP_LIVE


def test_stopping_clears_live(pane) -> None:
    """The exact regression: a stopped run must not keep claiming Live."""
    from aura.gui.info_hub_pane import _CHIP_IDLE, _CHIP_LIVE

    pane.set_worker_running(True)
    assert _chip(pane) == _CHIP_LIVE

    pane.set_worker_running(False)

    assert _chip(pane) == _CHIP_IDLE
    assert _chip(pane) != _CHIP_LIVE


def test_stopping_is_idempotent_and_preserves_a_rendered_receipt(pane) -> None:
    """Error and finish paths both stop the worker; neither may double-complete
    nor erase the truthful outcome the other rendered."""
    from aura.gui.info_hub_pane import _CHIP_RECEIPT

    pane.set_worker_running(True)
    pane.show_final_summary(
        False, "No authoritative change", needs_followup=True,
        status="no_authoritative_change",
    )
    assert _chip(pane) == _CHIP_RECEIPT

    pane.set_worker_running(False)
    pane.set_worker_running(False)

    assert _chip(pane) == _CHIP_RECEIPT


def test_non_success_receipt_text_is_visible_in_the_log(pane) -> None:
    pane.set_worker_running(True)
    pane.show_final_summary(
        False, "No authoritative change", needs_followup=True,
        status="no_authoritative_change",
    )
    pane.set_worker_running(False)

    assert "No authoritative change" in pane._log_view.toPlainText()


# ── WorkerFinishPresenter: truthful terminal visibility ─────────────────


@pytest.mark.parametrize(
    "status",
    [
        "no_authoritative_change",
        "harness_error",
        "blocked",
        "validation_failed",
        "no_authoritative_change",
        "provider_contract_failure",
    ],
)
def test_production_non_success_receipt_is_rendered_and_clears_live(status) -> None:
    """A direct production run has no spec card: its receipt is the only visible
    outcome, so every truthful non-success status must still be presented."""
    _chat, playground = _present(
        ok=False,
        summary=f"receipt for {status}",
        needs_followup=True,
        status=status,
        spec_card=None,
    )

    receipts = _receipts(playground)
    assert len(receipts) == 1
    args, kwargs = receipts[0][1], receipts[0][2]
    assert args[0] is False
    assert args[1] == f"receipt for {status}"
    assert kwargs["status"] == status
    # playground.worker_finished ends the Live state in the real playground;
    # a stray set_worker_running(True) must never appear on a finish path.
    assert ("set_worker_running", True) not in playground.events


def test_production_success_receipt_still_rendered() -> None:
    _chat, playground = _present(
        ok=True, summary="Production Report", needs_followup=False,
        status="completed", spec_card=None,
    )

    assert len(_receipts(playground)) == 1


def test_production_finish_does_not_fabricate_a_chat_summary() -> None:
    """No false completion receipt: the incomplete run must not gain an
    assistant-facing completion claim in the chat transcript."""
    chat, _playground = _present(
        ok=False, summary="No authoritative change", needs_followup=True,
        status="no_authoritative_change", spec_card=None,
    )

    assert chat.worker_summaries == []


def test_dispatch_non_success_keeps_spec_card_contract() -> None:
    """Dispatch flows are unchanged: the spec card owns their presentation and
    the workspace receipt stays suppressed."""
    spec_card = FakeSpecCard()
    _chat, playground = _present(
        ok=False, summary="Worker Error", needs_followup=True,
        status="harness_error", spec_card=spec_card,
    )

    assert _receipts(playground) == []
    assert ("set_worker_running", False) in playground.events
    assert spec_card.finished == [(False, "Worker Error", "harness_error")]


def test_mismatch_renders_no_receipt_but_still_clears_live() -> None:
    """A mismatch is answered by its own card, so no receipt is invented — but
    the run has stopped, so Live must still be released."""
    _chat, playground = _present(
        ok=False,
        summary="mismatch",
        needs_followup=True,
        status="harness_error",
        metadata={
            "extras": {
                "mismatch_kind": "scope",
                "mismatch_question": "Which file?",
            }
        },
        spec_card=None,
    )

    assert _receipts(playground) == []
    assert ("set_worker_running", False) in playground.events


# ── WorkerEventHandler: one cleanup per finish, cancellation distinct ───


def _make_handler():
    from aura.gui.worker_handler import WorkerEventHandler

    bridge = MagicMock()
    chat = MagicMock()
    playground = MagicMock()
    handler = WorkerEventHandler(
        bridge=bridge, chat=chat, playground=playground,
        settings=MagicMock(), parent=None,
    )
    return handler, playground


def test_finish_cleanup_runs_exactly_once(qt_app) -> None:
    handler, _playground = _make_handler()
    handler._finish_presenter.present = MagicMock(
        return_value=MagicMock(outcome=MagicMock(should_clear_dispatch_card=False))
    )
    running: list[bool] = []
    handler.worker_running_changed.connect(running.append)

    handler._on_worker_started("prod-1")
    handler._on_worker_finished(
        "prod-1", ok=False, summary="No authoritative change",
        needs_followup=True, status="no_authoritative_change",
    )
    pending = handler._pending_worker_finish
    assert pending is not None
    handler._flush_pending_worker_finish(pending.tool_call_id, pending.generation)
    # A duplicate flush of the same finish must not present or clean up twice.
    handler._flush_pending_worker_finish(pending.tool_call_id, pending.generation)

    handler._finish_presenter.present.assert_called_once()
    assert running == [True, False]
    assert handler._active_worker_tool_call_id is None


def test_cancellation_clears_live_without_a_failure_receipt(qt_app) -> None:
    handler, playground = _make_handler()
    running: list[bool] = []
    handler.worker_running_changed.connect(running.append)

    handler._on_worker_started("prod-1")
    handler._on_worker_cancelled("prod-1")

    playground.worker_cancelled.assert_called_once()
    playground.worker_finished.assert_not_called()
    assert running == [True, False]
    assert handler._active_worker_tool_call_id is None


def test_api_error_surfaces_error_and_clears_live(qt_app) -> None:
    handler, playground = _make_handler()
    running: list[bool] = []
    handler.worker_running_changed.connect(running.append)

    handler._on_worker_started("prod-1")
    handler._on_worker_api_error("prod-1", -1, "provider stream stalled after starting")

    playground.add_error.assert_called_once()
    playground.set_worker_running.assert_called_with(False)
    assert running == [True, False]
    assert handler._active_worker_tool_call_id is None
