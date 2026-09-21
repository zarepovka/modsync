"""Small dialogs used by profile, source, discovery, and switch workflows."""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Qt, QSettings, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ... import __version__
from ...models import GameInstallation, SwitchPlan


class OnboardingDialog(QDialog):
    discover_requested = Signal()
    manual_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("onboardingDialog")
        self.setWindowTitle("Welcome to ModSync")
        self.setModal(True)
        layout = QVBoxLayout(self)
        title = QLabel("Welcome to ModSync")
        title.setStyleSheet("font-size: 24px; font-weight: 600;")
        body = QLabel("Find Valheim and create your first profile.")
        find = QPushButton("Find installed games")
        manual = QPushButton("Choose Folder Manually")
        find.clicked.connect(self.discover_requested)
        manual.clicked.connect(self.manual_requested)
        layout.addWidget(title)
        layout.addWidget(body)
        layout.addWidget(find)
        layout.addWidget(manual)


class GameSelectionDialog(QDialog):
    def __init__(self, installations: list[GameInstallation], parent=None) -> None:
        super().__init__(parent)
        self.installations = installations
        self.setWindowTitle("Select game installation")
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Multiple Valheim installations were found:"))
        self.list = QListWidget()
        for index, installation in enumerate(installations):
            item = QListWidgetItem(
                f"{installation.display_name}\n{installation.provider.title()} • {installation.install_path}"
            )
            item.setData(Qt.ItemDataRole.UserRole, index)
            self.list.addItem(item)
        if installations:
            self.list.setCurrentRow(0)
        layout.addWidget(self.list)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_installation(self) -> GameInstallation | None:
        item = self.list.currentItem()
        return self.installations[item.data(Qt.ItemDataRole.UserRole)] if item else None


class CreateProfileDialog(QDialog):
    def __init__(
        self,
        parent=None,
        *,
        installation: GameInstallation | None = None,
        manual_path: Path | None = None,
    ) -> None:
        super().__init__(parent)
        self.installation = installation
        self.setWindowTitle("Create Profile")
        form = QFormLayout(self)
        self.name = QLineEdit()
        self.modpack = QLineEdit()
        browse_modpack = QPushButton("Browse…")
        browse_modpack.clicked.connect(self._browse_modpack)
        modpack_row = QHBoxLayout()
        modpack_row.addWidget(self.modpack)
        modpack_row.addWidget(browse_modpack)
        self.discover = QCheckBox("Find game automatically")
        self.discover.setChecked(installation is not None or manual_path is None)
        self.path = QLineEdit(str(manual_path or (installation.install_path if installation else "")))
        browse_path = QPushButton("Browse…")
        browse_path.clicked.connect(self._browse_path)
        path_row = QHBoxLayout()
        path_row.addWidget(self.path)
        path_row.addWidget(browse_path)
        form.addRow("Name", self.name)
        form.addRow("modpack.json", modpack_row)
        form.addRow("Game", self.discover)
        form.addRow("Game folder", path_row)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _browse_modpack(self) -> None:
        value, _ = QFileDialog.getOpenFileName(self, "Choose modpack", filter="JSON (*.json)")
        if value:
            self.modpack.setText(value)

    def _browse_path(self) -> None:
        value = QFileDialog.getExistingDirectory(self, "Choose game folder")
        if value:
            self.path.setText(value)
            self.discover.setChecked(False)

    def values(self) -> tuple[str, Path, bool, Path | None]:
        path = Path(self.path.text()) if self.path.text().strip() else None
        return self.name.text().strip(), Path(self.modpack.text()), self.discover.isChecked(), path


class AddModDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add Mod")
        layout = QVBoxLayout(self)
        common = QFormLayout()
        self.name = QLineEdit()
        self.source = QComboBox()
        self.source.addItems(["Thunderstore", "GitHub Release", "Direct URL"])
        common.addRow("Name", self.name)
        common.addRow("Source", self.source)
        layout.addLayout(common)
        self.pages = QStackedWidget()
        self.thunderstore_fields = self._page(
            ("Community", "community"),
            ("Namespace", "namespace"),
            ("Package", "package"),
            ("Version", "version"),
        )
        self.thunderstore_fields[1]["version"].setText("latest")
        self.github_fields = self._page(
            ("owner/repository", "repository"),
            ("Release", "release"),
            ("Asset", "asset"),
        )
        self.github_fields[1]["release"].setText("latest")
        self.direct_fields = self._page(
            ("URL", "url"), ("Version (optional)", "version"), ("SHA256 (optional)", "sha256")
        )
        for page, _ in (
            self.thunderstore_fields,
            self.github_fields,
            self.direct_fields,
        ):
            self.pages.addWidget(page)
        self.source.currentIndexChanged.connect(self.pages.setCurrentIndex)
        layout.addWidget(self.pages)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @staticmethod
    def _page(*fields: tuple[str, str]):
        page = QWidget()
        form = QFormLayout(page)
        values: dict[str, QLineEdit] = {}
        for label, key in fields:
            widget = QLineEdit()
            values[key] = widget
            form.addRow(label, widget)
        return page, values

    def definition(self) -> dict[str, object]:
        name = self.name.text().strip()
        index = self.source.currentIndex()
        if index == 0:
            fields = self.thunderstore_fields[1]
            source = {
                "type": "thunderstore",
                **{key: value.text().strip() for key, value in fields.items()},
            }
        elif index == 1:
            fields = self.github_fields[1]
            source = {
                "type": "github",
                **{key: value.text().strip() for key, value in fields.items()},
            }
        else:
            fields = self.direct_fields[1]
            source = {"type": "direct", "url": fields["url"].text().strip()}
            version = fields["version"].text().strip()
            if version:
                source["version"] = version
            definition: dict[str, object] = {"name": name, "source": source}
            checksum = fields["sha256"].text().strip()
            if checksum:
                definition["sha256"] = checksum
            return definition
        return {"name": name, "source": source}


class SwitchPreviewDialog(QDialog):
    def __init__(self, plan: SwitchPlan, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Switch profile?")
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"{plan.source_profile} → {plan.target_profile}"))
        layout.addWidget(
            QLabel(
                f"Keep: {len(plan.keep)} files\n"
                f"Remove: {len(plan.remove)} files\n"
                f"Install: {len(plan.install)} files\n"
                f"Restore configs: {len(plan.restore_configs)}"
            )
        )
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Switch")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class SettingsDialog(QDialog):
    open_logs_requested = Signal()

    def __init__(self, settings: QSettings, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("Settings")
        form = QFormLayout(self)
        self.theme = QComboBox()
        self.theme.addItems(["System", "Light", "Dark"])
        self.theme.setCurrentText(str(settings.value("theme", "System")))
        self.confirm = QCheckBox()
        self.confirm.setChecked(settings.value("confirmDestructive", True, bool))
        token = "GitHub token detected" if os.environ.get("MODSYNC_GITHUB_TOKEN") else "Not configured"
        token_label = QLabel(token + "\nUse the MODSYNC_GITHUB_TOKEN environment variable.")
        logs = QPushButton("Open Logs Folder")
        logs.clicked.connect(self.open_logs_requested)
        form.addRow("Theme", self.theme)
        form.addRow("Confirm destructive actions", self.confirm)
        form.addRow("GitHub token", token_label)
        form.addRow(logs)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _save(self) -> None:
        self.settings.setValue("theme", self.theme.currentText())
        self.settings.setValue("confirmDestructive", self.confirm.isChecked())
        self.accept()


class AboutDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("About ModSync")
        layout = QVBoxLayout(self)
        label = QLabel(
            f"<h2>ModSync</h2><p>Version {__version__}</p>"
            '<p>MIT License</p><p><a href="https://github.com/zarepovka/modsync">'
            "GitHub: zarepovka/modsync</a></p>"
        )
        label.setOpenExternalLinks(True)
        layout.addWidget(label)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
