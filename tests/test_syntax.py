from __future__ import annotations

import os

import pytest
from PySide6.QtWidgets import QApplication, QPlainTextEdit

from aura.gui.syntax import DiffHighlighter, PygmentsHighlighter


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_pygments_highlighter_handles_yaml_plain_scalar(qapp) -> None:
    edit = QPlainTextEdit()
    edit.setPlainText("plain: value\n")
    highlighter = PygmentsHighlighter(edit.document(), "yaml")

    highlighter.rehighlight()


def test_diff_highlighter_handles_yaml_plain_scalar(qapp) -> None:
    edit = QPlainTextEdit()
    edit.setPlainText("@@ -1 +1 @@\n-plain: old\n+plain: new\n")
    highlighter = DiffHighlighter(edit.document(), "yaml")

    highlighter.rehighlight()
