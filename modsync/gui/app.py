"""Desktop application entry point."""

from __future__ import annotations

import sys
import tempfile
from importlib.resources import files
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QSettings, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from .. import __version__
from ..profiles import ProfileStore
from ..services import ModSyncService
from .logging_setup import close_logging, configure_logging
from .main_window import MainWindow


def resource_path(name: str) -> Path:
    return Path(str(files("modsync.gui.resources").joinpath(name)))


def create_application(argv: list[str] | None = None) -> QApplication:
    app = QApplication(argv if argv is not None else sys.argv)
    QCoreApplication.setOrganizationName("ModSync")
    QCoreApplication.setApplicationName("ModSync")
    QCoreApplication.setApplicationVersion(__version__)
    icon = resource_path("modsync.svg")
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))
    return app


def main() -> int:
    smoke_test = "--smoke-test" in sys.argv
    if smoke_test:
        temporary = tempfile.TemporaryDirectory(prefix="modsync-gui-smoke-")
        configure_logging(Path(temporary.name) / "logs")
        app = create_application()
        settings = QSettings(str(Path(temporary.name) / "settings.ini"), QSettings.Format.IniFormat)
        service = ModSyncService(profile_store=ProfileStore(Path(temporary.name) / "data"))
        window = MainWindow(service, settings=settings, show_onboarding=False)
        window.show()
        QTimer.singleShot(150, app.quit)
        try:
            result = app.exec()
        finally:
            close_logging()
            temporary.cleanup()
        return result
    configure_logging()
    app = create_application()
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
