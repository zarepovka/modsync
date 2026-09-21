import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QLabel, QMessageBox

from modsync import __version__
from modsync.gui.dialogs import (
    AboutDialog,
    AddModDialog,
    CreateProfileDialog,
    GameSelectionDialog,
    SettingsDialog,
    SwitchPreviewDialog,
)
from modsync.gui.logging_setup import redact
from modsync.gui.main_window import MainWindow
from modsync.gui.theme import apply_theme
from modsync.gui.widgets import BackupsPage, GamesPage, ModsPage, ProfilesPage
from modsync.gui.workers import OperationWorker
from modsync.models import BackupInfo, GameInstallation, Profile, SwitchPlan
from modsync.models import LifecycleReport
from modsync.profiles import ProfileStore
from modsync.services import (
    CancellationToken,
    GameView,
    ModSyncService,
    ModView,
    OperationCancelled,
    ProgressEvent,
    UpdatePreview,
)

def _profile(tmp_path: Path, name="Friends", count=2):
    directory = tmp_path / name
    return Profile(
        name,
        "valheim",
        tmp_path / "Valheim",
        "2026-01-01T00:00:00Z",
        "2026-01-01T00:00:00Z",
        tmp_path / "modpack.json",
        count,
        directory,
    )


def _installation(tmp_path: Path, path_name="Valheim"):
    return GameInstallation(
        "valheim",
        "Valheim",
        "steam",
        tmp_path / path_name,
        892970,
        "linux",
        tmp_path,
        True,
    )


class FakeService:
    def __init__(self, tmp_path: Path, *, profiles=None, error=None):
        self.items = list(profiles or [])
        self.active = self.items[0] if self.items else None
        self.error = error
        self.calls = []
        self.discovered = [_installation(tmp_path)]
        self.mods = []
        self.backups = []

    def list_profiles(self):
        if self.error:
            raise self.error
        return self.items

    def active_profile(self):
        return self.active

    def game_view(self):
        if not self.active:
            return None
        return GameView("valheim", "Valheim", "steam", self.active.install_directory, True, len(self.items))

    def list_mods(self, profile=None):
        return self.mods

    def list_backups(self, profile):
        return self.backups

    def discover_games(self, game=None, progress=None, token=None):
        self.calls.append(("discover", game))
        if progress:
            progress(ProgressEvent("discovery", "Searching…"))
        return self.discovered

    def create_profile(self, name, modpack, installation=None, manual_path=None):
        self.calls.append(("create", name, installation, manual_path))
        profile = _profile(Path(modpack).parent, name)
        self.items.append(profile)
        return profile

    def activate_profile(self, name):
        self.calls.append(("activate", name))
        self.active = next(item for item in self.items if item.name == name)
        return self.active

    def delete_profile(self, name):
        self.calls.append(("delete", name))
        self.items = [item for item in self.items if item.name != name]

    def switch_plan(self, target):
        self.calls.append(("switch-plan", target))
        return _switch_plan(target)

    def switch_profile(self, target, progress=None, token=None):
        self.calls.append(("switch", target))
        return object()

    def add_mod(self, profile, definition):
        self.calls.append(("add", profile, definition))
        return self.active

    def import_modpack(self, path):
        self.calls.append(("import", path))
        return SimpleNamespace(name="Pack", version="1", game="valheim", mods=(1, 2))

    def check_updates(self, profile, progress=None, token=None):
        self.calls.append(("updates", profile))
        return UpdatePreview((), 0, 0)

    def lifecycle_preview(self, profile, action, mod):
        self.calls.append(("preview", action, mod))
        from modsync.models import LifecycleReport

        return LifecycleReport(action, mod, dry_run=True, paths=("BepInEx/a.dll",))

    def lifecycle(self, profile, action, mod, progress=None, token=None):
        self.calls.append((action, mod))
        return object()

    def restore_backup(self, profile, backup, progress=None, token=None):
        self.calls.append(("restore", backup))
        return object()

    def install_profile(self, profile, progress=None, token=None):
        self.calls.append(("install", profile))
        from modsync.models import InstallReport

        return InstallReport()


def _switch_plan(target="Hardcore"):
    return SwitchPlan(
        "Friends",
        target,
        Path("/game"),
        (),
        (),
        (),
        (),
        (),
        (),
        (),
        (),
        (),
        (),
    )


@pytest.fixture
def gui_settings(tmp_path):
    return QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)


def test_gui_application_is_qt_widgets(qapp):
    assert isinstance(qapp, QApplication)
    assert __version__ == "1.0.0"


def test_main_window_creation(qtbot, tmp_path, gui_settings):
    window = MainWindow(FakeService(tmp_path), settings=gui_settings, show_onboarding=False)
    qtbot.addWidget(window)
    assert window.windowTitle() == "ModSync"
    assert window.pages.count() == 4


@pytest.mark.parametrize(("row", "object_name"), [(0, "gamesPage"), (1, "profilesPage"), (2, "modsPage"), (3, "backupsPage")])
def test_navigation(qtbot, tmp_path, gui_settings, row, object_name):
    window = MainWindow(FakeService(tmp_path), settings=gui_settings, show_onboarding=False)
    qtbot.addWidget(window)
    window.navigation.setCurrentRow(row)
    assert window.pages.currentWidget().objectName() == object_name


def test_empty_state_without_profiles(qtbot, tmp_path, gui_settings):
    window = MainWindow(FakeService(tmp_path), settings=gui_settings, show_onboarding=False)
    qtbot.addWidget(window)
    assert window.profiles_page.stack.currentIndex() == 0
    assert window.mods_page.stack.currentIndex() == 0


def test_discovered_game_display(qtbot, tmp_path):
    page = GamesPage()
    qtbot.addWidget(page)
    page.set_game(GameView("valheim", "Valheim", "steam", tmp_path, True, 3))
    assert page.game_name.text() == "Valheim"
    assert "Steam" in page.provider.text()
    assert "Installed" in page.loader.text()
    assert page.profile_count.text() == "Profiles: 3"


def test_multiple_game_selection(qtbot, tmp_path):
    dialog = GameSelectionDialog([_installation(tmp_path, "One"), _installation(tmp_path, "Two")])
    qtbot.addWidget(dialog)
    dialog.list.setCurrentRow(1)
    assert dialog.selected_installation().install_path.name == "Two"


def test_profile_list_marks_active(qtbot, tmp_path):
    page = ProfilesPage()
    qtbot.addWidget(page)
    page.set_profiles([_profile(tmp_path), _profile(tmp_path, "Hardcore", 9)], "Friends")
    assert page.list.count() == 2
    assert "ACTIVE" in page.list.item(0).text()
    assert "9 mods" in page.list.item(1).text()


def test_create_profile_dialog_values(qtbot, tmp_path):
    dialog = CreateProfileDialog(manual_path=tmp_path / "Valheim")
    qtbot.addWidget(dialog)
    dialog.name.setText("Friends")
    dialog.modpack.setText(str(tmp_path / "modpack.json"))
    values = dialog.values()
    assert values[0] == "Friends"
    assert values[1].name == "modpack.json"
    assert values[3].name == "Valheim"


def test_switch_preview_uses_switch_plan(qtbot):
    dialog = SwitchPreviewDialog(_switch_plan(), None)
    qtbot.addWidget(dialog)
    labels = " ".join(item.text() for item in dialog.findChildren(QLabel))
    assert dialog.windowTitle() == "Switch profile?"
    assert "Friends" in labels
    assert "Hardcore" in labels


def test_switch_invocation_uses_service(qtbot, tmp_path, gui_settings, monkeypatch):
    service = FakeService(tmp_path, profiles=[_profile(tmp_path), _profile(tmp_path, "Hardcore")])
    window = MainWindow(service, settings=gui_settings, show_onboarding=False)
    qtbot.addWidget(window)
    monkeypatch.setattr(SwitchPreviewDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    window.preview_switch("Hardcore")
    qtbot.waitUntil(lambda: window._worker is None, timeout=3000)
    assert ("switch-plan", "Hardcore") in service.calls
    assert ("switch", "Hardcore") in service.calls


def test_mod_list_columns(qtbot):
    page = ModsPage()
    qtbot.addWidget(page)
    page.set_mods([ModView("EpicLoot", "0.10.1", "thunderstore", "enabled", "explicit")])
    assert page.table.item(0, 0).text() == "EpicLoot"
    assert page.table.item(0, 2).text() == "thunderstore"


@pytest.mark.parametrize(
    ("source_index", "expected"),
    [
        (0, {"type": "thunderstore", "version": "latest"}),
        (1, {"type": "github", "release": "latest"}),
        (2, {"type": "direct"}),
    ],
)
def test_add_mod_source_definitions(qtbot, source_index, expected):
    dialog = AddModDialog()
    qtbot.addWidget(dialog)
    dialog.name.setText("Example")
    dialog.source.setCurrentIndex(source_index)
    if source_index == 0:
        fields = dialog.thunderstore_fields[1]
        fields["community"].setText("valheim")
        fields["namespace"].setText("Author")
        fields["package"].setText("Example")
    elif source_index == 1:
        fields = dialog.github_fields[1]
        fields["repository"].setText("owner/repository")
        fields["asset"].setText("mod.zip")
    else:
        dialog.direct_fields[1]["url"].setText("https://example.com/mod.zip")
    source = dialog.definition()["source"]
    assert all(source[key] == value for key, value in expected.items())


def test_uninstall_requires_confirmation(qtbot, tmp_path, gui_settings, monkeypatch):
    service = FakeService(tmp_path, profiles=[_profile(tmp_path)])
    window = MainWindow(service, settings=gui_settings, show_onboarding=False)
    qtbot.addWidget(window)
    monkeypatch.setattr(window, "_confirm_destructive", lambda title, text: False)
    preview = LifecycleReport("uninstall", "Example", dry_run=True, paths=("a.dll",))
    window._confirm_lifecycle("Friends", "uninstall", "Example", preview)
    assert ("uninstall", "Example") not in service.calls


def test_backup_restore_requires_confirmation(qtbot, tmp_path, gui_settings, monkeypatch):
    service = FakeService(tmp_path, profiles=[_profile(tmp_path)])
    window = MainWindow(service, settings=gui_settings, show_onboarding=False)
    qtbot.addWidget(window)
    monkeypatch.setattr(window, "_confirm_destructive", lambda title, text: False)
    window.restore_backup("backup-id")
    assert not any(call[0] == "restore" for call in service.calls)


@pytest.mark.parametrize(
    ("status", "reason"),
    [("enabled", "explicit"), ("disabled", "explicit"), ("enabled", "dependency")],
)
def test_mod_status_and_dependency_display(qtbot, status, reason):
    page = ModsPage()
    qtbot.addWidget(page)
    page.set_mods([ModView("Library", "1", "thunderstore", status, reason)])
    assert page.table.item(0, 3).text() == status
    assert page.table.item(0, 4).text() == reason


@pytest.mark.parametrize("action", ["enable", "disable", "uninstall"])
def test_mod_actions_emit_selected_mod(qtbot, action):
    page = ModsPage()
    qtbot.addWidget(page)
    page.set_mods([ModView("EpicLoot", "1", "direct", "enabled", "explicit")])
    page.table.selectRow(0)
    with qtbot.waitSignal(page.action_requested) as blocker:
        page._action(action)
    assert blocker.args == [action, "EpicLoot"]


def test_backup_list_and_restore_signal(qtbot):
    page = BackupsPage()
    qtbot.addWidget(page)
    backup = BackupInfo("20260920T180000Z-abcdef", "2026-09-20T18:00:00Z", "Pack", "1", "update", 14)
    page.set_backups("Friends", [backup])
    page.table.selectRow(0)
    with qtbot.waitSignal(page.restore_requested) as blocker:
        page._restore()
    assert blocker.args == [backup.backup_id]


def test_import_modpack_preview(tmp_path):
    game = tmp_path / "Valheim"
    document = tmp_path / "modpack.json"
    document.write_text(json.dumps({"name": "Pack", "version": "1", "game": "valheim", "mods": []}), encoding="utf-8")
    service = ModSyncService(profile_store=ProfileStore(tmp_path / "data"))
    preview = service.import_modpack(document, install_directory=game)
    assert preview.name == "Pack"
    assert preview.install_directory == game.resolve()


def test_import_modpack_ui_shows_preview(qtbot, tmp_path, gui_settings, monkeypatch):
    service = FakeService(tmp_path)
    window = MainWindow(service, settings=gui_settings, show_onboarding=False)
    qtbot.addWidget(window)
    path = tmp_path / "modpack.json"
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args, **kwargs: (str(path), ""))
    questions = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: questions.append(args[2]) or QMessageBox.StandardButton.No,
    )
    window.import_modpack()
    assert ("import", path) in service.calls
    assert "Mods: 2" in questions[0]


def test_invalid_modpack_is_rejected(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{", encoding="utf-8")
    with pytest.raises(Exception, match="Invalid JSON"):
        ModSyncService(profile_store=ProfileStore(tmp_path / "data")).import_modpack(
            path, install_directory=tmp_path
        )


def test_add_mod_uses_existing_config_validation_and_atomic_storage(tmp_path):
    game = tmp_path / "Valheim"
    document = tmp_path / "modpack.json"
    document.write_text(
        json.dumps(
            {
                "name": "Pack",
                "version": "1",
                "game": "valheim",
                "install_directory": str(game),
                "mods": [],
            }
        ),
        encoding="utf-8",
    )
    store = ProfileStore(tmp_path / "data")
    store.create("Friends", document)
    service = ModSyncService(profile_store=store)
    service.add_mod(
        "Friends",
        {
            "name": "Example",
            "source": {"type": "direct", "url": "https://example.com/mod.zip"},
        },
    )
    assert [mod.name for mod in store.load_modpack("Friends").mods] == ["Example"]


def test_invalid_added_mod_does_not_change_stored_modpack(tmp_path):
    game = tmp_path / "Valheim"
    document = tmp_path / "modpack.json"
    document.write_text(
        json.dumps(
            {
                "name": "Pack",
                "version": "1",
                "game": "valheim",
                "install_directory": str(game),
                "mods": [],
            }
        ),
        encoding="utf-8",
    )
    store = ProfileStore(tmp_path / "data")
    store.create("Friends", document)
    service = ModSyncService(profile_store=store)
    with pytest.raises(Exception, match="valid HTTP"):
        service.add_mod(
            "Friends",
            {"name": "Example", "source": {"type": "direct", "url": "file:///tmp/mod"}},
        )
    assert store.load_modpack("Friends").mods == ()


def test_update_preview_available():
    assert UpdatePreview(("EpicLoot",), 2, 4).available


def test_update_preview_empty():
    assert not UpdatePreview((), 2, 0).available


def test_progress_event_fields():
    event = ProgressEvent("download", "Downloading…", 2, 10, True)
    assert (event.current, event.total, event.cancellable) == (2, 10, True)


def test_error_dialog_hides_traceback_by_default(qtbot, tmp_path, gui_settings, monkeypatch):
    captured = []
    monkeypatch.setattr(QMessageBox, "exec", lambda self: captured.append(self))
    window = MainWindow(FakeService(tmp_path), settings=gui_settings, show_onboarding=False)
    qtbot.addWidget(window)
    window.present_error("Installation failed", "Safe summary\nprivate technical details")
    assert captured[0].informativeText() == "Safe summary"
    assert "private technical details" in captured[0].detailedText()


def test_rollback_success_error_is_explained(qtbot, tmp_path, gui_settings, monkeypatch):
    captured = []
    monkeypatch.setattr(QMessageBox, "exec", lambda self: captured.append(self))
    window = MainWindow(FakeService(tmp_path), settings=gui_settings, show_onboarding=False)
    qtbot.addWidget(window)
    window.present_error("Installation failed", "Update failed; rolled back safely")
    assert "restored successfully" in captured[0].informativeText()


def test_worker_success_signal(qtbot):
    worker = OperationWorker(lambda progress, token: "done")
    with qtbot.waitSignal(worker.signals.result) as blocker:
        worker.run()
    assert blocker.args == ["done"]


def test_worker_exception_signal(qtbot):
    def fail(progress, token):
        raise RuntimeError("boom")

    worker = OperationWorker(fail)
    with qtbot.waitSignal(worker.signals.error) as blocker:
        worker.run()
    assert isinstance(blocker.args[0], RuntimeError)


def test_safe_cancellation_before_mutation():
    token = CancellationToken()
    assert token.cancel()
    with pytest.raises(OperationCancelled):
        token.checkpoint()


def test_unsafe_cancellation_is_prevented_after_mutation():
    token = CancellationToken()
    token.begin_mutation()
    assert token.cancel() is False
    assert token.mutation_started


def test_settings_dialog_reads_and_saves(qtbot, gui_settings):
    dialog = SettingsDialog(gui_settings)
    qtbot.addWidget(dialog)
    dialog.theme.setCurrentText("Dark")
    dialog.confirm.setChecked(False)
    dialog._save()
    assert gui_settings.value("theme") == "Dark"
    assert gui_settings.value("confirmDestructive", True, bool) is False


@pytest.mark.parametrize("theme", ["System", "Light", "Dark"])
def test_theme_setting(qapp, theme):
    apply_theme(qapp, theme)
    assert qapp.palette() is not None


@pytest.mark.parametrize(
    "secret",
    [
        "Authorization: Bearer abc123",
        "authorization=token-value",
        "MODSYNC_GITHUB_TOKEN=secret",
        "ghp_abcdefghijklmnopqrstuvwxyz",
        "github_pat_abc_123",
    ],
)
def test_log_redaction(secret):
    assert "secret" not in redact(secret).casefold()
    assert "abc123" not in redact(secret)
    assert "[REDACTED]" in redact(secret)


def test_about_dialog_uses_single_version_source(qtbot):
    dialog = AboutDialog()
    qtbot.addWidget(dialog)
    text = " ".join(label.text() for label in dialog.findChildren(QLabel))
    assert dialog.windowTitle() == "About ModSync"
    assert __version__ in text


def test_main_window_has_no_installer_or_backup_manager_imports():
    source = inspect.getsource(__import__("modsync.gui.main_window", fromlist=["MainWindow"]))
    assert "from ..installer" not in source
    assert "from ..backup" not in source


def test_gui_uses_services_layer():
    source = inspect.getsource(__import__("modsync.gui.main_window", fromlist=["MainWindow"]))
    assert "ModSyncService" in source


def test_cli_uses_services_layer():
    import modsync.cli

    assert "ModSyncService" in inspect.getsource(modsync.cli)


def test_startup_without_steam(qtbot, tmp_path, gui_settings):
    service = FakeService(tmp_path)
    service.discovered = []
    window = MainWindow(service, settings=gui_settings, show_onboarding=False)
    qtbot.addWidget(window)
    assert window.games_page.stack.currentIndex() == 0


def test_startup_without_profiles(qtbot, tmp_path, gui_settings):
    window = MainWindow(FakeService(tmp_path), settings=gui_settings, show_onboarding=False)
    qtbot.addWidget(window)
    assert window.profiles_page.list.count() == 0


def test_corrupt_profile_metadata_is_presented_as_error(qtbot, tmp_path, gui_settings, monkeypatch):
    shown = []
    monkeypatch.setattr(MainWindow, "present_error", lambda self, title, error: shown.append((title, str(error))))
    window = MainWindow(FakeService(tmp_path, error=RuntimeError("corrupt metadata")), settings=gui_settings, show_onboarding=False)
    qtbot.addWidget(window)
    assert "corrupt metadata" in shown[0][1]


def test_main_window_refresh_smoke(qtbot, tmp_path, gui_settings):
    service = FakeService(tmp_path, profiles=[_profile(tmp_path)])
    service.mods = [ModView("Example", "1", "direct", "enabled", "explicit")]
    service.backups = [BackupInfo("20260920T180000Z-abcdef", "date", "Pack", "1", "update", 1)]
    window = MainWindow(service, settings=gui_settings, show_onboarding=False)
    qtbot.addWidget(window)
    window.refresh()
    assert window.mods_page.table.rowCount() == 1
    assert window.backups_page.table.rowCount() == 1
