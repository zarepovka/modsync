import json
import os
import shutil
import zipfile
from pathlib import Path

import pytest

from modsync.backup import BackupManager
from modsync.cli import main
from modsync.exceptions import (
    ProfileLockError,
    ProfileSwitchError,
    SwitchConflictError,
)
from modsync.hashing import sha256_file
from modsync.installer import Installer
from modsync.profiles import ProfileStore
from modsync.state import disabled_storage_path, load_state_file, save_state_file
from modsync.switching import ProfileSwitcher
from modsync.verifier import verify_modpack


def write_pack(path, root, mods, *, game="valheim", name="Pack"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "name": name,
                "version": "1",
                "game": game,
                "install_directory": str(root),
                "mods": [
                    {
                        "name": mod_name,
                        "version": "1",
                        "url": f"https://example.com/{mod_name}.zip",
                        "enabled": enabled,
                    }
                    for mod_name, enabled in mods
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def make_profiles(tmp_path, *, a_mods=(("A", True),), b_mods=(("B", True),)):
    game = tmp_path / "game"
    (game / "BepInEx" / "core").mkdir(parents=True)
    (game / "valheim.exe").touch()
    store = ProfileStore(tmp_path / "data")
    store.create("alpha", write_pack(tmp_path / "a.json", game, a_mods, name="Alpha"))
    store.create("beta", write_pack(tmp_path / "b.json", game, b_mods, name="Beta"))
    store.activate("alpha")
    return store, game


def seed_profile(
    store,
    name,
    packages,
    *,
    physical=False,
    artifact=True,
):
    pack = store.load_modpack(name)
    state = load_state_file(pack.state_path)
    for package, spec in packages.items():
        content = spec.get("content", package.encode())
        path = spec.get("path", f"BepInEx/plugins/{package}/mod.dll")
        owner = spec.get("owner", package)
        status = spec.get("status", "enabled")
        storage = "disabled" if status == "disabled" and "BepInEx/config/" not in path else "game"
        digest = sha256_file(write_bytes(tmp_file(store, name, package), content))
        entry = {"path": path, "owner": owner, "sha256": digest}
        if storage == "disabled":
            entry["storage"] = "disabled"
        record = {
            "version": spec.get("version", "1"),
            "owner": owner,
            "package": owner,
            "status": status,
            "install_reason": spec.get("reason", "explicit"),
            "role": spec.get("reason", "explicit"),
            "dependencies": list(spec.get("dependencies", ())),
            "installed_files": [entry],
            "files": {path: digest},
            "source_sha256": "0" * 64,
            "source": {
                "type": spec.get("source", "direct"),
                "url": f"https://example.com/{package}.zip",
                "identity": spec.get("identity", {}),
                "release": {},
                "sha256": "0" * 64,
            },
        }
        state["mods"][package] = record
        relative = Path(path)
        if storage == "disabled":
            target = disabled_storage_path(pack) / owner / relative
            write_bytes(target, content)
        elif physical:
            write_bytes(pack.install_directory / relative, content)
        if storage == "game" and artifact:
            write_bytes(store.get(name).directory / "artifacts" / relative, content)
    state["modpack"] = {"name": pack.name, "version": pack.version}
    save_state_file(pack.state_path, state)
    return state


def tmp_file(store, profile, package):
    return store.root / ".digests" / profile / package


def write_bytes(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def game_path(game, package="A", filename="mod.dll"):
    return game / "BepInEx" / "plugins" / package / filename


def test_switch_a_to_b(tmp_path):
    store, game = make_profiles(tmp_path)
    seed_profile(store, "alpha", {"A": {}}, physical=True)
    seed_profile(store, "beta", {"B": {"content": b"beta"}})

    report = ProfileSwitcher(store).switch("beta")

    assert report.changed and store.active_name() == "beta"
    assert not game_path(game, "A").exists()
    assert game_path(game, "B").read_bytes() == b"beta"


def test_switch_b_to_a(tmp_path):
    store, game = make_profiles(tmp_path)
    seed_profile(store, "alpha", {"A": {"content": b"alpha"}}, physical=True)
    seed_profile(store, "beta", {"B": {"content": b"beta"}})
    switcher = ProfileSwitcher(store)
    switcher.switch("beta")

    switcher.switch("alpha")

    assert store.active_name() == "alpha"
    assert game_path(game, "A").read_bytes() == b"alpha"
    assert not game_path(game, "B").exists()


def test_same_file_is_kept(tmp_path):
    store, game = make_profiles(tmp_path, a_mods=(("Shared", True),), b_mods=(("Shared", True),))
    spec = {"path": "BepInEx/plugins/Shared/mod.dll", "content": b"same"}
    seed_profile(store, "alpha", {"Shared": spec}, physical=True)
    seed_profile(store, "beta", {"Shared": spec})
    before = game_path(game, "Shared").stat().st_ino

    report = ProfileSwitcher(store).switch("beta")

    assert report.kept == 1
    if os.name != "nt":
        assert game_path(game, "Shared").stat().st_ino == before


def test_same_package_version_is_minimal(tmp_path):
    store, _ = make_profiles(tmp_path, a_mods=(("Shared", True),), b_mods=(("Shared", True),))
    spec = {"content": b"same", "version": "1"}
    seed_profile(store, "alpha", {"Shared": spec}, physical=True)
    seed_profile(store, "beta", {"Shared": spec})
    plan = ProfileSwitcher(store).build_plan("alpha", "beta")
    assert len(plan.keep) == 1 and not plan.remove and not plan.install


def test_different_package_version_is_replaced(tmp_path):
    store, game = make_profiles(tmp_path, a_mods=(("Shared", True),), b_mods=(("Shared", True),))
    seed_profile(store, "alpha", {"Shared": {"content": b"v1", "version": "1"}}, physical=True)
    seed_profile(store, "beta", {"Shared": {"content": b"v2", "version": "2"}})
    ProfileSwitcher(store).switch("beta")
    assert game_path(game, "Shared").read_bytes() == b"v2"


def test_file_only_in_source_is_removed(tmp_path):
    store, game = make_profiles(tmp_path, b_mods=())
    seed_profile(store, "alpha", {"A": {}}, physical=True)
    seed_profile(store, "beta", {})
    ProfileSwitcher(store).switch("beta")
    assert not game_path(game, "A").exists()


def test_file_only_in_target_is_installed(tmp_path):
    store, game = make_profiles(tmp_path, a_mods=(), b_mods=(("B", True),))
    seed_profile(store, "alpha", {}, physical=True)
    seed_profile(store, "beta", {"B": {"content": b"new"}})
    ProfileSwitcher(store).switch("beta")
    assert game_path(game, "B").read_bytes() == b"new"


def test_enabled_to_disabled_removes_runtime(tmp_path):
    store, game = make_profiles(tmp_path, a_mods=(("Shared", True),), b_mods=(("Shared", False),))
    seed_profile(store, "alpha", {"Shared": {}}, physical=True)
    seed_profile(store, "beta", {"Shared": {"status": "disabled"}})
    report = ProfileSwitcher(store).switch("beta")
    assert not game_path(game, "Shared").exists()
    assert report.disabled_packages == ("Shared",)


def test_disabled_to_enabled_restores_runtime(tmp_path):
    store, game = make_profiles(tmp_path, a_mods=(("Shared", False),), b_mods=(("Shared", True),))
    seed_profile(store, "alpha", {"Shared": {"status": "disabled"}}, physical=True)
    seed_profile(store, "beta", {"Shared": {"content": b"enabled"}})
    ProfileSwitcher(store).switch("beta")
    assert game_path(game, "Shared").read_bytes() == b"enabled"


def test_target_missing_dependency_is_rejected(tmp_path):
    store, game = make_profiles(tmp_path, b_mods=(("B", True),))
    seed_profile(store, "alpha", {"A": {}}, physical=True)
    seed_profile(store, "beta", {"B": {"dependencies": ("Library",)}})
    with pytest.raises(ProfileSwitchError, match="unavailable"):
        ProfileSwitcher(store).switch("beta")
    assert game_path(game, "A").exists()


def test_target_disabled_dependency_is_rejected(tmp_path):
    store, _ = make_profiles(tmp_path, b_mods=(("B", True), ("Library", False)))
    seed_profile(store, "alpha", {"A": {}}, physical=True)
    seed_profile(
        store,
        "beta",
        {
            "B": {"dependencies": ("Library",)},
            "Library": {"status": "disabled"},
        },
    )
    with pytest.raises(ProfileSwitchError, match="unavailable"):
        ProfileSwitcher(store).switch("beta")


def test_v06_dependency_metadata_is_validated(tmp_path):
    store, _ = make_profiles(tmp_path, b_mods=(("B", True), ("Library", False)))
    seed_profile(store, "alpha", {"A": {}}, physical=True)
    state = seed_profile(
        store,
        "beta",
        {"B": {}, "Library": {"status": "disabled", "reason": "dependency"}},
    )
    state["mods"]["B"].pop("dependencies")
    state["mods"]["B"]["source"]["release"] = {
        "dependencies": ["Author-Library-1.0.0"]
    }
    state["mods"]["Library"]["source"]["identity"] = {
        "namespace": "Author",
        "package": "Library",
    }
    save_state_file(store.get("beta").directory / "state.json", state)
    with pytest.raises(ProfileSwitchError, match="unavailable"):
        ProfileSwitcher(store).switch("beta")


def test_source_modified_runtime_stops_switch(tmp_path):
    store, game = make_profiles(tmp_path)
    seed_profile(store, "alpha", {"A": {}}, physical=True)
    seed_profile(store, "beta", {"B": {}})
    game_path(game, "A").write_bytes(b"modified")
    with pytest.raises(SwitchConflictError, match="Modified managed runtime"):
        ProfileSwitcher(store).switch("beta")
    assert store.active_name() == "alpha"


def test_source_modified_config_is_preserved(tmp_path):
    store, game = make_profiles(tmp_path, a_mods=(("A", True),), b_mods=())
    path = "BepInEx/config/a.cfg"
    seed_profile(store, "alpha", {"A": {"path": path, "content": b"default"}}, physical=True)
    seed_profile(store, "beta", {})
    write_bytes(game / path, b"user-a")
    ProfileSwitcher(store).switch("beta")
    assert (store.get("alpha").directory / "preserved-config" / path).read_bytes() == b"user-a"


def test_profile_specific_configs_round_trip(tmp_path):
    store, game = make_profiles(tmp_path, a_mods=(("A", True),), b_mods=(("B", True),))
    path = "BepInEx/config/settings.cfg"
    seed_profile(store, "alpha", {"A": {"path": path, "content": b"a-default"}}, physical=True)
    seed_profile(store, "beta", {"B": {"path": path, "content": b"b-default", "owner": "B"}})
    write_bytes(game / path, b"a-user")
    switcher = ProfileSwitcher(store)
    switcher.switch("beta")
    write_bytes(game / path, b"b-user")
    switcher.switch("alpha")
    assert (game / path).read_bytes() == b"a-user"
    switcher.switch("beta")
    assert (game / path).read_bytes() == b"b-user"


def test_preserved_config_metadata_is_relative_and_profile_owned(tmp_path):
    store, game = make_profiles(tmp_path, b_mods=())
    path = "BepInEx/config/a.cfg"
    seed_profile(store, "alpha", {"A": {"path": path, "content": b"default"}}, physical=True)
    seed_profile(store, "beta", {})
    write_bytes(game / path, b"user")
    ProfileSwitcher(store).switch("beta")
    metadata = json.loads(
        (store.get("alpha").directory / "preserved-config.json").read_text(
            encoding="utf-8"
        )
    )
    entry = metadata["files"][0]
    assert entry["path"] == path
    assert entry["originating_profile"] == "alpha"
    assert entry["updated_at"].endswith("Z")


def test_unmanaged_destination_conflict(tmp_path):
    store, game = make_profiles(tmp_path, a_mods=(), b_mods=(("B", True),))
    seed_profile(store, "alpha", {}, physical=True)
    seed_profile(store, "beta", {"B": {}})
    write_bytes(game_path(game, "B"), b"unmanaged")
    with pytest.raises(SwitchConflictError, match="unmanaged"):
        ProfileSwitcher(store).switch("beta")


def test_ownership_conflict_is_explicit_transition(tmp_path):
    store, game = make_profiles(tmp_path, a_mods=(("A", True),), b_mods=(("B", True),))
    path = "BepInEx/plugins/shared.dll"
    seed_profile(store, "alpha", {"A": {"path": path, "content": b"a"}}, physical=True)
    seed_profile(store, "beta", {"B": {"path": path, "content": b"b"}})
    plan = ProfileSwitcher(store).build_plan("alpha", "beta")
    assert plan.remove[0].owner == "A" and plan.install[0].owner == "B"
    ProfileSwitcher(store).switch("beta")
    assert (game / path).read_bytes() == b"b"


def test_dry_run_reports_without_mutation(tmp_path):
    store, game = make_profiles(tmp_path)
    seed_profile(store, "alpha", {"A": {}}, physical=True)
    seed_profile(store, "beta", {"B": {}})
    before = game_path(game, "A").read_bytes()
    report = ProfileSwitcher(store).switch("beta", dry_run=True)
    assert report.dry_run and store.active_name() == "alpha"
    assert game_path(game, "A").read_bytes() == before
    assert not list((store.get("alpha").directory / "backups").iterdir())


class FailBeforeTarget(ProfileSwitcher):
    def apply_target(self, plan, target_name, prepared, target_state):
        raise OSError("injected target failure")


class FailAfterTarget(ProfileSwitcher):
    def apply_target(self, plan, target_name, prepared, target_state):
        super().apply_target(plan, target_name, prepared, target_state)
        raise OSError("injected post-install failure")


@pytest.mark.parametrize("switcher_type", [FailBeforeTarget, FailAfterTarget])
def test_switch_failure_rolls_back_files_and_active(tmp_path, switcher_type):
    store, game = make_profiles(tmp_path)
    seed_profile(store, "alpha", {"A": {"content": b"alpha"}}, physical=True)
    seed_profile(store, "beta", {"B": {"content": b"beta"}})
    with pytest.raises(ProfileSwitchError, match="rolled back"):
        switcher_type(store).switch("beta")
    assert store.active_name() == "alpha"
    assert game_path(game, "A").read_bytes() == b"alpha"
    assert not game_path(game, "B").exists()


def test_rollback_removes_new_preserved_config(tmp_path):
    store, game = make_profiles(tmp_path)
    path = "BepInEx/config/a.cfg"
    seed_profile(store, "alpha", {"A": {"path": path, "content": b"default"}}, physical=True)
    seed_profile(store, "beta", {"B": {}})
    write_bytes(game / path, b"user")
    with pytest.raises(ProfileSwitchError):
        FailBeforeTarget(store).switch("beta")
    assert not (store.get("alpha").directory / "preserved-config" / path).exists()
    assert not (store.get("alpha").directory / "preserved-config.json").exists()


def test_game_root_lock_blocks_switch(tmp_path):
    store, game = make_profiles(tmp_path)
    seed_profile(store, "alpha", {"A": {}}, physical=True)
    seed_profile(store, "beta", {"B": {}})
    with store.game_root_lock(game):
        with pytest.raises(ProfileLockError):
            ProfileSwitcher(store).switch("beta")


def test_game_root_lock_blocks_other_profile_mutation(tmp_path, capsys):
    store, game = make_profiles(tmp_path)
    seed_profile(store, "alpha", {"A": {}}, physical=True)
    with store.game_root_lock(game):
        assert main(["disable", "A", "--profile", "alpha"], profile_store=store) == 2
    assert "already being modified" in capsys.readouterr().err


def test_game_root_lock_released_after_exception(tmp_path):
    store, game = make_profiles(tmp_path)
    with pytest.raises(RuntimeError):
        with store.game_root_lock(game):
            raise RuntimeError("boom")
    with store.game_root_lock(game):
        pass


def test_switch_rejects_different_roots(tmp_path):
    store, _ = make_profiles(tmp_path)
    metadata_path = store.get("beta").directory / "profile.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["install_directory"] = str(tmp_path / "other")
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(ProfileSwitchError, match="same physical game root"):
        ProfileSwitcher(store).switch("beta")


def test_switch_rejects_different_games(tmp_path):
    store, _ = make_profiles(tmp_path)
    metadata_path = store.get("beta").directory / "profile.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["game"] = "other-game"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(ProfileSwitchError, match="different games"):
        ProfileSwitcher(store).switch("beta")


def test_verify_after_switch(tmp_path):
    store, _ = make_profiles(tmp_path)
    seed_profile(store, "alpha", {"A": {}}, physical=True)
    seed_profile(store, "beta", {"B": {}})
    ProfileSwitcher(store).switch("beta")
    assert verify_modpack(store.load_modpack("beta")).ok


def test_incomplete_marker_blocks_switch(tmp_path):
    store, game = make_profiles(tmp_path)
    marker = store.switch_marker(game)
    write_bytes(marker, b"{}")
    with pytest.raises(ProfileSwitchError, match="previous profile switch"):
        ProfileSwitcher(store).switch("beta")


def test_corrupt_target_state_is_rejected(tmp_path):
    store, game = make_profiles(tmp_path)
    seed_profile(store, "alpha", {"A": {}}, physical=True)
    (store.get("beta").directory / "state.json").write_text("{}", encoding="utf-8")
    with pytest.raises(Exception, match="invalid"):
        ProfileSwitcher(store).switch("beta")
    assert game_path(game, "A").exists()


@pytest.mark.parametrize(
    "unsafe_path",
    ["../escape.dll", "/tmp/escape.dll", "C:/escape.dll", "BepInEx\\evil.dll"],
)
def test_target_state_rejects_portable_path_escapes(tmp_path, unsafe_path):
    store, _ = make_profiles(tmp_path)
    seed_profile(store, "alpha", {"A": {}}, physical=True)
    state = seed_profile(store, "beta", {"B": {}})
    state["mods"]["B"]["installed_files"][0]["path"] = unsafe_path
    save_state_file(store.get("beta").directory / "state.json", state)
    with pytest.raises(ProfileSwitchError, match="Unsafe managed path"):
        ProfileSwitcher(store).switch("beta")


def test_source_symlink_attack_is_rejected(tmp_path):
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks unavailable")
    store, game = make_profiles(tmp_path)
    seed_profile(store, "alpha", {"A": {}}, physical=True)
    seed_profile(store, "beta", {"B": {}})
    game_path(game, "A").unlink()
    game_path(game, "A").symlink_to(game / "valheim.exe")
    with pytest.raises(ProfileSwitchError, match="Unsafe source profile"):
        ProfileSwitcher(store).switch("beta")


def test_source_hardlink_attack_is_rejected(tmp_path):
    store, game = make_profiles(tmp_path)
    seed_profile(store, "alpha", {"A": {}}, physical=True)
    seed_profile(store, "beta", {"B": {}})
    os.link(game_path(game, "A"), game / "linked.dll")
    with pytest.raises(ProfileSwitchError, match="Hard-linked"):
        ProfileSwitcher(store).switch("beta")


def test_case_collision_is_rejected_with_windows_semantics(tmp_path):
    store, _ = make_profiles(tmp_path, b_mods=(("B", True), ("C", True)))
    seed_profile(store, "alpha", {"A": {}}, physical=True)
    seed_profile(
        store,
        "beta",
        {
            "B": {"path": "BepInEx/plugins/Test.dll"},
            "C": {"path": "bepinex/plugins/test.dll"},
        },
    )
    with pytest.raises(SwitchConflictError, match="case-colliding"):
        ProfileSwitcher(store, case_sensitive=False).switch("beta")


def test_legacy_profile_directories_are_created_lazily(tmp_path):
    store, game = make_profiles(tmp_path)
    for profile in store.list():
        (profile.directory / "artifacts").rmdir()
        (profile.directory / "preserved-config").rmdir()
    seed_profile(store, "alpha", {"A": {}}, physical=True, artifact=False)
    seed_profile(store, "beta", {"B": {}}, artifact=True)
    ProfileSwitcher(store).switch("beta")
    assert (store.get("alpha").directory / "artifacts").is_dir()
    assert game_path(game, "B").exists()


def test_activate_remains_logical_only(tmp_path):
    store, game = make_profiles(tmp_path)
    seed_profile(store, "alpha", {"A": {}}, physical=True)
    store.activate("beta")
    assert store.active_name() == "beta" and game_path(game, "A").exists()


@pytest.mark.parametrize("source_type", ["direct", "github", "thunderstore"])
def test_mixed_source_target_uses_state_not_provider_for_cached_files(tmp_path, source_type):
    store, game = make_profiles(tmp_path)
    seed_profile(store, "alpha", {"A": {}}, physical=True)
    seed_profile(store, "beta", {"B": {"source": source_type}})
    ProfileSwitcher(store).switch("beta")
    assert game_path(game, "B").exists()


class ArchiveDownloader:
    def __init__(self, archive):
        self.archive = archive

    def download(self, resolved, destination, progress=None):
        target = destination / resolved.filename
        shutil.copy2(self.archive, target)
        return target


def test_missing_target_artifact_is_prepared_before_mutation(tmp_path):
    store, game = make_profiles(tmp_path, b_mods=(("B", True),))
    seed_profile(store, "alpha", {"A": {"content": b"alpha"}}, physical=True)
    path = "BepInEx/plugins/B/plugin.dll"
    state = seed_profile(
        store,
        "beta",
        {"B": {"path": path, "content": b"downloaded"}},
        artifact=False,
    )
    archive = tmp_path / "B.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("plugin.dll", b"downloaded")
    digest = sha256_file(archive)
    state["mods"]["B"]["source_sha256"] = digest
    state["mods"]["B"]["source"]["sha256"] = digest
    save_state_file(store.get("beta").directory / "state.json", state)

    switcher = ProfileSwitcher(store, installer=Installer(downloader=ArchiveDownloader(archive)))
    report = switcher.switch("beta")

    assert report.download_packages == ("B",)
    assert (game / path).read_bytes() == b"downloaded"


def test_dependency_graph_transition(tmp_path):
    store, game = make_profiles(
        tmp_path,
        b_mods=(("B", True), ("Library", True)),
    )
    seed_profile(store, "alpha", {"A": {}}, physical=True)
    seed_profile(
        store,
        "beta",
        {"B": {"dependencies": ("Library",)}, "Library": {"reason": "dependency"}},
    )
    ProfileSwitcher(store).switch("beta")
    assert game_path(game, "B").exists() and game_path(game, "Library").exists()


def test_source_orphan_package_does_not_remain_active(tmp_path):
    store, game = make_profiles(tmp_path)
    seed_profile(store, "alpha", {"A": {}, "OldLibrary": {"reason": "dependency"}}, physical=True)
    seed_profile(store, "beta", {"B": {}})
    ProfileSwitcher(store).switch("beta")
    assert not game_path(game, "OldLibrary").exists()


def test_profile_specific_disabled_storage_untouched(tmp_path):
    store, _ = make_profiles(tmp_path, b_mods=(("B", False),))
    seed_profile(store, "alpha", {"A": {}}, physical=True)
    seed_profile(store, "beta", {"B": {"status": "disabled", "content": b"off"}})
    beta_pack = store.load_modpack("beta")
    disabled = disabled_storage_path(beta_pack) / "B" / "BepInEx/plugins/B/mod.dll"
    ProfileSwitcher(store).switch("beta")
    assert disabled.read_bytes() == b"off"


def test_profile_config_storage_is_isolated(tmp_path):
    store, _ = make_profiles(tmp_path)
    assert (
        store.get("alpha").directory / "preserved-config"
        != store.get("beta").directory / "preserved-config"
    )


def test_schema_v4_restore_and_v1_v3_compatibility_remains(tmp_path):
    store, game = make_profiles(tmp_path)
    seed_profile(store, "alpha", {"A": {"content": b"alpha"}}, physical=True)
    seed_profile(store, "beta", {"B": {"content": b"beta"}})
    report = ProfileSwitcher(store).switch("beta")
    marker = store.switch_marker(game)
    write_bytes(marker, b"{}")
    BackupManager(store.load_modpack("alpha")).restore(report.backup_id)
    assert store.active_name() == "alpha"
    assert game_path(game, "A").read_bytes() == b"alpha"
    assert not marker.exists()


def test_cli_switch_smoke_and_output(tmp_path, capsys):
    store, game = make_profiles(tmp_path)
    seed_profile(store, "alpha", {"A": {}}, physical=True)
    seed_profile(store, "beta", {"B": {}})
    assert main(["profile", "switch", "beta"], profile_store=store) == 0
    output = capsys.readouterr().out
    assert "Switch: alpha -> beta" in output and "Active profile: beta" in output
    assert game_path(game, "B").exists()


def test_cli_switch_dry_run_output(tmp_path, capsys):
    store, _ = make_profiles(tmp_path)
    seed_profile(store, "alpha", {"A": {}}, physical=True)
    seed_profile(store, "beta", {"B": {}})
    assert main(["profile", "switch", "beta", "--dry-run"], profile_store=store) == 0
    output = capsys.readouterr().out
    assert "Keep:" in output and "Download required:" in output
    assert "were changed" in output


def test_profile_status_reports_shared_root(tmp_path, capsys):
    store, _ = make_profiles(tmp_path)
    seed_profile(store, "alpha", {"A": {}}, physical=True)
    assert main(["profile", "status"], profile_store=store) == 0
    output = capsys.readouterr().out
    assert "Active profile: alpha" in output
    assert "Profiles using this game root:" in output
    assert "alpha *" in output and "beta" in output


def test_new_profile_has_switch_storage(tmp_path):
    store, _ = make_profiles(tmp_path)
    profile = store.get("alpha")
    assert (profile.directory / "artifacts").is_dir()
    assert (profile.directory / "preserved-config").is_dir()


def test_game_root_identity_hides_absolute_path(tmp_path):
    identity = ProfileStore.game_root_identity(tmp_path / "Secret User" / "Game")
    assert len(identity) == 64 and str(tmp_path) not in identity
