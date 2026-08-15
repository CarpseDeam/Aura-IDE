from __future__ import annotations

from PySide6.QtWidgets import QApplication

from aura.gui.terminal_window import TerminalWindow


def test_terminal_window_accumulates_and_routes_cards() -> None:
    app = QApplication.instance() or QApplication([])
    window = TerminalWindow()
    try:
        window.set_command("tool-1", "echo one")
        window.set_command("tool-2", "echo two")
        window.append_output("tool-1", "one output")
        window.append_output("tool-2", "two output")
        window.set_result("tool-1", 0)
        window.set_result("tool-2", 1)
        app.processEvents()

        assert set(window._terminal_cards) == {"tool-1", "tool-2"}
        assert "one output" in window._terminal_cards["tool-1"]._output_view.toPlainText()
        assert "two output" in window._terminal_cards["tool-2"]._output_view.toPlainText()
        assert window._terminal_cards["tool-1"]._state == "done"
        assert window._terminal_cards["tool-2"]._state == "failed"
    finally:
        window.clear()
        window.deleteLater()
        app.processEvents()
