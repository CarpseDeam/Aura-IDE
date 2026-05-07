"""Entry point: `python -m aura`."""
from __future__ import annotations

import os
import sys

from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtGui import QGuiApplication, QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from aura.config import APP_NAME, ENV_API_KEY, icon_path
from aura.gui.main_window import MainWindow
from aura.gui.theme import apply_theme


def main() -> int:
    # Force UTF-8 stdout for any console output.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    QCoreApplication.setApplicationName(APP_NAME)
    QCoreApplication.setOrganizationName(APP_NAME)
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(str(icon_path())))
    apply_theme(app)

    if not os.environ.get(ENV_API_KEY):
        QMessageBox.critical(
            None,
            APP_NAME,
            f"{ENV_API_KEY} environment variable is not set.\n\n"
            "Set it (e.g. via System Properties → Environment Variables) and relaunch.",
        )
        return 2

    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
