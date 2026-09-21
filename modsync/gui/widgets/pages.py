"""Focused widgets for the four primary desktop views."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QStackedLayout,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...models import BackupInfo, Profile
from ...services import GameView, ModView
from .common import EmptyState


def _button_row(*buttons: QPushButton) -> QHBoxLayout:
    row = QHBoxLayout()
    for button in buttons:
        row.addWidget(button)
    row.addStretch()
    return row


class GamesPage(QWidget):
    rediscover_requested = Signal()
    create_requested = Signal()
    open_folder_requested = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("gamesPage")
        layout = QVBoxLayout(self)
        title = QLabel("Games")
        title.setStyleSheet("font-size: 26px; font-weight: 600;")
        layout.addWidget(title)
        self.stack = QStackedLayout()
        self.empty = EmptyState(
            "No games configured", "Find Valheim automatically or choose its folder.", "Find installed games"
        )
        self.empty.action_requested.connect(self.rediscover_requested)
        self.details = QWidget()
        details_layout = QVBoxLayout(self.details)
        self.game_name = QLabel()
        self.game_name.setStyleSheet("font-size: 22px; font-weight: 600;")
        self.provider = QLabel()
        self.path = QLabel()
        self.path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.loader = QLabel()
        self.profile_count = QLabel()
        for widget in (self.game_name, self.provider, self.path, self.loader, self.profile_count):
            details_layout.addWidget(widget)
        self.open_button = QPushButton("Open game folder")
        self.rediscover_button = QPushButton("Rediscover")
        self.create_button = QPushButton("Create profile")
        details_layout.addLayout(
            _button_row(self.open_button, self.rediscover_button, self.create_button)
        )
        details_layout.addStretch()
        self.open_button.clicked.connect(lambda: self.open_folder_requested.emit(self.path.text()))
        self.rediscover_button.clicked.connect(self.rediscover_requested)
        self.create_button.clicked.connect(self.create_requested)
        self.stack.addWidget(self.empty)
        self.stack.addWidget(self.details)
        layout.addLayout(self.stack)

    def set_game(self, game: GameView | None) -> None:
        if game is None:
            self.stack.setCurrentWidget(self.empty)
            return
        self.game_name.setText(game.display_name)
        self.provider.setText(f"Platform: {game.provider.title()}")
        self.path.setText(str(game.install_path))
        self.loader.setText(
            "BepInEx: Installed" if game.bep_in_ex_installed else "BepInEx: Not detected"
        )
        self.profile_count.setText(f"Profiles: {game.profile_count}")
        self.stack.setCurrentWidget(self.details)


class ProfilesPage(QWidget):
    create_requested = Signal()
    import_requested = Signal()
    switch_requested = Signal(str)
    delete_requested = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("profilesPage")
        layout = QVBoxLayout(self)
        title = QLabel("Profiles")
        title.setStyleSheet("font-size: 26px; font-weight: 600;")
        layout.addWidget(title)
        self.stack = QStackedLayout()
        self.empty = EmptyState(
            "No profiles yet",
            "Create your first profile to start managing mods.",
            "Create profile",
        )
        self.empty.action_requested.connect(self.create_requested)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        self.list = QListWidget()
        self.list.setObjectName("profileList")
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        content_layout.addWidget(self.list)
        self.create_button = QPushButton("Create")
        self.import_button = QPushButton("Import Modpack")
        self.switch_button = QPushButton("Switch")
        self.delete_button = QPushButton("Delete")
        content_layout.addLayout(
            _button_row(
                self.create_button,
                self.import_button,
                self.switch_button,
                self.delete_button,
            )
        )
        self.create_button.clicked.connect(self.create_requested)
        self.import_button.clicked.connect(self.import_requested)
        self.switch_button.clicked.connect(lambda: self._emit_selected(self.switch_requested))
        self.delete_button.clicked.connect(lambda: self._emit_selected(self.delete_requested))
        self.stack.addWidget(self.empty)
        self.stack.addWidget(content)
        layout.addLayout(self.stack)

    def _emit_selected(self, signal) -> None:
        item = self.list.currentItem()
        if item is not None:
            signal.emit(item.data(Qt.ItemDataRole.UserRole))

    def set_profiles(self, profiles: list[Profile], active: str | None) -> None:
        self.list.clear()
        for profile in profiles:
            suffix = "  • ACTIVE" if profile.name == active else ""
            list_item = QListWidgetItem(
                f"{profile.name}{suffix}\n{profile.mod_count} mods • {profile.game}"
            )
            list_item.setData(Qt.ItemDataRole.UserRole, profile.name)
            self.list.addItem(list_item)
        self.stack.setCurrentIndex(1 if profiles else 0)


class ModsPage(QWidget):
    add_requested = Signal()
    updates_requested = Signal()
    action_requested = Signal(str, str)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("modsPage")
        layout = QVBoxLayout(self)
        title = QLabel("Mods")
        title.setStyleSheet("font-size: 26px; font-weight: 600;")
        layout.addWidget(title)
        self.empty = EmptyState(
            "No mods installed", "Add a source or import a modpack to get started.", "Add Mod"
        )
        self.empty.action_requested.connect(self.add_requested)
        self.table = QTableWidget(0, 5)
        self.table.setObjectName("modsTable")
        self.table.setHorizontalHeaderLabels(
            ["Name", "Version", "Source", "Status", "Reason"]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.stack = QStackedLayout()
        self.stack.addWidget(self.empty)
        self.stack.addWidget(self.table)
        layout.addLayout(self.stack)
        self.add_button = QPushButton("Add Mod")
        self.update_button = QPushButton("Check for Updates")
        self.enable_button = QPushButton("Enable")
        self.disable_button = QPushButton("Disable")
        self.uninstall_button = QPushButton("Uninstall")
        layout.addLayout(
            _button_row(
                self.add_button,
                self.update_button,
                self.enable_button,
                self.disable_button,
                self.uninstall_button,
            )
        )
        self.add_button.clicked.connect(self.add_requested)
        self.update_button.clicked.connect(self.updates_requested)
        self.enable_button.clicked.connect(lambda: self._action("enable"))
        self.disable_button.clicked.connect(lambda: self._action("disable"))
        self.uninstall_button.clicked.connect(lambda: self._action("uninstall"))

    def _action(self, action: str) -> None:
        row = self.table.currentRow()
        if row >= 0:
            self.action_requested.emit(action, self.table.item(row, 0).text())

    def set_mods(self, mods: list[ModView]) -> None:
        self.table.setRowCount(len(mods))
        for row, mod in enumerate(mods):
            for column, value in enumerate(
                (mod.name, mod.version, mod.source, mod.status, mod.install_reason)
            ):
                self.table.setItem(row, column, QTableWidgetItem(value))
        self.stack.setCurrentIndex(1 if mods else 0)


class BackupsPage(QWidget):
    restore_requested = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("backupsPage")
        layout = QVBoxLayout(self)
        title = QLabel("Backups")
        title.setStyleSheet("font-size: 26px; font-weight: 600;")
        layout.addWidget(title)
        self.empty = EmptyState("No backups yet", "Backups appear before protected changes.")
        self.table = QTableWidget(0, 4)
        self.table.setObjectName("backupsTable")
        self.table.setHorizontalHeaderLabels(["Date", "Profile", "Reason", "Files"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.stack = QStackedLayout()
        self.stack.addWidget(self.empty)
        self.stack.addWidget(self.table)
        layout.addLayout(self.stack)
        self.restore_button = QPushButton("Restore")
        self.restore_button.clicked.connect(self._restore)
        layout.addLayout(_button_row(self.restore_button))

    def _restore(self) -> None:
        row = self.table.currentRow()
        if row >= 0:
            self.restore_requested.emit(self.table.item(row, 0).data(Qt.ItemDataRole.UserRole))

    def set_backups(self, profile_name: str, backups: list[BackupInfo]) -> None:
        self.table.setRowCount(len(backups))
        for row, backup in enumerate(backups):
            values = (backup.created_at, profile_name, backup.reason, str(backup.file_count))
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, backup.backup_id)
                self.table.setItem(row, column, item)
        self.stack.setCurrentIndex(1 if backups else 0)
