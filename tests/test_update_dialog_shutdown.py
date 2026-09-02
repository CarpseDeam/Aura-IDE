"""Regression coverage for the packaged updater shutdown handoff."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtWidgets import QApplication  # noqa: E402

from aura.gui import update_dialog  # noqa: E402
from aura.gui.update_dialog import UpdateDialog  # noqa: E402
from aura.updater import PullResult  # noqa: E402


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


class _AcceptProbeDialog(UpdateDialog):
    def __init__(self) -> None:
        super().__init__()
        self.accept_calls = 0

    def accept(self) -> None:
        self.accept_calls += 1
        super().accept()


@pytest.mark.parametrize("worker_cleared_first", [False, True])
def test_installer_handoff_waits_for_worker_and_accepts_once(
    qapp: QApplication,
    monkeypatch: pytest.MonkeyPatch,
    worker_cleared_first: bool,
) -> None:
    monkeypatch.setattr(update_dialog, "is_packaged", lambda: True)
    dialog = _AcceptProbeDialog()
    marker = object()
    dialog._thread = marker  # type: ignore[assignment]
    dialog._worker = marker  # type: ignore[assignment]

    try:
        if worker_cleared_first:
            dialog._clear_worker()

        dialog._show_pull_result(
            PullResult(True, None, message="Installer launched. Quitting Aura...")
        )

        if not worker_cleared_first:
            assert dialog.accept_calls == 0
            dialog._clear_worker()

        assert dialog.exit_after_install is True
        assert dialog._thread is None
        assert dialog._worker is None
        assert dialog.accept_calls == 1

        dialog._clear_worker()
        assert dialog.accept_calls == 1
    finally:
        dialog.deleteLater()
        qapp.processEvents()
