"""Main ModSync desktop window and frontend orchestration."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QByteArray, QSettings, QThreadPool, QTimer, Qt, QUrl
from PySide6.QtGui import QCloseEvent, QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from ..models import GameInstallation, InstallReport
from ..services import ModSyncService, OperationCancelled, ProgressEvent
from .dialogs import (
    AboutDialog,
    AddModDialog,
    CreateProfileDialog,
    GameSelectionDialog,
    OnboardingDialog,
    SettingsDialog,
    SwitchPreviewDialog,
)
from .logging_setup import log_directory
from .theme import apply_theme
from .widgets import BackupsPage, GamesPage, ModsPage, ProfilesPage
from .workers import OperationWorker

LOGGER = logging.getLogger("modsync.gui")


class MainWindow(QMainWindow):
    """Thin Qt frontend: all domain mutations go through ModSyncService."""

    def __init__(
        self,
        service: ModSyncService | None = None,
        *,
        settings: QSettings | None = None,
        show_onboarding: bool = True,
    ) -> None:
        super().__init__()
        self.setObjectName("mainWindow")
        self.service = service or ModSyncService()
        self.settings = settings or QSettings("ModSync", "ModSync")
        self.thread_pool = QThreadPool.globalInstance()
        self._worker: OperationWorker | None = None
        self._workers: set[OperationWorker] = set()
        self._build_ui()
        self._connect_ui()
        self._restore_window_state()
        self.refresh()
        try:
            needs_onboarding = not self.service.list_profiles()
        except Exception:
            # refresh() has already presented the actionable startup error.
            needs_onboarding = False
        if show_onboarding and needs_onboarding and not self.settings.value(
            "onboardingComplete", False, bool
        ):
            QTimer.singleShot(0, self.show_onboarding)

    def _build_ui(self) -> None:
        self.setWindowTitle("ModSync")
        self.resize(1040, 680)
        root = QWidget()
        outer = QVBoxLayout(root)
        outer.setContentsMargins(18, 14, 18, 10)
        header = QHBoxLayout()
        brand = QLabel("ModSync")
        brand.setStyleSheet("font-size: 24px; font-weight: 700;")
        header.addWidget(brand)
        header.addStretch()
        self.about_button = QPushButton("About")
        self.settings_button = QPushButton("Settings ⚙")
        header.addWidget(self.about_button)
        header.addWidget(self.settings_button)
        outer.addLayout(header)

        content = QHBoxLayout()
        self.navigation = QListWidget()
        self.navigation.setObjectName("navigation")
        self.navigation.setFixedWidth(170)
        for name in ("Games", "Profiles", "Mods", "Backups"):
            self.navigation.addItem(QListWidgetItem(name))
        self.pages = QStackedWidget()
        self.games_page = GamesPage()
        self.profiles_page = ProfilesPage()
        self.mods_page = ModsPage()
        self.backups_page = BackupsPage()
        for page in (
            self.games_page,
            self.profiles_page,
            self.mods_page,
            self.backups_page,
        ):
            self.pages.addWidget(page)
        content.addWidget(self.navigation)
        content.addWidget(self.pages, 1)
        outer.addLayout(content, 1)
        self.setCentralWidget(root)

        status = QStatusBar()
        self.status_label = QLabel("Ready")
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.progress_bar.setMaximumWidth(260)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setVisible(False)
        status.addWidget(self.status_label, 1)
        status.addPermanentWidget(self.progress_bar)
        status.addPermanentWidget(self.cancel_button)
        self.setStatusBar(status)
        self.setStyleSheet(
            "QListWidget#navigation { border: none; padding: 8px; }"
            "QListWidget#navigation::item { padding: 12px; border-radius: 6px; }"
            "QListWidget#navigation::item:selected { background: palette(highlight); }"
            "QPushButton { padding: 6px 12px; }"
        )

    def _connect_ui(self) -> None:
        self.navigation.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.navigation.setCurrentRow(int(self.settings.value("section", 0)))
        self.settings_button.clicked.connect(self.show_settings)
        self.about_button.clicked.connect(lambda: AboutDialog(self).exec())
        self.cancel_button.clicked.connect(self.cancel_operation)
        self.games_page.rediscover_requested.connect(self.rediscover)
        self.games_page.create_requested.connect(self.show_create_profile)
        self.games_page.open_folder_requested.connect(self.open_folder)
        self.profiles_page.create_requested.connect(self.show_create_profile)
        self.profiles_page.import_requested.connect(self.import_modpack)
        self.profiles_page.switch_requested.connect(self.preview_switch)
        self.profiles_page.delete_requested.connect(self.delete_profile)
        self.mods_page.add_requested.connect(self.add_mod)
        self.mods_page.updates_requested.connect(self.check_updates)
        self.mods_page.action_requested.connect(self.lifecycle_action)
        self.backups_page.restore_requested.connect(self.restore_backup)
        QShortcut(QKeySequence.StandardKey.Refresh, self, activated=self.refresh)
        QShortcut(QKeySequence("Ctrl+,"), self, activated=self.show_settings)

    def _restore_window_state(self) -> None:
        geometry = self.settings.value("geometry")
        if isinstance(geometry, QByteArray):
            self.restoreGeometry(geometry)
        apply_theme(QApplication.instance(), str(self.settings.value("theme", "System")))

    def closeEvent(self, event: QCloseEvent) -> None:
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("section", self.navigation.currentRow())
        super().closeEvent(event)

    def refresh(self) -> None:
        try:
            profiles = self.service.list_profiles()
            active = self.service.active_profile()
            active_name = active.name if active is not None else None
            self.games_page.set_game(self.service.game_view())
            self.profiles_page.set_profiles(profiles, active_name)
            self.mods_page.set_mods(self.service.list_mods(active_name))
            backups = self.service.list_backups(active_name) if active_name else []
            self.backups_page.set_backups(active_name or "", backups)
            self.status_label.setText("Ready")
        except Exception as exc:
            self.present_error("Could not load ModSync data", exc)

    def _run_operation(self, operation, on_success=None) -> None:
        if self._worker is not None:
            self.status_label.setText("Another operation is already running")
            return
        worker = OperationWorker(operation)
        self._worker = worker
        self._workers.add(worker)
        worker.signals.progress.connect(self._show_progress)
        worker.signals.error.connect(self._operation_error)
        if on_success is not None:
            # Run chained actions after the current worker's finished signal has
            # cleared the single-operation guard.
            worker.signals.result.connect(
                lambda result: QTimer.singleShot(0, lambda: on_success(result))
            )
        worker.signals.finished.connect(lambda: self._operation_finished(worker))
        worker.signals.cancellation_changed.connect(self._cancellation_changed)
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setVisible(True)
        self.cancel_button.setVisible(True)
        self.cancel_button.setEnabled(True)
        self.thread_pool.start(worker)

    def _show_progress(self, event: ProgressEvent) -> None:
        self.status_label.setText(event.message)
        self.cancel_button.setEnabled(event.cancellable)
        self.cancel_button.setText("Cancel" if event.cancellable else "Finishing safely…")
        if event.total and event.current is not None:
            self.progress_bar.setRange(0, event.total)
            self.progress_bar.setValue(min(event.current, event.total))
        else:
            self.progress_bar.setRange(0, 0)

    def _operation_error(self, error: Exception) -> None:
        if isinstance(error, OperationCancelled):
            self.status_label.setText(str(error))
            return
        self.present_error("Operation failed", error)

    def _operation_finished(self, worker: OperationWorker) -> None:
        self._workers.discard(worker)
        if self._worker is worker:
            self._worker = None
        self.progress_bar.setVisible(False)
        self.cancel_button.setVisible(False)
        self.cancel_button.setText("Cancel")
        self.refresh()

    def _cancellation_changed(self, accepted: bool) -> None:
        self.status_label.setText(
            "Cancelling safely…" if accepted else "Finishing safely…"
        )
        self.cancel_button.setEnabled(False)

    def cancel_operation(self) -> None:
        if self._worker is not None:
            self._worker.cancel()

    def present_error(self, title: str, error: Exception | str) -> None:
        message = str(error)
        LOGGER.error("%s: %s", title, message)
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Icon.Critical)
        dialog.setWindowTitle(title)
        dialog.setText(title)
        if "rolled back" in message.casefold():
            dialog.setInformativeText("Your previous installation was restored successfully.")
        else:
            dialog.setInformativeText(message.splitlines()[0] if message else "Unknown error")
        dialog.setDetailedText(message)
        dialog.exec()

    def show_onboarding(self) -> None:
        dialog = OnboardingDialog(self)
        dialog.discover_requested.connect(lambda: (dialog.accept(), self.rediscover(True)))
        dialog.manual_requested.connect(lambda: (dialog.accept(), self.show_create_profile(manual=True)))
        dialog.exec()

    def rediscover(self, create_after: bool = False) -> None:
        self._run_operation(
            lambda progress, token: self.service.discover_games(
                "valheim", progress=progress, token=token
            ),
            lambda results: self._discovery_finished(results, create_after),
        )

    def _discovery_finished(
        self, results: list[GameInstallation], create_after: bool
    ) -> None:
        valid = [item for item in results if item.validated]
        if not valid:
            answer = QMessageBox.question(
                self,
                "Valheim was not found automatically",
                "Try again or choose the game folder manually?",
                QMessageBox.StandardButton.Retry | QMessageBox.StandardButton.Open,
            )
            if answer == QMessageBox.StandardButton.Retry:
                self.rediscover(create_after)
            elif answer == QMessageBox.StandardButton.Open:
                self.show_create_profile(manual=True)
            return
        installation = valid[0]
        if len(valid) > 1:
            selection = GameSelectionDialog(valid, self)
            if selection.exec() != QDialog.DialogCode.Accepted:
                return
            installation = selection.selected_installation() or installation
        self.status_label.setText(f"Found {installation.display_name}: {installation.install_path}")
        if create_after or not self.service.list_profiles():
            self.show_create_profile(installation=installation)

    def show_create_profile(
        self,
        *,
        installation: GameInstallation | None = None,
        manual: bool = False,
        modpack_path: Path | None = None,
    ) -> None:
        manual_path = None
        if manual:
            value = QFileDialog.getExistingDirectory(self, "Choose Valheim folder")
            if not value:
                return
            manual_path = Path(value)
        dialog = CreateProfileDialog(
            self, installation=installation, manual_path=manual_path
        )
        if modpack_path is not None:
            dialog.modpack.setText(str(modpack_path))
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        name, modpack, discover, selected_path = dialog.values()
        if discover and installation is None:
            self._run_operation(
                lambda progress, token: self.service.discover_games(
                    "valheim", progress=progress, token=token
                ),
                lambda results: self._create_after_discovery(name, modpack, results),
            )
            return
        self._create_profile(name, modpack, installation if discover else None, selected_path)

    def _create_after_discovery(
        self, name: str, modpack: Path, results: list[GameInstallation]
    ) -> None:
        valid = [item for item in results if item.validated]
        if not valid:
            self.present_error("Valheim was not found", "Choose its folder manually.")
            return
        selected = valid[0]
        if len(valid) > 1:
            dialog = GameSelectionDialog(valid, self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            selected = dialog.selected_installation() or selected
        self._create_profile(name, modpack, selected, None)

    def _create_profile(
        self,
        name: str,
        modpack: Path,
        installation: GameInstallation | None,
        manual_path: Path | None,
    ) -> None:
        self._run_operation(
            lambda progress, token: self.service.create_profile(
                name, modpack, installation=installation, manual_path=manual_path
            ),
            self._profile_created,
        )

    def _profile_created(self, profile) -> None:
        self.settings.setValue("onboardingComplete", True)
        if self.service.active_profile() is None:
            self.service.activate_profile(profile.name)
        answer = QMessageBox.question(
            self,
            "Profile created",
            f"Install {profile.mod_count} configured mod(s) now?",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._run_operation(
                lambda progress, token: self.service.install_profile(
                    profile.name, progress=progress, token=token
                ),
                self._install_finished,
            )

    def import_modpack(self) -> None:
        value, _ = QFileDialog.getOpenFileName(self, "Import Modpack", filter="JSON (*.json)")
        if not value:
            return
        path = Path(value)
        try:
            preview = self.service.import_modpack(path)
        except Exception as exc:
            self.present_error("Invalid modpack", exc)
            return
        summary = (
            f"Name: {preview.name}\n"
            f"Version: {preview.version}\n"
            f"Game: {preview.game}\n"
            f"Mods: {len(preview.mods)}\n\n"
            "Create a profile from this modpack?"
        )
        if (
            QMessageBox.question(self, "Import Modpack", summary)
            == QMessageBox.StandardButton.Yes
        ):
            self.show_create_profile(modpack_path=path)

    def preview_switch(self, target_name: str) -> None:
        self._run_operation(
            lambda progress, token: self.service.switch_plan(target_name),
            lambda plan: self._confirm_switch(plan),
        )

    def _confirm_switch(self, plan) -> None:
        dialog = SwitchPreviewDialog(plan, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._run_operation(
                lambda progress, token: self.service.switch_profile(
                    plan.target_profile, progress=progress, token=token
                )
            )

    def delete_profile(self, name: str) -> None:
        if self._confirm_destructive(
            "Delete profile?", f"Delete ModSync data for {name}? Game files will remain."
        ):
            self._run_operation(
                lambda progress, token: self.service.delete_profile(name)
            )

    def add_mod(self) -> None:
        active = self.service.active_profile()
        if active is None:
            self.present_error("No active profile", "Create or activate a profile first.")
            return
        dialog = AddModDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._run_operation(
            lambda progress, token: self.service.add_mod(active.name, dialog.definition()),
            lambda profile: self._ask_install_added(profile.name),
        )

    def _ask_install_added(self, profile_name: str) -> None:
        if QMessageBox.question(self, "Mod added", "Install configured changes now?") == QMessageBox.StandardButton.Yes:
            self._run_operation(
                lambda progress, token: self.service.install_profile(
                    profile_name, progress=progress, token=token
                ),
                self._install_finished,
            )

    def check_updates(self) -> None:
        active = self.service.active_profile()
        if active is None:
            self.present_error("No active profile", "Create or activate a profile first.")
            return
        self._run_operation(
            lambda progress, token: self.service.check_updates(
                active.name, progress=progress, token=token
            ),
            lambda preview: self._updates_found(active.name, preview),
        )

    def _updates_found(self, profile_name: str, preview) -> None:
        if not preview.available:
            QMessageBox.information(self, "Updates", "All mods are up to date.")
            return
        names = "\n".join(f"• {name}" for name in preview.changed_packages)
        if QMessageBox.question(self, "Updates available", names or f"{preview.planned_files} file(s)") == QMessageBox.StandardButton.Yes:
            self._run_operation(
                lambda progress, token: self.service.update_profile(
                    profile_name, progress=progress, token=token
                ),
                self._install_finished,
            )

    def _install_finished(self, report: InstallReport) -> None:
        if report.failures:
            reason = "\n".join(f"{item.mod_name}: {item.message}" for item in report.failures)
            if report.rollback_succeeded:
                reason += "\n\nYour previous installation was restored successfully."
            self.present_error("Installation failed", reason)
        else:
            self.status_label.setText(
                f"Done: {report.installed} installed, {report.skipped} unchanged"
            )

    def lifecycle_action(self, action: str, mod_name: str) -> None:
        active = self.service.active_profile()
        if active is None:
            return
        self._run_operation(
            lambda progress, token: self.service.lifecycle_preview(
                active.name, action, mod_name
            ),
            lambda preview: self._confirm_lifecycle(active.name, action, mod_name, preview),
        )

    def _confirm_lifecycle(self, profile_name: str, action: str, mod_name: str, preview) -> None:
        details = f"Managed files: {len(preview.paths)}"
        if preview.preserved_files:
            details += "\nModified configuration files will be preserved."
        if preview.orphan_dependencies:
            details += "\n\nPotential orphan dependencies:\n" + "\n".join(
                f"• {name}" for name in preview.orphan_dependencies
            )
        if self._confirm_destructive(f"{action.title()} {mod_name}?", details):
            self._run_operation(
                lambda progress, token: self.service.lifecycle(
                    profile_name,
                    action,
                    mod_name,
                    progress=progress,
                    token=token,
                )
            )

    def restore_backup(self, backup_id: str) -> None:
        active = self.service.active_profile()
        if active is None:
            return
        if self._confirm_destructive("Restore backup?", f"Restore {backup_id}?"):
            self._run_operation(
                lambda progress, token: self.service.restore_backup(
                    active.name, backup_id, progress=progress, token=token
                )
            )

    def _confirm_destructive(self, title: str, text: str) -> bool:
        if not self.settings.value("confirmDestructive", True, bool):
            return True
        return QMessageBox.question(self, title, text) == QMessageBox.StandardButton.Yes

    def show_settings(self) -> None:
        dialog = SettingsDialog(self.settings, self)
        dialog.open_logs_requested.connect(lambda: self.open_folder(log_directory()))
        if dialog.exec() == QDialog.DialogCode.Accepted:
            apply_theme(QApplication.instance(), dialog.theme.currentText())

    @staticmethod
    def open_folder(path) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
