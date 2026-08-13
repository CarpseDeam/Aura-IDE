"""SearchableModelCombo: shared search/filter/select picker used by both
ModelsPage (Settings) and LeftPane (main window).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication

from aura.gui.widgets.searchable_model_combo import SearchableModelCombo
from aura.providers.model_presentation import ModelPickerItem


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    yield app


def _items() -> list[ModelPickerItem]:
    return [
        ModelPickerItem("deepseek/deepseek-v4-flash", "DeepSeek V4 Flash"),
        ModelPickerItem("openai/gpt-5.4", "GPT 5.4"),
        ModelPickerItem("anthropic/claude-sonnet", "Claude Sonnet"),
    ]


def _key_event(key: Qt.Key) -> QKeyEvent:
    return QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier)


class TestSetItemsSelection:
    def test_set_items_selects_requested_model_id(self, qapp):
        combo = SearchableModelCombo()
        combo.set_items(_items(), selected_model_id="openai/gpt-5.4")
        assert combo.currentData() == "openai/gpt-5.4"

    def test_set_items_falls_back_to_first_when_selection_absent(self, qapp):
        combo = SearchableModelCombo()
        combo.set_items(_items(), selected_model_id="does/not-exist")
        assert combo.currentData() == "deepseek/deepseek-v4-flash"

    def test_set_items_is_ordinary_combo_box_api(self, qapp):
        combo = SearchableModelCombo()
        combo.set_items(_items())
        assert combo.count() == 3
        assert combo.findData("anthropic/claude-sonnet") == 2
        assert combo.itemText(1) == "GPT 5.4"


class TestSearchFiltering:
    def test_label_search_is_case_insensitive(self, qapp):
        combo = SearchableModelCombo()
        combo.set_items(_items())
        combo._popup.load(
            [(combo.itemText(i), combo.itemData(i)) for i in range(combo.count())],
            combo.currentIndex(),
        )
        combo._popup.search.setText("gpt")
        assert combo._popup.list.count() == 1
        assert combo._popup.list.item(0).text() == "GPT 5.4"

        combo._popup.search.setText("GPT")
        assert combo._popup.list.count() == 1

    def test_model_id_search_matches(self, qapp):
        combo = SearchableModelCombo()
        combo.set_items(_items())
        combo._popup.load(
            [(combo.itemText(i), combo.itemData(i)) for i in range(combo.count())],
            combo.currentIndex(),
        )
        combo._popup.search.setText("anthropic/")
        assert combo._popup.list.count() == 1
        assert combo._popup.list.item(0).data(Qt.ItemDataRole.UserRole) == (
            "anthropic/claude-sonnet"
        )

    def test_unrelated_entries_are_hidden(self, qapp):
        combo = SearchableModelCombo()
        combo.set_items(_items())
        combo._popup.load(
            [(combo.itemText(i), combo.itemData(i)) for i in range(combo.count())],
            combo.currentIndex(),
        )
        combo._popup.search.setText("deepseek")
        visible_ids = {
            combo._popup.list.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(combo._popup.list.count())
        }
        assert visible_ids == {"deepseek/deepseek-v4-flash"}

    def test_clearing_search_restores_all_entries(self, qapp):
        combo = SearchableModelCombo()
        combo.set_items(_items())
        combo._popup.load(
            [(combo.itemText(i), combo.itemData(i)) for i in range(combo.count())],
            combo.currentIndex(),
        )
        combo._popup.search.setText("gpt")
        assert combo._popup.list.count() == 1
        combo._popup.search.setText("")
        assert combo._popup.list.count() == 3

    def test_reopening_resets_search_text(self, qapp):
        combo = SearchableModelCombo()
        combo.set_items(_items())
        combo.showPopup()
        combo._popup.search.setText("gpt")
        assert combo._popup.list.count() == 1

        combo.showPopup()
        assert combo._popup.search.text() == ""
        assert combo._popup.list.count() == 3


class TestSelection:
    def test_clicking_a_filtered_item_selects_it_by_model_id(self, qapp):
        combo = SearchableModelCombo()
        combo.set_items(_items(), selected_model_id="deepseek/deepseek-v4-flash")
        combo.showPopup()
        combo._popup.search.setText("claude")
        item = combo._popup.list.item(0)
        combo._popup._choose_item(item)
        assert combo.currentData() == "anthropic/claude-sonnet"

    def test_enter_selects_the_highlighted_row(self, qapp):
        combo = SearchableModelCombo()
        combo.set_items(_items())
        combo.showPopup()
        combo._popup.list.setCurrentRow(1)
        combo._popup.eventFilter(combo._popup.search, _key_event(Qt.Key.Key_Return))
        assert combo.currentData() == "openai/gpt-5.4"

    def test_down_then_up_arrow_moves_highlighted_row(self, qapp):
        combo = SearchableModelCombo()
        combo.set_items(_items())
        combo.showPopup()
        assert combo._popup.list.currentRow() == 0
        combo._popup.eventFilter(combo._popup.search, _key_event(Qt.Key.Key_Down))
        assert combo._popup.list.currentRow() == 1
        combo._popup.eventFilter(combo._popup.search, _key_event(Qt.Key.Key_Up))
        assert combo._popup.list.currentRow() == 0

    def test_escape_closes_without_changing_the_current_value(self, qapp):
        combo = SearchableModelCombo()
        combo.set_items(_items(), selected_model_id="deepseek/deepseek-v4-flash")
        combo.showPopup()
        combo._popup.search.setText("this is not a real model id")
        combo._popup.eventFilter(combo._popup.search, _key_event(Qt.Key.Key_Escape))
        assert combo.currentData() == "deepseek/deepseek-v4-flash"

    def test_arbitrary_search_text_never_becomes_the_value(self, qapp):
        combo = SearchableModelCombo()
        combo.set_items(_items(), selected_model_id="deepseek/deepseek-v4-flash")
        combo.showPopup()
        combo._popup.search.setText("xyz-not-a-model-nonexistent")
        combo.hidePopup()
        assert combo.currentData() == "deepseek/deepseek-v4-flash"
        assert combo.currentText() == "DeepSeek V4 Flash"
        assert not combo.isEditable()


class TestWheelSuppression:
    def test_wheel_events_are_ignored(self, qapp):
        combo = SearchableModelCombo()
        combo.set_items(_items())
        event = MagicMock()
        combo.wheelEvent(event)
        event.ignore.assert_called_once()


class TestSharedPickerOwnership:
    def test_models_page_uses_searchable_model_combo(self, qapp, monkeypatch):
        from aura.config import AppSettings
        from aura.gui.settings_pages.models_page import ModelsPage

        monkeypatch.setattr(ModelsPage, "_start_discovery", lambda self, _pid: None)
        page = ModelsPage(AppSettings())
        try:
            assert isinstance(page._model_combo, SearchableModelCombo)
        finally:
            page.cleanup_threads()
            page.deleteLater()

    def test_left_pane_uses_searchable_model_combo(self, qapp):
        from aura.gui.left_pane import LeftPane

        with patch("aura.gui.left_pane.ProjectStore") as MockStore:
            MockStore.return_value = MagicMock()
            pane = LeftPane(Path("/tmp/test-workspace"))
            try:
                assert isinstance(pane._planner_model_combo, SearchableModelCombo)
            finally:
                pane.deleteLater()
