import json
import os
import shutil
import sys
import zipfile
from pathlib import Path

import pytest

from modsync.backup import BackupManager
from modsync.cli import main
from modsync.exceptions import (
    ProfileError,
    ProfileExistsError,
    ProfileLockError,
    ProfileNotFoundError,
)
from modsync.hashing import sha256_file
from modsync.installer import Installer
from modsync.profiles import ProfileStore, default_data_directory, validate_profile_name
from modsync.state import load_state_file, save_state_file


class CopyDownloader:
    def __init__(self, source: Path):
        self.source = source

    def download(self, mod, destination, progress=None):
        target = destination / self.source.name
        shutil.copy2(self.source, target)
        return target


def write_modpack(
    path: Path,
    *,
    name: str = "Friends Pack",
    game: str = "Valheim",
    version: str = "1.0",
    install_directory: str = "./mods",
    mod_name: str = "ExampleMod",
    mod_version: str = "1",
    url: str = "https://example.com/mod.zip",
    sha256: str | None = None,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "name": name,
                "version": version,
                "game": game,
                "install_directory": install_directory,
                "mods": [
                    {
                        "name": mod_name,
                        "version": mod_version,
                        "url": url,
                        "sha256": sha256,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def make_store(tmp_path: Path) -> ProfileStore:
    return ProfileStore(tmp_path / "user-data")


def test_default_data_directory_is_platform_native():
    directory = default_data_directory()

    if sys.platform == "darwin":
        assert directory == Path.home() / "Library" / "Application Support" / "ModSync"
    elif os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
        assert directory == base / "ModSync"
    else:
        xdg_data_home = os.environ.get("XDG_DATA_HOME")
        base = Path(xdg_data_home) if xdg_data_home else Path.home() / ".local" / "share"
        assert directory == base / "modsync"


def test_create_profile_builds_complete_storage_without_installing(tmp_path):
    config = write_modpack(tmp_path / "source" / "modpack.json")
    store = make_store(tmp_path)

    profile = store.create("friends-server", config)

    assert profile.name == "friends-server"
    assert profile.game == "Valheim"
    assert profile.mod_count == 1
    assert (profile.directory / "profile.json").is_file()
    assert (profile.directory / "modpack.json").is_file()
    assert (profile.directory / "state.json").is_file()
    assert (profile.directory / "backups").is_dir()
    assert json.loads(store.config_path.read_text(encoding="utf-8"))["active_profile"] is None
    assert load_state_file(profile.directory / "state.json")["mods"] == {}
    assert not profile.install_directory.exists()


def test_duplicate_profile_is_rejected_case_insensitively(tmp_path):
    config = write_modpack(tmp_path / "modpack.json")
    store = make_store(tmp_path)
    store.create("Friends", config)

    with pytest.raises(ProfileExistsError, match="already exists"):
        store.create("friends", config)


def test_cli_warns_when_profiles_share_install_directory(tmp_path, capsys):
    config = write_modpack(tmp_path / "modpack.json")
    store = make_store(tmp_path)
    store.create("one", config)

    assert main(["profile", "create", "two", str(config)], profile_store=store) == 0

    error = capsys.readouterr().err
    assert "Profiles share the same physical mod directory" in error
    assert "state and backups remain separate" in error


def test_list_profiles_is_sorted_and_reports_metadata(tmp_path):
    config = write_modpack(tmp_path / "modpack.json")
    store = make_store(tmp_path)
    store.create("zeta", config)
    store.create("alpha", config)

    profiles = store.list()

    assert [profile.name for profile in profiles] == ["alpha", "zeta"]
    assert all(profile.mod_count == 1 for profile in profiles)


def test_activate_and_switch_active_profile(tmp_path):
    config = write_modpack(tmp_path / "modpack.json")
    store = make_store(tmp_path)
    store.create("one", config)
    store.create("two", config)

    store.activate("one")
    assert store.active_name() == "one"
    store.activate("two")
    assert store.active_name() == "two"


def test_delete_removes_only_profile_data(tmp_path):
    config = write_modpack(tmp_path / "modpack.json")
    store = make_store(tmp_path)
    profile = store.create("friends", config)
    installed = profile.install_directory / "ExampleMod" / "plugin.txt"
    installed.parent.mkdir(parents=True)
    installed.write_text("keep", encoding="utf-8")

    store.delete("friends")

    assert not profile.directory.exists()
    assert installed.read_text(encoding="utf-8") == "keep"


def test_delete_active_profile_clears_active_selection(tmp_path):
    config = write_modpack(tmp_path / "modpack.json")
    store = make_store(tmp_path)
    store.create("friends", config)
    store.activate("friends")

    store.delete("friends")

    assert store.active_name() is None


def test_delete_cli_requires_confirmation(tmp_path, monkeypatch, capsys):
    config = write_modpack(tmp_path / "modpack.json")
    store = make_store(tmp_path)
    store.create("friends", config)
    monkeypatch.setattr("builtins.input", lambda prompt: "no")

    assert main(["profile", "delete", "friends"], profile_store=store) == 0
    assert store.get("friends").name == "friends"
    assert "cancelled" in capsys.readouterr().out


def test_delete_cli_yes_skips_confirmation(tmp_path):
    config = write_modpack(tmp_path / "modpack.json")
    store = make_store(tmp_path)
    store.create("friends", config)

    assert main(["profile", "delete", "friends", "--yes"], profile_store=store) == 0
    with pytest.raises(ProfileNotFoundError):
        store.get("friends")


@pytest.mark.parametrize(
    "name",
    [
        "..",
        "../escape",
        "nested/profile",
        r"nested\profile",
        "/absolute",
        "bad\x00name",
        "profile.",
        "CON.txt",
    ],
)
def test_rejects_unsafe_profile_names(name):
    with pytest.raises(ProfileError):
        validate_profile_name(name)


def test_profile_modpack_preserves_original_relative_install_directory(tmp_path):
    config = write_modpack(tmp_path / "source" / "modpack.json", install_directory="../game/mods")
    store = make_store(tmp_path)
    store.create("friends", config)

    modpack = store.load_modpack("friends")

    assert modpack.install_directory == (tmp_path / "game" / "mods").resolve()
    assert modpack.state_path == store.root / "profiles" / "friends" / "state.json"
    assert modpack.backup_directory == store.root / "profiles" / "friends" / "backups"


def test_profiles_have_independent_state_files(tmp_path):
    config = write_modpack(tmp_path / "modpack.json")
    store = make_store(tmp_path)
    store.create("one", config)
    store.create("two", config)
    one = store.load_modpack("one")
    two = store.load_modpack("two")
    state = {"schema_version": 1, "modpack": {"name": "one"}, "mods": {"A": {}}}

    save_state_file(one.state_path, state)

    assert load_state_file(one.state_path)["mods"] == {"A": {}}
    assert load_state_file(two.state_path)["mods"] == {}


def test_profiles_have_independent_backup_directories(tmp_path):
    config = write_modpack(tmp_path / "modpack.json")
    store = make_store(tmp_path)
    store.create("one", config)
    store.create("two", config)
    one = store.load_modpack("one")
    two = store.load_modpack("two")
    installed = one.install_directory / "ExampleMod" / "plugin.txt"
    installed.parent.mkdir(parents=True)
    installed.write_bytes(b"one")
    state = {
        "schema_version": 1,
        "modpack": {"name": one.name, "version": one.version},
        "mods": {"ExampleMod": {"version": "1", "files": {"plugin.txt": sha256_file(installed)}}},
    }

    backup = BackupManager(one).create(one.mods, state)

    assert (one.backup_directory / backup.backup_id).is_dir()
    assert list(two.backup_directory.iterdir()) == []


def test_profile_update_does_not_touch_other_installation_or_state(tmp_path):
    archive_v1 = tmp_path / "v1.zip"
    with zipfile.ZipFile(archive_v1, "w") as bundle:
        bundle.writestr("plugin.txt", b"version one")
    archive_v2 = tmp_path / "v2.zip"
    with zipfile.ZipFile(archive_v2, "w") as bundle:
        bundle.writestr("plugin.txt", b"version two")
    config_a = write_modpack(
        tmp_path / "a.json", install_directory="./mods-a", sha256=sha256_file(archive_v1)
    )
    config_b = write_modpack(
        tmp_path / "b.json", install_directory="./mods-b", sha256=sha256_file(archive_v1)
    )
    store = make_store(tmp_path)
    store.create("a", config_a)
    store.create("b", config_b)
    a_v1 = store.load_modpack("a")
    b = store.load_modpack("b")
    Installer(CopyDownloader(archive_v1)).install_modpack(a_v1)
    Installer(CopyDownloader(archive_v1)).install_modpack(b)
    saved = json.loads((store.get("a").directory / "modpack.json").read_text(encoding="utf-8"))
    saved["version"] = "2.0"
    saved["mods"][0]["version"] = "2"
    saved["mods"][0]["sha256"] = sha256_file(archive_v2)
    (store.get("a").directory / "modpack.json").write_text(json.dumps(saved), encoding="utf-8")

    report = Installer(CopyDownloader(archive_v2)).update_modpack(store.load_modpack("a"))

    assert not report.failures
    assert (a_v1.install_directory / "ExampleMod" / "plugin.txt").read_bytes() == b"version two"
    assert (b.install_directory / "ExampleMod" / "plugin.txt").read_bytes() == b"version one"
    assert load_state_file(b.state_path)["mods"]["ExampleMod"]["version"] == "1"
    assert list(b.backup_directory.iterdir()) == []


def test_active_profile_is_used_when_modpack_argument_is_omitted(tmp_path, capsys):
    config = write_modpack(tmp_path / "modpack.json", name="Active Pack")
    store = make_store(tmp_path)
    store.create("active", config)
    store.activate("active")

    assert main(["info"], profile_store=store) == 0
    assert "Active Pack 1.0" in capsys.readouterr().out


def test_explicit_profile_overrides_active_profile(tmp_path, capsys):
    one = write_modpack(tmp_path / "one.json", name="One")
    two = write_modpack(tmp_path / "two.json", name="Two")
    store = make_store(tmp_path)
    store.create("one", one)
    store.create("two", two)
    store.activate("one")

    assert main(["info", "--profile", "two"], profile_store=store) == 0
    assert "Two 1.0" in capsys.readouterr().out


def test_legacy_modpack_cli_remains_supported(tmp_path, capsys):
    config = write_modpack(tmp_path / "modpack.json", name="Legacy Pack")

    assert main(["info", str(config)], profile_store=make_store(tmp_path)) == 0
    assert "Legacy Pack 1.0" in capsys.readouterr().out


def test_missing_active_profile_has_human_readable_error(tmp_path, capsys):
    result = main(["info"], profile_store=make_store(tmp_path))

    assert result == 2
    assert "No active profile" in capsys.readouterr().err


def test_profile_lock_rejects_concurrent_writer(tmp_path):
    config = write_modpack(tmp_path / "modpack.json")
    store = make_store(tmp_path)
    store.create("friends", config)

    with store.lock("friends"):
        with pytest.raises(ProfileLockError, match="already being modified"):
            with store.lock("friends"):
                pass


def test_profile_lock_is_released_after_error(tmp_path):
    config = write_modpack(tmp_path / "modpack.json")
    store = make_store(tmp_path)
    store.create("friends", config)

    with pytest.raises(RuntimeError):
        with store.lock("friends"):
            raise RuntimeError("injected")
    with store.lock("friends"):
        pass


def test_read_only_profile_command_does_not_require_writer_lock(tmp_path, capsys):
    config = write_modpack(tmp_path / "modpack.json")
    store = make_store(tmp_path)
    store.create("friends", config)

    with store.lock("friends"):
        assert main(["info", "--profile", "friends"], profile_store=store) == 0
    assert "Friends Pack" in capsys.readouterr().out


def test_global_config_is_written_atomically(tmp_path):
    config = write_modpack(tmp_path / "modpack.json")
    store = make_store(tmp_path)
    store.create("friends", config)

    store.activate("friends")

    value = json.loads(store.config_path.read_text(encoding="utf-8"))
    assert value == {"schema_version": 1, "active_profile": "friends"}
    assert not list(store.root.glob(".config.json.*.tmp"))


def test_corrupted_profile_metadata_is_rejected(tmp_path):
    config = write_modpack(tmp_path / "modpack.json")
    store = make_store(tmp_path)
    profile = store.create("friends", config)
    (profile.directory / "profile.json").write_text("not json", encoding="utf-8")

    with pytest.raises(ProfileError, match="Cannot read profile metadata"):
        store.get("friends")


def test_corrupted_global_config_is_rejected(tmp_path):
    store = make_store(tmp_path)
    store.root.mkdir(parents=True)
    store.config_path.write_text('{"schema_version": 999}', encoding="utf-8")

    with pytest.raises(ProfileError, match="Invalid global"):
        store.active_name()


def test_missing_profile_is_reported(tmp_path):
    with pytest.raises(ProfileNotFoundError, match="Profile not found"):
        make_store(tmp_path).get("missing")


def test_backup_cli_uses_profile_storage(tmp_path, capsys):
    config = write_modpack(tmp_path / "modpack.json")
    store = make_store(tmp_path)
    store.create("friends", config)

    assert main(["backup", "list", "--profile", "friends"], profile_store=store) == 0
    assert "No backups found" in capsys.readouterr().out


def test_backup_restore_cli_uses_profile_state_and_backup(tmp_path):
    config = write_modpack(tmp_path / "modpack.json")
    store = make_store(tmp_path)
    store.create("friends", config)
    pack = store.load_modpack("friends")
    installed = pack.install_directory / "ExampleMod" / "plugin.txt"
    installed.parent.mkdir(parents=True)
    installed.write_bytes(b"previous")
    state = {
        "schema_version": 1,
        "modpack": {"name": pack.name, "version": pack.version},
        "mods": {
            "ExampleMod": {
                "version": "1",
                "source_sha256": "a" * 64,
                "directory": "ExampleMod",
                "files": {"plugin.txt": sha256_file(installed)},
            }
        },
    }
    save_state_file(pack.state_path, state)
    backup = BackupManager(pack).create(pack.mods, state)
    installed.write_bytes(b"damaged")

    result = main(
        ["backup", "restore", backup.backup_id, "--profile", "friends"],
        profile_store=store,
    )

    assert result == 0
    assert installed.read_bytes() == b"previous"
    assert load_state_file(pack.state_path) == state


def test_profile_list_cli_marks_active_profile(tmp_path, capsys):
    config = write_modpack(tmp_path / "modpack.json")
    store = make_store(tmp_path)
    store.create("friends", config)
    store.activate("friends")

    assert main(["profile", "list"], profile_store=store) == 0
    output = capsys.readouterr().out
    assert "NAME | GAME | MODS | ACTIVE" in output
    assert "friends | Valheim | 1 | yes" in output


def test_create_profile_cli_reports_next_steps(tmp_path, capsys):
    config = write_modpack(tmp_path / "modpack.json")
    store = make_store(tmp_path)

    assert main(["profile", "create", "friends", str(config)], profile_store=store) == 0
    output = capsys.readouterr().out
    assert "Profile created: friends" in output
    assert "modsync install --profile friends" in output
