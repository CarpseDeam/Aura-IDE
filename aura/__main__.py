"""Entry point: `python -m aura`."""
from __future__ import annotations

import logging
import os
import platform
import sys
import argparse
from pathlib import Path

from aura.startup_logging import configure_startup_logging

logger = logging.getLogger(__name__)


def main() -> int:
    log_path = configure_startup_logging()
    args, qt_argv = _parse_args(sys.argv[1:])

    try:
        return _run_app(log_path, args, qt_argv)
    except Exception as exc:
        logger.critical("Aura failed to start", exc_info=True)
        if _qapplication_exists():
            try:
                _show_crash_dialog(log_path, exc)
            except Exception:
                logger.exception("failed to show startup crash dialog")
                _print_startup_failure(log_path, exc)
        else:
            _print_startup_failure(log_path, exc)
        return 1


def _run_app(log_path: Path, args: argparse.Namespace, qt_argv: list[str]) -> int:
    # Force UTF-8 stdout for any console output.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if args.profile_dir is not None:
        profile_dir = Path(args.profile_dir).expanduser()
        os.environ["AURA_CONFIG_DIR"] = str(profile_dir)
        os.environ["AURA_DATA_DIR"] = str(profile_dir)

    logger.info("app start")
    logger.info("argv: %r", sys.argv)
    logger.info("qt argv: %r", qt_argv)
    logger.info("Python: %s", sys.version.replace("\n", " "))
    logger.info("platform: %s", platform.platform())

    from aura.paths import config_dir, data_dir

    logger.info("config path: %s", config_dir())
    logger.info("data path: %s", data_dir())
    logger.info("startup log path: %s", log_path)

    from PySide6.QtCore import QCoreApplication, QTimer, Qt
    from PySide6.QtGui import QGuiApplication, QIcon
    from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

    app_name = "Aura"

    QCoreApplication.setApplicationName(app_name)
    QCoreApplication.setOrganizationName(app_name)
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    # Force software OpenGL to suppress startup window flicker on Windows.
    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_UseSoftwareOpenGL, True)

    logger.info("QApplication creation start")
    app = QApplication(qt_argv)
    logger.info("QApplication creation end")

    from aura.config import APP_NAME, PROVIDERS, has_usable_provider_configuration, get_provider_kind, icon_path, load_settings
    from aura.gui.theme import apply_theme

    app.setWindowIcon(QIcon(str(icon_path())))

    logger.info("theme apply start")
    apply_theme(app)
    logger.info("theme apply end")

    logger.info("settings load start")
    settings = load_settings()
    logger.info("settings load end")

    selected_provider = settings.provider

    _should_open_api_settings = False

    if args.startup_smoke:
        logger.info("startup smoke mode: skipping provider warnings")
    elif not has_usable_provider_configuration():
        # No provider at all is configured/available — show setup dialog.
        logger.info("no providers configured — showing setup dialog")
        from aura.gui.setup_dialog import SetupDialog
        dlg = SetupDialog()
        result = dlg.exec()
        if dlg._continue_readonly:
            logger.info("user chose Continue Read-only")
        elif result == QDialog.DialogCode.Accepted:
            logger.info("user chose Open Provider Settings")
            _should_open_api_settings = True
        else:
            logger.info("user chose Exit")
            return 0
    elif not has_usable_provider_configuration(selected_provider):
        # Selected provider is not configured/available, but another one is.
        cfg = PROVIDERS[selected_provider]
        kind = get_provider_kind(selected_provider)
        if kind == "external_cli":
            logger.info("selected provider external CLI unavailable warning start")
            QMessageBox.warning(
                None,
                APP_NAME,
                f"{cfg.label} is selected, but its CLI executable is not available.\n\n"
                "Install and sign in to the CLI, or choose another provider in "
                "Settings -> Provider Setup.\n\n"
                "The app will open, but chat with this provider will fail until "
                "the CLI is available.",
            )
            logger.info("selected provider external CLI unavailable warning end")
            _should_open_api_settings = True
        else:
            logger.info("provider key warning start")
            QMessageBox.warning(
                None,
                APP_NAME,
                f"No API key found for {cfg.label}.\n\n"
                f"Set the {cfg.env_key} environment variable or open "
                f"Settings → API Key to paste one.\n\n"
                f"The app will open, but chat with this provider will fail "
                f"until a key is configured.",
            )
            logger.info("provider key warning end")

    from aura.gui.main_window import MainWindow

    logger.info("MainWindow construction start")
    win = MainWindow()
    logger.info("MainWindow construction end")

    logger.info("win.show start")
    win.show()
    logger.info("win.show end")

    if _should_open_api_settings:
        logger.info("opening API settings post-startup")
        QTimer.singleShot(100, win.open_api_settings)

    if args.startup_smoke:
        logger.info("startup smoke mode: scheduling quit")
        QTimer.singleShot(500, QApplication.quit)

    logger.info("app.exec start")
    exit_code = app.exec()
    logger.info("app.exec end: %s", exit_code)
    return 0 if args.startup_smoke else exit_code


def _parse_args(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument(
        "--profile-dir",
        help="Use PATH for both Aura config and data during this run.",
    )
    parser.add_argument(
        "--startup-smoke",
        action="store_true",
        help="Open Aura briefly, then quit with a startup success/failure code.",
    )
    args, qt_args = parser.parse_known_args(argv)
    return args, [sys.argv[0], *qt_args]


def _show_crash_dialog(log_path: Path, exc: Exception) -> None:
    from PySide6.QtWidgets import QMessageBox

    QMessageBox.critical(
        None,
        "Aura failed to start",
        f"{type(exc).__name__}: {exc}\n\nStartup log:\n{log_path}",
    )


def _print_startup_failure(log_path: Path, exc: Exception) -> None:
    message = (
        f"Aura failed to start: {type(exc).__name__}: {exc}\n"
        f"Startup log: {log_path}"
    )
    stream = sys.stderr or sys.stdout
    if stream is not None:
        print(message, file=stream)


def _qapplication_exists() -> bool:
    try:
        from PySide6.QtWidgets import QApplication
    except Exception:
        return False
    return QApplication.instance() is not None


if __name__ == "__main__":
    sys.exit(main())
