"""Focused checks for the status bar's session prompt-cache percentage formatting."""
from __future__ import annotations

import sys

import pytest

pytest.importorskip("PySide6.QtWidgets")

from aura.conversation.telemetry import LatestContext  # noqa: E402
from aura.gui.status_bar import (  # noqa: E402
    AuraStatusBar,
    _format_cache_percentage,
    _format_context_occupancy,
)


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    yield app


def test_format_cache_percentage_matches_example() -> None:
    assert _format_cache_percentage(12_450, 1_820) == "87.2"


def test_format_cache_percentage_zero_hit() -> None:
    assert _format_cache_percentage(0, 100) == "0.0"


def test_format_cache_percentage_no_traffic_is_em_dash() -> None:
    assert _format_cache_percentage(0, 0) == "—"


def test_refresh_appends_cache_percentage(qapp) -> None:
    bar = AuraStatusBar()
    try:
        bar.refresh(
            workspace_root="/tmp/proj",
            model_id="test-model",
            thinking="off",
            conversation_usage={"test-model": {"hit": 12_450, "miss": 1_820, "out": 3_204}},
        )
        assert bar._status_cache._full_text == "12,450 hit · 1,820 miss · 3,204 out · cache 87.2%"
    finally:
        bar.deleteLater()


def test_refresh_cache_percentage_without_traffic(qapp) -> None:
    bar = AuraStatusBar()
    try:
        bar.refresh(
            workspace_root="/tmp/proj",
            model_id="test-model",
            thinking="off",
            conversation_usage={"test-model": {"hit": 0, "miss": 0, "out": 10}},
        )
        assert bar._status_cache._full_text == "0 hit · 0 miss · 10 out · cache —%"
    finally:
        bar.deleteLater()


def test_context_occupancy_uses_compact_counts_and_percentage() -> None:
    assert _format_context_occupancy(556_000, 1_000_000) == (
        "Context 556k / 1.0M · 55.6%",
        556,
    )


def test_context_occupancy_unknown_limit_is_honest() -> None:
    assert _format_context_occupancy(0, 0) == ("Context 0 / unknown · —", None)


def test_refresh_shows_zero_against_the_selected_model_capacity(qapp) -> None:
    bar = AuraStatusBar()
    try:
        bar.refresh(
            workspace_root="/tmp/proj",
            model_id="deepseek-v4-pro",
            thinking="off",
            conversation_usage={},
        )
        assert bar._status_context._full_text == "Context 0 / 1.0M · 0.0%"
        assert bar._context_meter.value() == 0
    finally:
        bar.deleteLater()


def test_refresh_shows_context_and_conversation_cost(qapp) -> None:
    bar = AuraStatusBar()
    try:
        bar.refresh(
            workspace_root="/tmp/proj",
            model_id="deepseek-v4-pro",
            thinking="off",
            conversation_usage={},
            latest_context=LatestContext(
                model_id="deepseek-v4-pro",
                input_tokens=556_000,
                context_window_tokens=1_000_000,
            ),
        )
        assert bar._status_context._full_text == "Context 556k / 1.0M · 55.6%"
        assert bar._context_meter.value() == 556
        assert bar._status_session.text() == "Conversation $—"
    finally:
        bar.deleteLater()


def test_handoff_is_a_footer_action_and_disables_during_execution(qapp) -> None:
    bar = AuraStatusBar()
    requests: list[bool] = []
    try:
        bar.handoff_requested.connect(lambda: requests.append(True))
        assert "Fresh Chat" in bar._handoff_btn.toolTip()
        bar.set_execution_active(True)
        assert not bar._handoff_btn.isEnabled()
        bar.set_execution_active(False)
        bar._handoff_btn.click()
        assert requests == [True]
    finally:
        bar.deleteLater()
