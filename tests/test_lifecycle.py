import json
import os
from pathlib import Path

import pytest

from modsync.backup import BackupManager
from modsync.cli import main
from modsync.exceptions import (
    BackupIntegrityError,
    DependencySafetyError,
    InstallationConflictError,
    LifecycleError,
    ProfileLockError,
)
from modsync.hashing import sha256_file
from modsync.lifecycle import LifecycleManager
from modsync.models import Mod, Modpack, RemovalPlan
from modsync.profiles import ProfileStore
from modsync.state import (
    disabled_storage_path,
    load_state_file,
    record_install_reason,
    record_status,
    save_state_file,
)
from modsync.verifier import verify_modpack


def make_pack(tmp_path, *, state_path=None, backup_directory=None):
    root = tmp_path / "game"
    root.mkdir(parents=True, exist_ok=True)
    (root / "valheim.exe").touch()
    (root / "BepInEx" / "core").mkdir(parents=True, exist_ok=True)
    return Modpack(
        "Pack",
        "1",
        "",
        "valheim",
        root,
        (),
        tmp_path / "pack.json",
        state_path=state_path,
        backup_directory=backup_directory,
        game_adapter_id="valheim",
    )


def seed_mod(
    pack,
    name="ExampleMod",
    *,
    files=None,
    dependencies=(),
    reason="explicit",
    status="enabled",
    source_type="direct",
):
    files = files or {f"BepInEx/plugins/{name}/mod.dll": b"binary"}
    state_path = pack.state_path or pack.install_directory / ".modsync-state.json"
    state = load_state_file(state_path)
    owner = name
    installed = []
    for relative, content in files.items():
        storage = "game"
        if status == "disabled" and not relative.casefold().startswith("bepinex/config/"):
            storage = "disabled"
            target = disabled_storage_path(pack) / owner / relative
        else:
            target = pack.install_directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        entry = {"path": relative, "owner": owner, "sha256": sha256_file(target)}
        if storage == "disabled":
            entry["storage"] = "disabled"
        installed.append(entry)
    state["mods"][name] = {
        "version": "1.0.0",
        "owner": owner,
        "package": owner,
        "status": status,
        "install_reason": reason,
        "role": reason,
        "dependencies": list(dependencies),
        "installed_files": installed,
        "files": {entry["path"]: entry["sha256"] for entry in installed},
        "source": {"type": source_type},
    }
    state["modpack"] = {"name": pack.name, "version": pack.version}
    save_state_file(state_path, state)
    return state["mods"][name]


def game_file(pack, name="ExampleMod", filename="mod.dll"):
    return pack.install_directory / "BepInEx" / "plugins" / name / filename


def disabled_file(pack, name="ExampleMod", filename="mod.dll"):
    return disabled_storage_path(pack) / name / "BepInEx" / "plugins" / name / filename


def test_uninstall_regular_mod(tmp_path):
    pack = make_pack(tmp_path)
    seed_mod(pack)
    report = LifecycleManager(pack).uninstall("ExampleMod")
    assert report.changed and report.backup_id
    assert not game_file(pack).exists()
    assert "ExampleMod" not in load_state_file(pack.install_directory / ".modsync-state.json")["mods"]


def test_uninstall_unused_dependency_is_allowed(tmp_path):
    pack = make_pack(tmp_path)
    seed_mod(pack, "Library", reason="dependency")
    assert LifecycleManager(pack).uninstall("Library").changed


def test_uninstall_dependency_required_by_another_mod_is_blocked(tmp_path):
    pack = make_pack(tmp_path)
    seed_mod(pack, "Library", reason="dependency")
    seed_mod(pack, "ModA", dependencies=("Library",))
    with pytest.raises(DependencySafetyError, match="ModA"):
        LifecycleManager(pack).uninstall("Library")
    assert game_file(pack, "Library").exists()


def test_orphan_dependency_is_reported_but_not_removed(tmp_path):
    pack = make_pack(tmp_path)
    seed_mod(pack, "Library", reason="dependency")
    seed_mod(pack, "ModA", dependencies=("Library",))
    report = LifecycleManager(pack).uninstall("ModA")
    assert report.orphan_dependencies == ("Library 1.0.0",)
    assert game_file(pack, "Library").exists()


def test_explicit_dependency_is_not_reported_as_orphan(tmp_path):
    pack = make_pack(tmp_path)
    seed_mod(pack, "Library", reason="explicit")
    seed_mod(pack, "ModA", dependencies=("Library",))
    assert not LifecycleManager(pack).uninstall("ModA").orphan_dependencies


def test_unchanged_config_is_removed(tmp_path):
    pack = make_pack(tmp_path)
    seed_mod(pack, files={"BepInEx/config/example.cfg": b"default"})
    LifecycleManager(pack).uninstall("ExampleMod")
    assert not (pack.install_directory / "BepInEx/config/example.cfg").exists()


def test_modified_config_is_preserved_as_unmanaged(tmp_path):
    pack = make_pack(tmp_path)
    seed_mod(
        pack,
        files={
            "BepInEx/plugins/ExampleMod/mod.dll": b"binary",
            "BepInEx/config/example.cfg": b"default",
        },
    )
    config = pack.install_directory / "BepInEx/config/example.cfg"
    config.write_bytes(b"user changes")
    report = LifecycleManager(pack).uninstall("ExampleMod")
    assert report.preserved_files == ("BepInEx/config/example.cfg",)
    assert config.read_bytes() == b"user changes"


def test_modified_binary_requires_force(tmp_path):
    pack = make_pack(tmp_path)
    seed_mod(pack)
    game_file(pack).write_bytes(b"modified")
    with pytest.raises(LifecycleError, match="--force"):
        LifecycleManager(pack).uninstall("ExampleMod")


def test_force_uninstall_backs_up_and_removes_modified_binary(tmp_path):
    pack = make_pack(tmp_path)
    seed_mod(pack)
    game_file(pack).write_bytes(b"modified")
    report = LifecycleManager(pack).uninstall("ExampleMod", force=True)
    assert report.backup_id and not game_file(pack).exists()


def test_removal_plan_contains_paths_owner_and_cleanup(tmp_path):
    pack = make_pack(tmp_path)
    record = seed_mod(pack)
    state = load_state_file(pack.install_directory / ".modsync-state.json")
    plan = LifecycleManager(pack).build_removal_plan("ExampleMod", record, state)
    assert isinstance(plan, RemovalPlan)
    assert plan.files[0].owner == "ExampleMod"
    assert plan.files[0].path.name == "mod.dll"
    assert plan.cleanup_directories


def test_uninstall_dry_run_changes_nothing(tmp_path):
    pack = make_pack(tmp_path)
    seed_mod(pack)
    before = load_state_file(pack.install_directory / ".modsync-state.json")
    report = LifecycleManager(pack).uninstall("ExampleMod", dry_run=True)
    assert report.dry_run and not report.backup_id
    assert game_file(pack).exists()
    assert load_state_file(pack.install_directory / ".modsync-state.json") == before
    assert not (pack.install_directory / ".modsync-backups").exists()


class FailRemoval(LifecycleManager):
    def apply_removal(self, plan):
        super().apply_removal(plan)
        raise OSError("injected removal failure")


def test_uninstall_failure_rolls_back_file_and_state(tmp_path):
    pack = make_pack(tmp_path)
    seed_mod(pack)
    with pytest.raises(LifecycleError, match="rolled back"):
        FailRemoval(pack).uninstall("ExampleMod")
    assert game_file(pack).read_bytes() == b"binary"
    assert "ExampleMod" in load_state_file(pack.install_directory / ".modsync-state.json")["mods"]


def test_lifecycle_restore_rejects_incomplete_snapshot_before_mutation(tmp_path):
    pack = make_pack(tmp_path)
    seed_mod(pack)
    report = LifecycleManager(pack).disable("ExampleMod")
    metadata_path = (
        pack.install_directory
        / ".modsync-backups"
        / report.backup_id
        / "metadata.json"
    )
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["saved_files"] = []
    metadata["file_count"] = 0
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    with pytest.raises(BackupIntegrityError, match="do not match"):
        BackupManager(pack).restore(report.backup_id)
    assert disabled_file(pack).read_bytes() == b"binary"
    assert not game_file(pack).exists()


def test_disable_moves_runtime_but_keeps_config(tmp_path):
    pack = make_pack(tmp_path)
    seed_mod(
        pack,
        files={
            "BepInEx/plugins/ExampleMod/mod.dll": b"binary",
            "BepInEx/config/example.cfg": b"config",
        },
    )
    LifecycleManager(pack).disable("ExampleMod")
    assert not game_file(pack).exists()
    assert disabled_file(pack).read_bytes() == b"binary"
    assert (pack.install_directory / "BepInEx/config/example.cfg").exists()


def test_disable_already_disabled_is_rejected(tmp_path):
    pack = make_pack(tmp_path)
    seed_mod(pack, status="disabled")
    with pytest.raises(LifecycleError, match="already disabled"):
        LifecycleManager(pack).disable("ExampleMod")


def test_disable_required_dependency_lists_all_enabled_dependents(tmp_path):
    pack = make_pack(tmp_path)
    seed_mod(pack, "Library", reason="dependency")
    seed_mod(pack, "ModA", dependencies=("Library",))
    seed_mod(pack, "ModB", dependencies=("Library",))
    with pytest.raises(DependencySafetyError, match=r"ModA[\s\S]*ModB"):
        LifecycleManager(pack).disable("Library")


def test_disabled_dependent_does_not_block_disabling_dependency(tmp_path):
    pack = make_pack(tmp_path)
    seed_mod(pack, "Library", reason="dependency")
    seed_mod(pack, "ModA", dependencies=("Library",), status="disabled")
    assert LifecycleManager(pack).disable("Library").changed


def test_enable_restores_runtime(tmp_path):
    pack = make_pack(tmp_path)
    seed_mod(pack, status="disabled")
    assert LifecycleManager(pack).enable("ExampleMod").changed
    assert game_file(pack).read_bytes() == b"binary"
    assert not disabled_file(pack).exists()


def test_enable_already_enabled_is_rejected(tmp_path):
    pack = make_pack(tmp_path)
    seed_mod(pack)
    with pytest.raises(LifecycleError, match="already enabled"):
        LifecycleManager(pack).enable("ExampleMod")


def test_enable_missing_dependency_is_blocked(tmp_path):
    pack = make_pack(tmp_path)
    seed_mod(pack, status="disabled", dependencies=("Library",))
    with pytest.raises(DependencySafetyError, match="Library"):
        LifecycleManager(pack).enable("ExampleMod")


def test_enable_disabled_dependency_is_blocked(tmp_path):
    pack = make_pack(tmp_path)
    seed_mod(pack, "Library", status="disabled", reason="dependency")
    seed_mod(pack, "ExampleMod", status="disabled", dependencies=("Library",))
    with pytest.raises(DependencySafetyError, match="Library"):
        LifecycleManager(pack).enable("ExampleMod")


def test_enable_destination_conflict_protects_unmanaged_file(tmp_path):
    pack = make_pack(tmp_path)
    seed_mod(pack, status="disabled")
    game_file(pack).parent.mkdir(parents=True, exist_ok=True)
    game_file(pack).write_bytes(b"unmanaged")
    with pytest.raises(InstallationConflictError, match="unmanaged"):
        LifecycleManager(pack).enable("ExampleMod")
    assert game_file(pack).read_bytes() == b"unmanaged"


def test_enable_detects_case_insensitive_unmanaged_collision(tmp_path):
    pack = make_pack(tmp_path)
    seed_mod(pack, status="disabled")
    collision = game_file(pack).with_name("MOD.dll")
    collision.parent.mkdir(parents=True, exist_ok=True)
    collision.write_bytes(b"unmanaged")
    with pytest.raises(InstallationConflictError, match="case-insensitive|unmanaged"):
        LifecycleManager(pack, case_sensitive=False).enable("ExampleMod")


def test_disabled_storage_is_outside_bepinex_runtime(tmp_path):
    pack = make_pack(tmp_path)
    seed_mod(pack)
    LifecycleManager(pack).disable("ExampleMod")
    assert disabled_storage_path(pack) == pack.install_directory / ".modsync-disabled"
    assert pack.install_directory / "BepInEx" not in disabled_file(pack).parents


def test_enable_rejects_disabled_sha256_mismatch(tmp_path):
    pack = make_pack(tmp_path)
    seed_mod(pack, status="disabled")
    disabled_file(pack).write_bytes(b"corrupted")
    with pytest.raises(LifecycleError, match="SHA256"):
        LifecycleManager(pack).enable("ExampleMod")


class FailEnable(LifecycleManager):
    def apply_enable(self, plan):
        super().apply_enable(plan)
        raise OSError("injected enable failure")


class FailDisable(LifecycleManager):
    def apply_disable(self, plan):
        super().apply_disable(plan)
        raise OSError("injected disable failure")


def test_enable_failure_rolls_back_both_storage_roots(tmp_path):
    pack = make_pack(tmp_path)
    seed_mod(pack, status="disabled")
    with pytest.raises(LifecycleError, match="rolled back"):
        FailEnable(pack).enable("ExampleMod")
    assert disabled_file(pack).read_bytes() == b"binary"
    assert not game_file(pack).exists()


def test_disable_failure_rolls_back_both_storage_roots(tmp_path):
    pack = make_pack(tmp_path)
    seed_mod(pack)
    with pytest.raises(LifecycleError, match="rolled back"):
        FailDisable(pack).disable("ExampleMod")
    assert game_file(pack).read_bytes() == b"binary"
    assert not disabled_file(pack).exists()


def test_disable_updates_ownership_storage_and_status(tmp_path):
    pack = make_pack(tmp_path)
    seed_mod(pack)
    LifecycleManager(pack).disable("ExampleMod")
    record = load_state_file(pack.install_directory / ".modsync-state.json")["mods"]["ExampleMod"]
    assert record["status"] == "disabled"
    assert record["installed_files"][0]["storage"] == "disabled"


def test_verifier_understands_disabled_storage(tmp_path):
    pack = make_pack(tmp_path)
    seed_mod(pack, status="disabled")
    configured = Modpack(**{
        **{name: getattr(pack, name) for name in pack.__dataclass_fields__},
        "mods": (Mod("ExampleMod", "1.0.0", "https://example.com/mod.zip"),),
    })
    assert verify_modpack(configured).ok


def test_enable_updates_status_serialization(tmp_path):
    pack = make_pack(tmp_path)
    seed_mod(pack, status="disabled")
    LifecycleManager(pack).enable("ExampleMod")
    record = load_state_file(pack.install_directory / ".modsync-state.json")["mods"]["ExampleMod"]
    assert record["status"] == "enabled"
    assert record["installed_files"][0]["storage"] == "game"


def test_install_reason_is_preserved_across_disable_enable(tmp_path):
    pack = make_pack(tmp_path)
    seed_mod(pack, reason="dependency")
    manager = LifecycleManager(pack)
    manager.disable("ExampleMod")
    manager.enable("ExampleMod")
    record = load_state_file(pack.install_directory / ".modsync-state.json")["mods"]["ExampleMod"]
    assert record["install_reason"] == "dependency"


def test_legacy_state_defaults_are_safe():
    assert record_status({"version": "1"}) == "enabled"
    assert record_install_reason({"version": "1"}) == "explicit"
    assert record_install_reason({"role": "dependency"}) == "dependency"


def test_v06_thunderstore_dependency_metadata_is_used_for_safety(tmp_path):
    pack = make_pack(tmp_path)
    seed_mod(pack, "Deps-Library", reason="dependency")
    record = seed_mod(pack, "ModA")
    record.pop("dependencies")
    record["source"] = {
        "type": "thunderstore",
        "release": {"dependencies": ["Deps-Library-1.0.0"]},
    }
    state_path = pack.install_directory / ".modsync-state.json"
    state = load_state_file(state_path)
    state["mods"]["ModA"] = record
    save_state_file(state_path, state)
    with pytest.raises(DependencySafetyError, match="ModA"):
        LifecycleManager(pack).uninstall("Deps-Library")


def test_dependency_identity_alias_is_used_for_safety(tmp_path):
    pack = make_pack(tmp_path)
    library = seed_mod(pack, "Friendly Library", reason="dependency")
    library["source"] = {
        "type": "thunderstore",
        "identity": {"namespace": "Deps", "package": "Library"},
    }
    state_path = pack.install_directory / ".modsync-state.json"
    state = load_state_file(state_path)
    state["mods"]["Friendly Library"] = library
    save_state_file(state_path, state)
    seed_mod(pack, "ModA", dependencies=("Deps-Library",))

    with pytest.raises(DependencySafetyError, match="ModA"):
        LifecycleManager(pack).uninstall("Friendly Library")


def test_profile_disabled_storage_is_isolated(tmp_path):
    root = tmp_path / "shared-game"
    root.mkdir()
    (root / "valheim.exe").touch()
    (root / "BepInEx/core").mkdir(parents=True)
    one = make_pack(tmp_path / "one", state_path=tmp_path / "profiles/one/state.json", backup_directory=tmp_path / "profiles/one/backups")
    two = Modpack(**{**{name: getattr(one, name) for name in one.__dataclass_fields__}, "install_directory": one.install_directory, "state_path": tmp_path / "profiles/two/state.json", "backup_directory": tmp_path / "profiles/two/backups"})
    assert disabled_storage_path(one) != disabled_storage_path(two)


def write_profile_config(path, game_root):
    path.write_text(
        json.dumps(
            {
                "name": "Pack",
                "version": "1",
                "game": "valheim",
                "install_directory": str(game_root),
                "mods": [],
            }
        ),
        encoding="utf-8",
    )


def test_active_profile_cli_disable(tmp_path, capsys):
    game = tmp_path / "game"
    game.mkdir()
    (game / "valheim.exe").touch()
    (game / "BepInEx/core").mkdir(parents=True)
    config = tmp_path / "pack.json"
    write_profile_config(config, game)
    store = ProfileStore(tmp_path / "data")
    store.create("friends", config)
    store.activate("friends")
    pack = store.load_modpack("friends")
    seed_mod(pack)
    assert main(["disable", "ExampleMod"], profile_store=store) == 0
    assert "Disable: ExampleMod" in capsys.readouterr().out


def test_lifecycle_cli_uses_existing_profile_lock(tmp_path, capsys):
    game = tmp_path / "game"
    game.mkdir()
    (game / "valheim.exe").touch()
    (game / "BepInEx/core").mkdir(parents=True)
    config = tmp_path / "pack.json"
    write_profile_config(config, game)
    store = ProfileStore(tmp_path / "data")
    store.create("friends", config)
    seed_mod(store.load_modpack("friends"))
    with store.lock("friends"):
        assert main(["disable", "ExampleMod", "--profile", "friends"], profile_store=store) == 2
    assert "already being modified" in capsys.readouterr().err


@pytest.mark.parametrize("source_type", ["direct", "github", "thunderstore"])
def test_lifecycle_is_source_provider_independent(tmp_path, source_type):
    pack = make_pack(tmp_path)
    seed_mod(pack, source_type=source_type)
    assert LifecycleManager(pack).disable("ExampleMod").changed


def test_valheim_adapter_mode_is_required(tmp_path):
    pack = make_pack(tmp_path)
    legacy = Modpack(**{**{name: getattr(pack, name) for name in pack.__dataclass_fields__}, "game_adapter_id": None})
    with pytest.raises(LifecycleError, match="adapter-based"):
        LifecycleManager(legacy)


def test_case_insensitive_collision_in_corrupted_state(tmp_path):
    pack = make_pack(tmp_path)
    record = seed_mod(pack)
    duplicate = dict(record["installed_files"][0])
    duplicate["path"] = duplicate["path"].replace("mod.dll", "MOD.dll")
    record["installed_files"].append(duplicate)
    state = load_state_file(pack.install_directory / ".modsync-state.json")
    state["mods"]["ExampleMod"] = record
    save_state_file(pack.install_directory / ".modsync-state.json", state)
    with pytest.raises(InstallationConflictError, match="case-colliding"):
        LifecycleManager(pack, case_sensitive=False).uninstall("ExampleMod", dry_run=True)


@pytest.mark.parametrize("unsafe", ["../outside", "/absolute", "C:/escape", "..\\escape"])
def test_corrupted_state_path_traversal_is_rejected(tmp_path, unsafe):
    pack = make_pack(tmp_path)
    record = seed_mod(pack)
    record["installed_files"][0]["path"] = unsafe
    state = load_state_file(pack.install_directory / ".modsync-state.json")
    state["mods"]["ExampleMod"] = record
    save_state_file(pack.install_directory / ".modsync-state.json", state)
    with pytest.raises(LifecycleError, match="Unsafe managed path"):
        LifecycleManager(pack).uninstall("ExampleMod", dry_run=True)


def test_symlink_attack_is_rejected(tmp_path):
    pack = make_pack(tmp_path)
    seed_mod(pack)
    target = game_file(pack)
    outside = tmp_path / "outside.dll"
    outside.write_bytes(b"outside")
    target.unlink()
    try:
        target.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    with pytest.raises(LifecycleError, match="escapes|safe regular file"):
        LifecycleManager(pack).uninstall("ExampleMod", force=True)
    assert outside.read_bytes() == b"outside"


def test_hardlink_attack_is_rejected(tmp_path):
    pack = make_pack(tmp_path)
    seed_mod(pack)
    try:
        os.link(game_file(pack), tmp_path / "hardlink.dll")
    except (OSError, NotImplementedError):
        pytest.skip("hardlinks unavailable")
    with pytest.raises(LifecycleError, match="Hard-linked"):
        LifecycleManager(pack).disable("ExampleMod", force=True)


def test_cli_info_shows_status_and_reason(tmp_path, capsys):
    pack = make_pack(tmp_path)
    seed_mod(pack, status="disabled", reason="dependency")
    pack.source_path.write_text(
        json.dumps(
            {
                "name": "Pack",
                "version": "1",
                "game": "valheim",
                "install_directory": str(pack.install_directory),
                "mods": [{"name": "ExampleMod", "version": "1.0.0", "url": "https://example.com/mod.zip"}],
            }
        ),
        encoding="utf-8",
    )
    assert main(["info", str(pack.source_path)]) == 0
    output = capsys.readouterr().out
    assert "disabled" in output and "dependency" in output


@pytest.mark.parametrize("command", ["uninstall", "disable", "enable"])
def test_lifecycle_cli_dry_run_smoke(tmp_path, command, capsys):
    pack = make_pack(tmp_path)
    status = "disabled" if command == "enable" else "enabled"
    seed_mod(pack, status=status)
    pack.source_path.write_text(
        json.dumps(
            {
                "name": "Pack",
                "version": "1",
                "game": "valheim",
                "install_directory": str(pack.install_directory),
                "mods": [],
            }
        ),
        encoding="utf-8",
    )
    assert main([command, "ExampleMod", str(pack.source_path), "--dry-run"], profile_store=ProfileStore(tmp_path / "data")) == 0
    assert "DRY RUN" in capsys.readouterr().out


def test_modified_binary_can_be_force_disabled_and_hash_is_updated(tmp_path):
    pack = make_pack(tmp_path)
    seed_mod(pack)
    game_file(pack).write_bytes(b"modified")
    LifecycleManager(pack).disable("ExampleMod", force=True)
    record = load_state_file(pack.install_directory / ".modsync-state.json")["mods"]["ExampleMod"]
    assert record["installed_files"][0]["sha256"] == sha256_file(disabled_file(pack))


def test_shared_file_legacy_corruption_is_not_removed(tmp_path):
    pack = make_pack(tmp_path)
    first = seed_mod(pack, "A")
    second = seed_mod(pack, "B")
    second["installed_files"][0] = dict(first["installed_files"][0])
    state = load_state_file(pack.install_directory / ".modsync-state.json")
    state["mods"]["B"] = second
    save_state_file(pack.install_directory / ".modsync-state.json", state)
    with pytest.raises(InstallationConflictError, match="multiple owners"):
        LifecycleManager(pack).uninstall("A", dry_run=True)
    assert game_file(pack, "A").exists()
