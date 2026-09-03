"""Focused checks for the status bar's session prompt-cache percentage formatting."""
from __future__ import annotations

import sys

import pytest

pytest.importorskip("PySide6.QtWidgets")

from aura.conversation.telemetry import ConversationTelemetry, LatestContext  # noqa: E402
from aura.gui.status_bar import (  # noqa: E402
    AuraStatusBar,
    _footer_cost_tooltip,
    _format_cache_percentage,
    _format_context_occupancy,
    _format_footer_cost,
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
        assert bar._status_context.text() == "Context 0 / 1.0M · 0.0%"
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
        assert bar._status_context.text() == "Context 556k / 1.0M · 55.6%"
        assert bar._context_meter.value() == 556
        assert bar._status_session.text() == "Cost —"
    finally:
        bar.deleteLater()


def test_refresh_shows_estimated_cost_from_priced_events(qapp) -> None:
    telemetry = ConversationTelemetry()
    telemetry.record_usage(
        model_id="gpt-5.4-mini",
        prompt=1_000_000,
        completion=1_000_000,
        hit=0,
        miss=1_000_000,
        context_window_tokens=400_000,
    )
    bar = AuraStatusBar()
    try:
        bar.refresh(
            workspace_root="/tmp/proj",
            model_id="gpt-5.4-mini",
            thinking="off",
            conversation_usage=telemetry.per_model,
            telemetry=telemetry,
        )
        # 1,000,000 miss tokens @ $0.15/M + 1,000,000 output tokens @ $0.60/M
        assert bar._status_session.text() == "Est. cost $0.7500"
        assert "gpt-5.4-mini" in bar._status_session.toolTip()
    finally:
        bar.deleteLater()


def test_refresh_shows_cost_dash_for_eventless_telemetry(qapp) -> None:
    telemetry = ConversationTelemetry.from_dict({
        "per_model": {"gpt-5.4-mini": {"hit": 0, "miss": 100, "out": 50}},
        "latest_context": {"model_id": "gpt-5.4-mini", "input_tokens": 100, "context_window_tokens": 400_000},
    })
    bar = AuraStatusBar()
    try:
        bar.refresh(
            workspace_root="/tmp/proj",
            model_id="gpt-5.4-mini",
            thinking="off",
            conversation_usage=telemetry.per_model,
            telemetry=telemetry,
        )
        assert bar._status_session.text() == "Cost —"
        assert bar._status_session.toolTip() == "No priced usage recorded."
    finally:
        bar.deleteLater()


def test_context_label_is_not_squeezed_below_its_full_text_width(qapp) -> None:
    """Regression test: the context percentage must never be visually clipped.

    `_status_context` used to be a shrinkable `_ElidingLabel`, so under footer
    width pressure (long workspace path + telemetry) the trailing percentage
    (e.g. "7.7%") could be truncated to an ellipsis before the meter/Handoff
    button. It must now hold its full natural width regardless of window size,
    with the cache telemetry and workspace path labels absorbing the squeeze.
    """
    bar = AuraStatusBar()
    try:
        bar.refresh(
            workspace_root="/tmp/some/fairly/long/workspace/path/for/the/project/that/is/long",
            model_id="deepseek-v4-pro",
            thinking="off",
            conversation_usage={"deepseek-v4-pro": {"hit": 12_450, "miss": 1_820, "out": 3_204}},
            latest_context=LatestContext(
                model_id="deepseek-v4-pro",
                input_tokens=77_000,
                context_window_tokens=1_000_000,
            ),
        )
        expected_text = "Context 77k / 1.0M · 7.7%"
        assert bar._status_context.text() == expected_text

        bar.show()
        try:
            for width in (1600, 900, 700, 550):
                bar.resize(width, 30)
                qapp.processEvents()

                # The label must still show the complete, un-elided text ...
                assert bar._status_context.text() == expected_text
                # ... and must have received enough rendered width to display it,
                # not merely have the string retained while pixels are clipped.
                required_width = bar._status_context.fontMetrics().horizontalAdvance(expected_text)
                assert bar._status_context.width() >= required_width, (
                    f"at window width {width}: label width "
                    f"{bar._status_context.width()} < required {required_width}"
                )
        finally:
            bar.close()
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


def test_format_footer_cost_states() -> None:
    from decimal import Decimal

    from aura.conversation.telemetry import CostSummary

    unknown = CostSummary(known_total=None, unknown_count=1, total_events=1, exact=False, any_stale=False)
    assert _format_footer_cost(unknown) == "Cost —"

    estimated = CostSummary(known_total=Decimal("0.6402"), unknown_count=0, total_events=1, exact=False, any_stale=False)
    assert _format_footer_cost(estimated) == "Est. cost $0.6402"

    exact = CostSummary(known_total=Decimal("0.6402"), unknown_count=0, total_events=1, exact=True, any_stale=False)
    assert _format_footer_cost(exact) == "Cost $0.6402"

    stale = CostSummary(known_total=Decimal("0.6402"), unknown_count=0, total_events=1, exact=False, any_stale=True)
    assert _format_footer_cost(stale) == "Est. cost $0.6402 · stale"

    tiny = CostSummary(known_total=Decimal("0.00001"), unknown_count=0, total_events=1, exact=False, any_stale=False)
    assert _format_footer_cost(tiny) == "Est. cost < $0.0001"


def test_footer_cost_tooltip_is_generic_for_any_eventless_conversation() -> None:
    """An eventless conversation shows the same generic wording whether it has
    no usage at all, or only legacy aggregate-only telemetry with no events —
    no historical cost is inferred or reconstructed either way."""
    empty = ConversationTelemetry()
    assert _footer_cost_tooltip(empty, empty.cost_summary()) == "No priced usage recorded."

    legacy = ConversationTelemetry.from_dict({
        "per_model": {"gpt-5.4-mini": {"hit": 0, "miss": 100, "out": 50}},
        "latest_context": {},
    })
    assert _footer_cost_tooltip(legacy, legacy.cost_summary()) == "No priced usage recorded."


def test_footer_cost_tooltip_identifies_zero_cost_local_inference() -> None:
    telemetry = ConversationTelemetry()
    telemetry.record_usage(
        provider_id="local_openai",
        model_id="qwen-local",
        prompt=100,
        completion=50,
        hit=0,
        miss=100,
        context_window_tokens=0,
    )

    tooltip = _footer_cost_tooltip(telemetry, telemetry.cost_summary())
    assert "Local inference is recorded at $0 provider cost." in tooltip
    assert "published rates" not in tooltip
