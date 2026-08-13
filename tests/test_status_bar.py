"""Focused checks for the status bar's session prompt-cache percentage formatting."""
from __future__ import annotations

import sys

import pytest

pytest.importorskip("PySide6.QtWidgets")

from aura.gui.status_bar import (  # noqa: E402
    AuraStatusBar,
    _format_cache_percentage,
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
            session_usage={"test-model": {"hit": 12_450, "miss": 1_820, "out": 3_204}},
        )
        assert bar._status_cache.text() == "12,450 hit · 1,820 miss · 3,204 out · cache 87.2%"
    finally:
        bar.deleteLater()


def test_refresh_cache_percentage_without_traffic(qapp) -> None:
    bar = AuraStatusBar()
    try:
        bar.refresh(
            workspace_root="/tmp/proj",
            model_id="test-model",
            thinking="off",
            session_usage={"test-model": {"hit": 0, "miss": 0, "out": 10}},
        )
        assert bar._status_cache.text() == "0 hit · 0 miss · 10 out · cache —%"
    finally:
        bar.deleteLater()
