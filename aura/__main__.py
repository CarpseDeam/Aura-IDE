"""Entry point: `python -m aura`."""
from __future__ import annotations

import argparse
import logging
import os
import platform
import sys
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
        if args.selfcheck:
            _print_startup_failure(log_path, exc)
        elif _qapplication_exists():
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

    if args.companion_relay_only:
        return _run_companion_relay(args.host, args.port)

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

    from PySide6.QtCore import QCoreApplication, Qt, QTimer
    from PySide6.QtGui import QGuiApplication, QIcon
    from PySide6.QtWidgets import QApplication, QMessageBox

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

    from aura.config import (
        APP_NAME,
        PROVIDERS,
        get_provider_kind,
        has_usable_provider_configuration,
        icon_path,
        load_settings,
    )
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
    _should_open_aura_settings = False

    if not has_usable_provider_configuration():
        logger.info("no providers configured — opening settings post-startup if not first launch")
        if settings.first_launch_done:
            _should_open_aura_settings = True
        # When first_launch_done is False, OnboardingDialog Step 4 covers
        # provider awareness with its "Open Settings" button.
    elif not has_usable_provider_configuration(selected_provider):
        # Selected provider is not configured/available, but another one is.
        # If Aura Credits is configured, auto-switch to it silently.
        if has_usable_provider_configuration("aura"):
            logger.info(
                "selected provider %s not configured but Aura Credits is — auto-switching to aura",
                selected_provider,
            )
            settings.planner_provider = "aura"
            settings.worker_provider = "aura"
            settings.provider = "aura"
            from aura.config import save_settings
            save_settings(settings)
            # Re-read selected_provider for subsequent checks
            selected_provider = "aura"
        else:
            cfg = PROVIDERS[selected_provider]
            kind = get_provider_kind(selected_provider)
            if kind == "external_cli":
                if not args.selfcheck:
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
                _should_open_aura_settings = True
            else:
                if not args.selfcheck:
                    logger.info("provider key warning start")
                    QMessageBox.warning(
                        None,
                        APP_NAME,
                        f"{cfg.label} is selected, but no API key is configured.\n\n"
                        "Choose one of these options in Settings → Credits & Account:\n"
                        "  • Set up Aura Credits (easiest — no API key needed)\n"
                        "  • Bring your own API key in Settings → API Keys\n\n"
                        "The app will open, but chat will fail until a provider is configured.",
                    )
                    logger.info("provider key warning end")
                _should_open_aura_settings = True

    from aura.gui.main_window import MainWindow

    logger.info("MainWindow construction start")
    win = MainWindow()
    logger.info("MainWindow construction end")

    logger.info("win.show start")
    win.show()
    logger.info("win.show end")

    if not args.selfcheck:
        if _should_open_aura_settings:
            logger.info("opening Aura settings post-startup")
            QTimer.singleShot(100, win.open_aura_settings)
        elif _should_open_api_settings:
            logger.info("opening API settings post-startup")
            QTimer.singleShot(100, win.open_api_settings)

    def _finish_selfcheck() -> None:
        logger.info("Aura selfcheck success")
        print("Aura selfcheck: OK", flush=True)
        if args.dump_ui_tree is not None:
            try:
                from aura.ui_probe import dump_ui_tree
                dump_ui_tree(win, args.dump_ui_tree)
            except Exception:
                logger.exception("dump-ui-tree failed")
        logging.shutdown()
        os._exit(0)


    if args.selfcheck:
        QTimer.singleShot(300, _finish_selfcheck)

    logger.info("app.exec start")
    exit_code = app.exec()
    logger.info("app.exec end: %s", exit_code)
    return exit_code


def _parse_args(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument(
        "--profile-dir",
        help="Use PATH for both Aura config and data during this run.",
    )
    parser.add_argument(
        "--selfcheck",
        action="store_true",
        help="Boot the app, confirm the main window constructs, then exit cleanly. Used by automated launch verification.",
    )
    parser.add_argument("--dump-ui-tree", type=str, default=None, metavar="PATH",
        help="(Honored only alongside --selfcheck.) Write an accessibility-tree snapshot as JSON to PATH.",
    )
    parser.add_argument("--companion-relay-only", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--host", default="127.0.0.1", help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, default=8765, help=argparse.SUPPRESS)
    args, qt_args = parser.parse_known_args(argv)
    return args, [sys.argv[0], *qt_args]


def _run_companion_relay(host: str, port: int) -> int:
    logger.info("starting Companion relay only on %s:%s", host, port)
    try:
        import uvicorn

        uvicorn.run("relay.main:app", host=host, port=port, log_level="info")
    except Exception:
        logger.exception("Companion relay failed")
        return 1
    return 0


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
