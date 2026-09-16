import json
import os
import re
import shutil
import zipfile
from pathlib import Path

import pytest

from modsync.backup import BACKUP_DIRECTORY, BackupManager
from modsync.cli import main
from modsync.exceptions import (
    BackupIntegrityError,
    BackupNotFoundError,
    InstallError,
    RollbackError,
)
from modsync.hashing import sha256_file
from modsync.installer import Installer
from modsync.models import Mod, Modpack
from modsync.state import load_state, save_state


class CopyDownloader:
    def __init__(self, sources: dict[str, Path | Exception]):
        self.sources = sources

    def download(self, mod, destination, progress=None):
        source = self.sources[mod.name]
        if isinstance(source, Exception):
            raise source
        target = destination / source.name
        shutil.copy2(source, target)
        return target


class ApplyThenFailInstaller(Installer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.should_fail = True

    def apply_prepared_mod(self, root, prepared, displaced_root):
        super().apply_prepared_mod(root, prepared, displaced_root)
        if self.should_fail:
            self.should_fail = False
            raise InstallError("injected apply failure")


class RestoreFailingBackupManager(BackupManager):
    def restore(self, backup_id):
        raise RollbackError("injected rollback failure")


def make_pack(tmp_path, *, pack_version="2.0", mod_version="2.0", name="ExampleMod"):
    mod = Mod(name=name, version=mod_version, url=f"https://example.com/{name}.zip")
    return Modpack(
        name="Test Pack",
        version=pack_version,
        description="",
        game="Test Game",
        install_directory=tmp_path / "mods",
        mods=(mod,),
        source_path=tmp_path / "modpack.json",
    )


def seed_installed(pack, content=b"old version", *, pack_version="1.0", mod_version="1.0"):
    installed = pack.install_directory / pack.mods[0].name / "plugin.txt"
    installed.parent.mkdir(parents=True, exist_ok=True)
    installed.write_bytes(content)
    state = {
        "schema_version": 1,
        "modpack": {"name": pack.name, "version": pack_version},
        "mods": {
            pack.mods[0].name: {
                "version": mod_version,
                "source_sha256": "a" * 64,
                "directory": pack.mods[0].name,
                "files": {"plugin.txt": sha256_file(installed)},
            }
        },
    }
    save_state(pack.install_directory, state)
    return installed, state


def make_zip(path, content):
    with zipfile.ZipFile(path, "w") as bundle:
        bundle.writestr("plugin.txt", content)
    return path


def backup_paths(pack, backup_id):
    directory = pack.install_directory / BACKUP_DIRECTORY / backup_id
    return directory, directory / "metadata.json", directory / "state.json"


def test_create_backup_writes_metadata_state_and_hashes(tmp_path):
    pack = make_pack(tmp_path)
    installed, state = seed_installed(pack)

    backup = BackupManager(pack).create(pack.mods, state, reason="update")
    directory, metadata_path, state_path = backup_paths(pack, backup.backup_id)
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))

    assert re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f]{6}", backup.backup_id)
    assert metadata["schema_version"] == 1
    assert metadata["backup_id"] == backup.backup_id
    assert metadata["modpack"] == {"name": "Test Pack", "version": "1.0"}
    assert metadata["reason"] == "update"
    assert metadata["affected_mods"][0]["version"] == "1.0"
    assert metadata["saved_files"] == [
        {
            "path": "ExampleMod/plugin.txt",
            "sha256": sha256_file(installed),
        }
    ]
    assert metadata["state_sha256"] == sha256_file(state_path)
    assert (directory / "files" / "ExampleMod" / "plugin.txt").read_bytes() == b"old version"


def test_list_backups_returns_multiple_backups_newest_first(tmp_path):
    pack = make_pack(tmp_path)
    _, state = seed_installed(pack)
    manager = BackupManager(pack)

    created = [manager.create(pack.mods, state) for _ in range(3)]
    listed = manager.list_backups()

    assert len(listed) == 3
    assert {item.backup_id for item in listed} == {item.backup_id for item in created}
    assert all(item.file_count == 1 for item in listed)
    assert len({item.backup_id for item in created}) == 3


def test_restore_backup_restores_files_and_state(tmp_path):
    pack = make_pack(tmp_path)
    installed, state = seed_installed(pack)
    manager = BackupManager(pack)
    backup = manager.create(pack.mods, state)
    installed.write_bytes(b"new version")
    changed_state = dict(state)
    changed_state["modpack"] = {"name": pack.name, "version": "2.0"}
    changed_state["mods"] = {
        "ExampleMod": {
            **state["mods"]["ExampleMod"],
            "version": "2.0",
            "files": {"plugin.txt": sha256_file(installed)},
        }
    }
    save_state(pack.install_directory, changed_state)

    report = manager.restore(backup.backup_id)

    assert report.restored_files == 1
    assert report.state_restored
    assert installed.read_bytes() == b"old version"
    assert load_state(pack.install_directory) == state


def test_restore_old_backup_preserves_unrelated_later_mods(tmp_path):
    mod_a = Mod("ModA", "2", "https://example.com/a.zip")
    mod_b = Mod("ModB", "3", "https://example.com/b.zip")
    base = make_pack(tmp_path)
    pack = Modpack(
        base.name,
        "2",
        "",
        base.game,
        base.install_directory,
        (mod_a, mod_b),
        base.source_path,
    )
    file_a = pack.install_directory / "ModA" / "plugin.txt"
    file_b = pack.install_directory / "ModB" / "plugin.txt"
    file_a.parent.mkdir(parents=True)
    file_b.parent.mkdir(parents=True)
    file_a.write_bytes(b"a1")
    file_b.write_bytes(b"b1")
    old_state = {
        "schema_version": 1,
        "modpack": {"name": pack.name, "version": "1"},
        "mods": {
            "ModA": {"version": "1", "files": {"plugin.txt": sha256_file(file_a)}},
            "ModB": {"version": "1", "files": {"plugin.txt": sha256_file(file_b)}},
        },
    }
    save_state(pack.install_directory, old_state)
    manager = BackupManager(pack)
    backup = manager.create((mod_a,), old_state)

    file_a.write_bytes(b"a2")
    file_b.write_bytes(b"b3")
    current_state = {
        "schema_version": 1,
        "modpack": {"name": pack.name, "version": "3"},
        "mods": {
            "ModA": {"version": "2", "files": {"plugin.txt": sha256_file(file_a)}},
            "ModB": {"version": "3", "files": {"plugin.txt": sha256_file(file_b)}},
        },
    }
    save_state(pack.install_directory, current_state)

    manager.restore(backup.backup_id)
    restored_state = load_state(pack.install_directory)

    assert file_a.read_bytes() == b"a1"
    assert file_b.read_bytes() == b"b3"
    assert restored_state["mods"]["ModA"]["version"] == "1"
    assert restored_state["mods"]["ModB"]["version"] == "3"
    assert restored_state["modpack"]["version"] == "3"


def test_restore_rejects_corrupted_backup_file_before_changes(tmp_path):
    pack = make_pack(tmp_path)
    installed, state = seed_installed(pack)
    manager = BackupManager(pack)
    backup = manager.create(pack.mods, state)
    backup_file = (
        pack.install_directory
        / BACKUP_DIRECTORY
        / backup.backup_id
        / "files"
        / "ExampleMod"
        / "plugin.txt"
    )
    backup_file.write_bytes(b"corrupted backup")
    installed.write_bytes(b"current installation")

    with pytest.raises(BackupIntegrityError, match="Checksum mismatch"):
        manager.restore(backup.backup_id)

    assert installed.read_bytes() == b"current installation"


def test_restore_rejects_corrupted_state_snapshot(tmp_path):
    pack = make_pack(tmp_path)
    installed, state = seed_installed(pack)
    manager = BackupManager(pack)
    backup = manager.create(pack.mods, state)
    _, _, state_path = backup_paths(pack, backup.backup_id)
    state_path.write_text("{}", encoding="utf-8")
    installed.write_bytes(b"current installation")

    with pytest.raises(BackupIntegrityError, match="State checksum mismatch"):
        manager.restore(backup.backup_id)

    assert installed.read_bytes() == b"current installation"


def test_list_rejects_corrupted_metadata(tmp_path):
    pack = make_pack(tmp_path)
    _, state = seed_installed(pack)
    manager = BackupManager(pack)
    backup = manager.create(pack.mods, state)
    _, metadata_path, _ = backup_paths(pack, backup.backup_id)
    metadata_path.write_text("not json", encoding="utf-8")

    with pytest.raises(BackupIntegrityError, match="Invalid metadata"):
        manager.list_backups()


def test_restore_reports_missing_backup(tmp_path):
    pack = make_pack(tmp_path)

    with pytest.raises(BackupNotFoundError, match="Backup not found"):
        BackupManager(pack).restore("20260916T153012Z-a4f21c")


def test_restore_rejects_path_traversal_in_metadata(tmp_path):
    pack = make_pack(tmp_path)
    installed, state = seed_installed(pack)
    manager = BackupManager(pack)
    backup = manager.create(pack.mods, state)
    _, metadata_path, _ = backup_paths(pack, backup.backup_id)
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["saved_files"][0]["path"] = "../escaped.txt"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    installed.write_bytes(b"current")

    with pytest.raises(BackupIntegrityError, match="Unsafe saved file path"):
        manager.restore(backup.backup_id)

    assert installed.read_bytes() == b"current"
    assert not (tmp_path / "escaped.txt").exists()


def test_restore_rejects_symlink_in_backup(tmp_path):
    pack = make_pack(tmp_path)
    _, state = seed_installed(pack)
    manager = BackupManager(pack)
    backup = manager.create(pack.mods, state)
    backup_file = (
        pack.install_directory
        / BACKUP_DIRECTORY
        / backup.backup_id
        / "files"
        / "ExampleMod"
        / "plugin.txt"
    )
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside")
    backup_file.unlink()
    try:
        backup_file.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("Symbolic links are unavailable on this platform")

    with pytest.raises(BackupIntegrityError, match="not a regular file"):
        manager.restore(backup.backup_id)


def test_restore_rejects_hardlink_in_backup_when_supported(tmp_path):
    pack = make_pack(tmp_path)
    _, state = seed_installed(pack)
    manager = BackupManager(pack)
    backup = manager.create(pack.mods, state)
    backup_file = (
        pack.install_directory
        / BACKUP_DIRECTORY
        / backup.backup_id
        / "files"
        / "ExampleMod"
        / "plugin.txt"
    )
    linked = tmp_path / "linked.txt"
    try:
        os.link(backup_file, linked)
    except (OSError, NotImplementedError):
        pytest.skip("Hard links are unavailable on this platform")

    with pytest.raises(BackupIntegrityError, match="Hard links"):
        manager.restore(backup.backup_id)


def test_retention_keeps_five_newest_backups(tmp_path):
    pack = make_pack(tmp_path)
    _, state = seed_installed(pack)
    manager = BackupManager(pack)
    for _ in range(7):
        manager.create(pack.mods, state)

    removed = manager.prune()

    assert len(removed) == 2
    assert len(manager.list_backups()) == 5


def test_successful_update_creates_backup_and_updates_state(tmp_path):
    archive_v1 = make_zip(tmp_path / "v1.zip", b"old")
    archive_v2 = make_zip(tmp_path / "v2.zip", b"new")
    pack_v1 = make_pack(tmp_path, pack_version="1.0", mod_version="1.0")
    first_mod = Mod(
        name="ExampleMod",
        version="1.0",
        url="https://example.com/v1.zip",
        sha256=sha256_file(archive_v1),
    )
    pack_v1 = Modpack(
        pack_v1.name,
        pack_v1.version,
        pack_v1.description,
        pack_v1.game,
        pack_v1.install_directory,
        (first_mod,),
        pack_v1.source_path,
    )
    Installer(CopyDownloader({"ExampleMod": archive_v1})).install_modpack(pack_v1)
    updated_mod = Mod(
        name="ExampleMod",
        version="2.0",
        url="https://example.com/v2.zip",
        sha256=sha256_file(archive_v2),
    )
    pack_v2 = Modpack(
        pack_v1.name,
        "2.0",
        "",
        pack_v1.game,
        pack_v1.install_directory,
        (updated_mod,),
        pack_v1.source_path,
    )
    backup_manager = BackupManager(pack_v2)
    old_state = load_state(pack_v2.install_directory)
    for _ in range(5):
        backup_manager.create((updated_mod,), old_state, reason="test")

    report = Installer(CopyDownloader({"ExampleMod": archive_v2})).update_modpack(pack_v2)

    assert report.installed == 1
    assert report.backup_id is not None
    assert not report.failures
    assert (pack_v2.install_directory / "ExampleMod" / "plugin.txt").read_bytes() == b"new"
    assert load_state(pack_v2.install_directory)["mods"]["ExampleMod"]["version"] == "2.0"
    backup_file = (
        pack_v2.install_directory
        / BACKUP_DIRECTORY
        / report.backup_id
        / "files"
        / "ExampleMod"
        / "plugin.txt"
    )
    assert backup_file.read_bytes() == b"old"
    assert len(backup_manager.list_backups()) == 5


def test_update_failure_after_apply_rolls_back_files_and_state(tmp_path):
    archive_v1 = make_zip(tmp_path / "v1.zip", b"old")
    archive_v2 = make_zip(tmp_path / "v2.zip", b"new")
    mod_v1 = Mod("ExampleMod", "1.0", "https://example.com/v1.zip", sha256_file(archive_v1))
    base = make_pack(tmp_path, pack_version="1.0", mod_version="1.0")
    pack_v1 = Modpack(
        base.name,
        base.version,
        base.description,
        base.game,
        base.install_directory,
        (mod_v1,),
        base.source_path,
    )
    Installer(CopyDownloader({"ExampleMod": archive_v1})).install_modpack(pack_v1)
    old_state = load_state(pack_v1.install_directory)
    mod_v2 = Mod("ExampleMod", "2.0", "https://example.com/v2.zip", sha256_file(archive_v2))
    pack_v2 = Modpack(
        pack_v1.name,
        "2.0",
        "",
        pack_v1.game,
        pack_v1.install_directory,
        (mod_v2,),
        pack_v1.source_path,
    )
    backup_manager = BackupManager(pack_v2)
    for _ in range(5):
        backup_manager.create((mod_v2,), old_state, reason="test")
    updater = ApplyThenFailInstaller(CopyDownloader({"ExampleMod": archive_v2}))

    report = updater.update_modpack(pack_v2)

    assert report.failures
    assert "injected apply failure" in report.failures[0].message
    assert report.rollback_attempted
    assert report.rollback_succeeded is True
    assert (pack_v2.install_directory / "ExampleMod" / "plugin.txt").read_bytes() == b"old"
    assert load_state(pack_v2.install_directory) == old_state
    assert len(backup_manager.list_backups()) == 6


def test_update_reports_rollback_failure_and_preserves_backup(tmp_path):
    archive_v1 = make_zip(tmp_path / "v1.zip", b"old")
    archive_v2 = make_zip(tmp_path / "v2.zip", b"new")
    mod_v1 = Mod("ExampleMod", "1.0", "https://example.com/v1.zip", sha256_file(archive_v1))
    base = make_pack(tmp_path, pack_version="1.0", mod_version="1.0")
    pack_v1 = Modpack(
        base.name,
        "1.0",
        "",
        base.game,
        base.install_directory,
        (mod_v1,),
        base.source_path,
    )
    Installer(CopyDownloader({"ExampleMod": archive_v1})).install_modpack(pack_v1)
    mod_v2 = Mod("ExampleMod", "2.0", "https://example.com/v2.zip", sha256_file(archive_v2))
    pack_v2 = Modpack(
        base.name,
        "2.0",
        "",
        base.game,
        base.install_directory,
        (mod_v2,),
        base.source_path,
    )
    updater = ApplyThenFailInstaller(
        CopyDownloader({"ExampleMod": archive_v2}),
        backup_manager_factory=RestoreFailingBackupManager,
    )

    report = updater.update_modpack(pack_v2)

    assert report.rollback_attempted
    assert report.rollback_succeeded is False
    assert "injected rollback failure" in report.rollback_error
    assert report.backup_id is not None
    assert (pack_v2.install_directory / BACKUP_DIRECTORY / report.backup_id).is_dir()


def test_download_failure_before_apply_leaves_installation_untouched(tmp_path):
    first_archive = make_zip(tmp_path / "first.zip", b"first old")
    second_archive = make_zip(tmp_path / "second.zip", b"second old")
    mods_v1 = (
        Mod("First", "1", "https://example.com/first.zip", sha256_file(first_archive)),
        Mod("Second", "1", "https://example.com/second.zip", sha256_file(second_archive)),
    )
    base = make_pack(tmp_path)
    pack_v1 = Modpack(
        base.name,
        "1",
        "",
        base.game,
        base.install_directory,
        mods_v1,
        base.source_path,
    )
    Installer(CopyDownloader({"First": first_archive, "Second": second_archive})).install_modpack(
        pack_v1
    )
    first_update = make_zip(tmp_path / "first-new.zip", b"first new")
    mods_v2 = (
        Mod("First", "2", "https://example.com/first-new.zip", sha256_file(first_update)),
        Mod("Second", "2", "https://example.com/second-new.zip"),
    )
    pack_v2 = Modpack(
        base.name,
        "2",
        "",
        base.game,
        base.install_directory,
        mods_v2,
        base.source_path,
    )
    downloader = CopyDownloader({"First": first_update, "Second": InstallError("download failed")})

    report = Installer(downloader).update_modpack(pack_v2)

    assert report.failures
    assert report.backup_id is None
    assert (base.install_directory / "First" / "plugin.txt").read_bytes() == b"first old"
    assert (base.install_directory / "Second" / "plugin.txt").read_bytes() == b"second old"


def test_backup_cli_lists_and_restores_backup(tmp_path, capsys):
    config = tmp_path / "modpack.json"
    config.write_text(
        json.dumps(
            {
                "name": "Test Pack",
                "version": "2.0",
                "description": "",
                "game": "Test Game",
                "install_directory": "./mods",
                "mods": [
                    {
                        "name": "ExampleMod",
                        "version": "2.0",
                        "url": "https://example.com/mod.zip",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    pack = make_pack(tmp_path)
    installed, state = seed_installed(pack)
    backup = BackupManager(pack).create(pack.mods, state)
    installed.write_bytes(b"changed")

    assert main(["backup", "list", str(config)]) == 0
    assert backup.backup_id in capsys.readouterr().out
    assert main(["backup", "restore", str(config), backup.backup_id]) == 0
    output = capsys.readouterr().out

    assert "Backup restored" in output
    assert installed.read_bytes() == b"old version"
