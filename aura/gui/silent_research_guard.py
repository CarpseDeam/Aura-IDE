"""Qt UI guard for answer-only silent research turns."""

from __future__ import annotations

import logging
from collections.abc import Iterable

from PySide6.QtCore import QObject, QEvent, QTimer
from PySide6.QtWidgets import QApplication, QWidget

_log = logging.getLogger(__name__)


class SilentResearchUiGuard(QObject):
    """Suppress floating Aura work surfaces while answer-only research runs."""

    def __init__(
        self,
        owner: QWidget,
        *,
        blocked_widgets: Iterable[QWidget] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        super().__init__(owner)
        self._owner = owner
        self._blocked_widgets: set[QWidget] = set(blocked_widgets or [])
        self._logger = logger or _log
        self._active = False
        self._installed = False

    def set_blocked_widgets(self, widgets: Iterable[QWidget]) -> None:
        self._blocked_widgets = {widget for widget in widgets if widget is not None}

    def start(self) -> None:
        app = QApplication.instance()
        if app is None:
            return
        if not self._installed:
            app.installEventFilter(self)
            self._installed = True
        self._active = True
        self.suppress_existing_surfaces()
        self._logger.info("answer_only_research_ui_guard_start")

    def stop(self) -> None:
        self._active = False
        app = QApplication.instance()
        if app is not None and self._installed:
            app.removeEventFilter(self)
        self._installed = False
        self._logger.info("answer_only_research_ui_guard_stop")

    def suppress_existing_surfaces(self) -> None:
        app = QApplication.instance()
        if app is None:
            return
        for widget in app.topLevelWidgets():
            if self._should_block_widget(widget):
                widget.hide()
        for widget in list(self._blocked_widgets):
            widget.hide()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            self._active
            and event.type()
            in {
                QEvent.Type.Show,
                QEvent.Type.ShowToParent,
                QEvent.Type.WindowActivate,
            }
            and isinstance(watched, QWidget)
            and self._should_block_widget(watched)
        ):
            event.ignore()
            QTimer.singleShot(0, watched.hide)
            self._logger.warning(
                "answer_only_research_ui_suppressed widget=%s title=%r",
                type(watched).__name__,
                watched.windowTitle(),
            )
            return True
        return super().eventFilter(watched, event)

    def _should_block_widget(self, widget: QWidget) -> bool:
        return _should_block_silent_research_surface(
            is_owner=widget is self._owner,
            is_owner_window=widget is self._owner.window(),
            is_explicitly_blocked=widget in self._blocked_widgets,
            is_window=bool(widget.isWindow()),
        )


def _should_block_silent_research_surface(
    *,
    is_owner: bool,
    is_owner_window: bool,
    is_explicitly_blocked: bool,
    is_window: bool,
) -> bool:
    if is_owner or is_owner_window:
        return False
    if is_explicitly_blocked:
        return True
    return is_window
