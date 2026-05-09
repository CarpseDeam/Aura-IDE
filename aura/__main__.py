"""Entry point: `python -m aura`."""
from __future__ import annotations

import sys

from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtGui import QGuiApplication, QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from aura.config import APP_NAME, PROVIDERS, get_api_key, icon_path, load_settings
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

    # Force software OpenGL to suppress startup window flicker on Windows.
    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_UseSoftwareOpenGL, True)

    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(str(icon_path())))
    apply_theme(app)

    # Check if any provider has an API key configured (env var or stored).
    settings = load_settings()
    selected_provider = settings.provider
    has_any_key = any(get_api_key(pid) is not None for pid in PROVIDERS)
    has_selected_key = get_api_key(selected_provider) is not None

    if not has_any_key:
        # No provider at all has a key — show a warning but don't block.
        QMessageBox.warning(
            None,
            APP_NAME,
            "No API key found for any provider.\n\n"
            "Set one of the following environment variables or open "
            "Settings → API Key to paste a key:\n"
            + "\n".join(f"  • {cfg.env_key}  ({cfg.label})" for cfg in PROVIDERS.values())
            + "\n\nThe app will open, but chat will fail until a key is configured.",
        )
    elif not has_selected_key:
        # Selected provider doesn't have a key, but another one does.
        cfg = PROVIDERS[selected_provider]
        QMessageBox.warning(
            None,
            APP_NAME,
            f"No API key found for {cfg.label}.\n\n"
            f"Set the {cfg.env_key} environment variable or open "
            f"Settings → API Key to paste one.\n\n"
            f"The app will open, but chat with this provider will fail "
            f"until a key is configured.",
        )

    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
