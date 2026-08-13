"""PlanReviewProxy: conversation-thread <-> GUI synchronization for one active review."""
from __future__ import annotations

import sys
import threading
import time

import pytest

pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtWidgets import QApplication  # noqa: E402

from aura.bridge.plan_review_proxy import PlanReviewProxy  # noqa: E402
from aura.conversation.plan_review import ApprovedPlan  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    yield app


def _wait_for_pending(proxy: PlanReviewProxy, timeout: float = 2.0) -> None:
    deadline = time.time() + timeout
    while proxy._pending is None and time.time() < deadline:
        time.sleep(0.01)
    assert proxy._pending is not None, "conversation thread never registered its pending review"


def test_stop_cancels_a_pending_review_and_unblocks_the_conversation_thread(qapp) -> None:
    proxy = PlanReviewProxy()
    result: dict = {}

    def run_conversation() -> None:
        result["decision"] = proxy.request_review("goal", [], "spec", "acceptance", "")

    t = threading.Thread(target=run_conversation)
    t.start()
    _wait_for_pending(proxy)

    proxy.cancel_active()
    t.join(timeout=2)

    assert not t.is_alive(), "Stop must unblock the conversation thread, never strand it"
    assert result["decision"].approved is False


def test_resolve_approved_delivers_the_edited_plan_to_the_conversation_thread(qapp) -> None:
    proxy = PlanReviewProxy()
    result: dict = {}

    def run_conversation() -> None:
        result["decision"] = proxy.request_review("goal", ["a.py"], "spec", "acceptance", "")

    t = threading.Thread(target=run_conversation)
    t.start()
    _wait_for_pending(proxy)

    review_id = proxy._pending.review_id
    plan = ApprovedPlan(goal="edited", files=("a.py",), spec="s2", acceptance="a2", summary="sum")
    assert proxy.resolve_approved(review_id, plan, user_edited=True) is True
    t.join(timeout=2)

    decision = result["decision"]
    assert decision.approved is True
    assert decision.user_edited is True
    assert decision.plan is plan


def test_resolve_with_stale_review_id_is_a_no_op(qapp) -> None:
    proxy = PlanReviewProxy()
    assert proxy.resolve_cancelled("no-such-review") is False


def test_review_requested_signal_carries_the_review_id(qapp) -> None:
    proxy = PlanReviewProxy()
    seen: list[tuple] = []
    proxy.reviewRequested.connect(lambda *args: seen.append(args))

    def run_conversation() -> None:
        proxy.request_review("goal text", ["x.py"], "spec text", "acceptance text", "summary text")

    t = threading.Thread(target=run_conversation)
    t.start()
    _wait_for_pending(proxy)
    review_id = proxy._pending.review_id

    # The signal crosses threads (conversation -> GUI), so delivery is queued and
    # needs the GUI thread's event loop pumped, exactly as a real Qt app does.
    deadline = time.time() + 2.0
    while not seen and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.01)

    proxy.cancel_active()
    t.join(timeout=2)

    assert seen, "reviewRequested must fire so the GUI can render the card"
    review_id_arg, goal, files, spec, acceptance, summary = seen[0]
    assert review_id_arg == review_id
    assert goal == "goal text"
    assert files == ["x.py"]
    assert spec == "spec text"
    assert acceptance == "acceptance text"
    assert summary == "summary text"
