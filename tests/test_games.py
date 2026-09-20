import json
import os
import shutil
import zipfile
from pathlib import Path, PurePosixPath

import pytest

from modsync.config import load_modpack
from modsync.cli import main
from modsync.exceptions import GameAdapterError, InstallationConflictError
from modsync.games import (
    GameRegistry,
    ValheimAdapter,
    build_default_game_registry,
    collision_key,
    validate_plan,
    validate_relative_destination,
)
from modsync.hashing import sha256_file
from modsync.installer import Installer
from modsync.models import (
    InstallationPlan,
    InstallationPlanEntry,
    Mod,
    Modpack,
    ResolvedMod,
    SourceSpec,
)
from modsync.sources import SourceRegistry
from modsync.profiles import ProfileStore
from modsync.state import load_state_file
from modsync.verifier import verify_modpack


class CopyDownloader:
    def __init__(self, archives):
        self.archives = archives

    def download(self, resolved, destination, progress=None):
        source = self.archives[resolved.name]
        target = destination / source.name
        shutil.copy2(source, target)
        return target


class StaticSource:
    def __init__(self, resolved):
        self.resolved = resolved

    def resolve(self, mod):
        return self.resolved[mod.name]


def resolved(name="ExampleMod", version="1", *, source="direct", dependencies=()):
    metadata = {"type": source}
    if source == "thunderstore":
        metadata.update(namespace="Author", package=name)
    return ResolvedMod(
        name=name,
        version=version,
        download_url=f"https://example.com/{name}.zip",
        filename=f"{name}.zip",
        sha256=None,
        source_metadata=metadata,
        release_metadata={},
        source_identity={"type": source, "name": name, "version": version},
        dependencies=dependencies,
        provider_key=f"{source}:{name}",
    )


def game_root(tmp_path):
    root = tmp_path / "Valheim"
    (root / "BepInEx" / "core").mkdir(parents=True)
    (root / "valheim.exe").write_bytes(b"")
    return root


def staged(tmp_path, files):
    root = tmp_path / "staged"
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return root


def plan_for(tmp_path, files, *, source="direct", name="ExampleMod"):
    root = game_root(tmp_path)
    stage = staged(tmp_path, files)
    return ValheimAdapter().build_installation_plan(root, [(name, resolved(name, source=source), stage)])


def destinations(plan):
    return {entry.destination.as_posix() for entry in plan.entries}


def archive(path, files):
    with zipfile.ZipFile(path, "w") as bundle:
        for name, content in files.items():
            bundle.writestr(name, content)
    return path


def adapter_pack(tmp_path, mods, *, version="1", state_path=None, backup_directory=None):
    return Modpack(
        "Pack",
        version,
        "",
        "valheim",
        game_root(tmp_path),
        tuple(mods),
        tmp_path / "modpack.json",
        state_path=state_path,
        backup_directory=backup_directory,
        game_adapter_id="valheim",
    )


def registry(values):
    result = SourceRegistry()
    result.register("direct", StaticSource(values))
    result.register("github", StaticSource(values))
    result.register("thunderstore", StaticSource(values))
    return result


def test_game_registry_selects_valheim_and_display_alias():
    games = build_default_game_registry()
    assert isinstance(games.get("valheim"), ValheimAdapter)
    assert games.get("Valheim") is games.get("valheim")


def test_game_registry_rejects_unknown_game():
    with pytest.raises(GameAdapterError, match="Unsupported game"):
        GameRegistry().get("unknown")


@pytest.mark.parametrize("marker", ["valheim.exe", "valheim.x86_64", "valheim.app", "valheim_Data"])
def test_valheim_validation_is_cross_platform(tmp_path, marker):
    root = tmp_path / "game"
    root.mkdir()
    (root / marker).mkdir() if marker.endswith((".app", "_Data")) else (root / marker).write_bytes(b"")
    ValheimAdapter().validate_game(root)


def test_valheim_validation_rejects_unrelated_directory(tmp_path):
    with pytest.raises(GameAdapterError, match="does not appear"):
        ValheimAdapter().validate_game(tmp_path)


def test_bepinex_detection(tmp_path):
    root = tmp_path / "game"
    root.mkdir()
    assert not ValheimAdapter.has_bepinex(root)
    (root / "BepInEx" / "plugins").mkdir(parents=True)
    assert ValheimAdapter.has_bepinex(root)


@pytest.mark.parametrize(
    ("source_path", "destination"),
    [
        ("plugins/Test.dll", "BepInEx/plugins/ExampleMod/Test.dll"),
        ("BepInEx/plugins/Test.dll", "BepInEx/plugins/ExampleMod/Test.dll"),
        ("core/Core.dll", "BepInEx/core/ExampleMod/Core.dll"),
        ("patchers/Fix.dll", "BepInEx/patchers/ExampleMod/Fix.dll"),
        ("monomod/Patch.dll", "BepInEx/monomod/ExampleMod/Patch.dll"),
        ("config/settings.cfg", "BepInEx/config/settings.cfg"),
        ("Patch.mm.dll", "BepInEx/monomod/ExampleMod/Patch.mm.dll"),
        ("plugins/assets/data.json", "BepInEx/plugins/ExampleMod/assets/data.json"),
        ("assets/data.json", "BepInEx/plugins/ExampleMod/data.json"),
    ],
)
def test_bepinex_routing(tmp_path, source_path, destination):
    assert destinations(plan_for(tmp_path, {source_path: b"x"})) == {destination}


def test_thunderstore_owner_uses_namespace_and_package(tmp_path):
    plan = plan_for(tmp_path, {"plugin.dll": b"x"}, source="thunderstore", name="CoolMod")
    assert destinations(plan) == {"BepInEx/plugins/Author-CoolMod/plugin.dll"}


def test_package_metadata_is_not_installed(tmp_path):
    plan = plan_for(
        tmp_path,
        {"manifest.json": b"{}", "README.md": b"r", "CHANGELOG.md": b"c", "icon.png": b"i", "mod.dll": b"m"},
    )
    assert destinations(plan) == {"BepInEx/plugins/ExampleMod/mod.dll"}


def test_double_bepinex_layout_is_rejected(tmp_path):
    with pytest.raises(GameAdapterError, match="Malformed package"):
        plan_for(tmp_path, {"BepInEx/BepInEx/plugins/mod.dll": b"x"})


def test_missing_bepinex_is_rejected(tmp_path):
    root = tmp_path / "game"
    root.mkdir()
    (root / "valheim.exe").write_bytes(b"")
    stage = staged(tmp_path, {"mod.dll": b"x"})
    with pytest.raises(GameAdapterError, match="BepInEx does not appear"):
        ValheimAdapter().build_installation_plan(root, [("Mod", resolved("Mod"), stage)])


def test_explicit_bepinex_package_can_bootstrap_loader(tmp_path):
    root = tmp_path / "game"
    root.mkdir()
    (root / "valheim.exe").write_bytes(b"")
    stage = staged(tmp_path, {"doorstop_config.ini": b"x", "BepInEx/core/core.dll": b"x"})
    loader = resolved("BepInExPack_Valheim", source="thunderstore")
    plan = ValheimAdapter().build_installation_plan(root, [("BepInEx", loader, stage)])
    assert destinations(plan) == {"doorstop_config.ini", "BepInEx/core/core.dll"}


@pytest.mark.parametrize("path", ["../x", "/x", "C:/x", "//server/share", "safe/../../x", "bad\x00x"])
def test_destination_validation_rejects_escapes(path):
    with pytest.raises(GameAdapterError):
        validate_relative_destination(path)


def manual_plan(root, entries):
    return InstallationPlan("valheim", root, tuple(entries))


def entry(source, destination, owner="Owner", mod="Mod"):
    return InstallationPlanEntry(source, PurePosixPath(destination), owner, mod)


def test_duplicate_destination_is_rejected(tmp_path):
    root = game_root(tmp_path)
    source = tmp_path / "x"
    source.write_bytes(b"x")
    plan = manual_plan(root, [entry(source, "BepInEx/config/x.cfg", "A"), entry(source, "BepInEx/config/x.cfg", "B")])
    with pytest.raises(InstallationConflictError, match="A and B"):
        validate_plan(plan)


def test_case_insensitive_collision_is_explicitly_testable(tmp_path):
    root = game_root(tmp_path)
    source = tmp_path / "x"
    source.write_bytes(b"x")
    plan = manual_plan(root, [entry(source, "BepInEx/plugins/Test.dll", "A"), entry(source, "BepInEx/plugins/test.dll", "B")])
    validate_plan(plan, case_sensitive=True)
    with pytest.raises(InstallationConflictError):
        validate_plan(plan, case_sensitive=False)
    assert collision_key("A/Test.dll", case_sensitive=False) == collision_key("a/test.dll", case_sensitive=False)


def test_unmanaged_existing_file_is_protected(tmp_path):
    root = game_root(tmp_path)
    target = root / "BepInEx" / "config" / "x.cfg"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"mine")
    source = tmp_path / "x"
    source.write_bytes(b"new")
    with pytest.raises(InstallationConflictError, match="unmanaged"):
        validate_plan(manual_plan(root, [entry(source, "BepInEx/config/x.cfg")]))
    assert target.read_bytes() == b"mine"


def test_legitimate_managed_replacement_is_allowed(tmp_path):
    root = game_root(tmp_path)
    target = root / "BepInEx" / "config" / "x.cfg"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"old")
    source = tmp_path / "x"
    source.write_bytes(b"new")
    validate_plan(
        manual_plan(root, [entry(source, "BepInEx/config/x.cfg")]),
        managed_owners={"BepInEx/config/x.cfg": "Owner"},
    )


def test_symlink_escape_is_rejected(tmp_path):
    root = game_root(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "BepInEx" / "plugins"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    source = tmp_path / "x"
    source.write_bytes(b"x")
    with pytest.raises(GameAdapterError, match="symbolic link"):
        validate_plan(manual_plan(root, [entry(source, "BepInEx/plugins/x.dll")]))


def test_existing_hardlink_destination_is_rejected(tmp_path):
    root = game_root(tmp_path)
    target = root / "BepInEx" / "config" / "x.cfg"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"old")
    try:
        os.link(target, tmp_path / "linked.cfg")
    except (OSError, NotImplementedError):
        pytest.skip("hardlinks unavailable")
    source = tmp_path / "x"
    source.write_bytes(b"new")
    with pytest.raises(InstallationConflictError, match="unsafe existing"):
        validate_plan(
            manual_plan(root, [entry(source, "BepInEx/config/x.cfg")]),
            managed_owners={"BepInEx/config/x.cfg": "Owner"},
        )


def test_config_normalized_game_enables_adapter_and_legacy_display_name_does_not(tmp_path):
    def write(name, game):
        path = tmp_path / name
        path.write_text(json.dumps({"name": "P", "version": "1", "game": game, "install_directory": "game", "mods": []}), encoding="utf-8")
        return load_modpack(path)
    assert write("new.json", "valheim").game_adapter_id == "valheim"
    assert write("old.json", "Valheim").game_adapter_id is None


def test_adapter_install_records_ownership_and_verifies_sha(tmp_path):
    package = archive(tmp_path / "mod.zip", {"plugins/mod.dll": b"one"})
    mod = Mod("ExampleMod", "1", "https://example.com/mod.zip")
    value = resolved()
    pack = adapter_pack(tmp_path, [mod])
    report = Installer(CopyDownloader({"ExampleMod": package}), source_registry=registry({"ExampleMod": value})).install_modpack(pack)
    assert not report.failures
    target = pack.install_directory / "BepInEx" / "plugins" / "ExampleMod" / "mod.dll"
    state = load_state_file(pack.install_directory / ".modsync-state.json")
    owned = state["mods"]["ExampleMod"]["installed_files"][0]
    assert owned == {"path": "BepInEx/plugins/ExampleMod/mod.dll", "owner": "ExampleMod", "sha256": sha256_file(target)}
    assert verify_modpack(pack).ok
    target.write_bytes(b"damaged")
    assert not verify_modpack(pack).ok


def test_adapter_update_backup_and_restore_real_destination(tmp_path):
    first = archive(tmp_path / "v1.zip", {"mod.dll": b"one"})
    second = archive(tmp_path / "v2.zip", {"mod.dll": b"two"})
    mod1 = Mod("ExampleMod", "1", "https://example.com/1.zip")
    pack1 = adapter_pack(tmp_path, [mod1], version="1")
    Installer(CopyDownloader({"ExampleMod": first}), source_registry=registry({"ExampleMod": resolved(version="1")})).install_modpack(pack1)
    mod2 = Mod("ExampleMod", "2", "https://example.com/2.zip")
    pack2 = Modpack(**{**{field: getattr(pack1, field) for field in pack1.__dataclass_fields__}, "version": "2", "mods": (mod2,)})
    updater = Installer(CopyDownloader({"ExampleMod": second}), source_registry=registry({"ExampleMod": resolved(version="2")}))
    report = updater.update_modpack(pack2)
    target = pack2.install_directory / "BepInEx" / "plugins" / "ExampleMod" / "mod.dll"
    assert report.backup_id and target.read_bytes() == b"two"
    from modsync.backup import BackupManager
    BackupManager(pack2).restore(report.backup_id)
    assert target.read_bytes() == b"one"


def test_dry_run_builds_plan_without_modifying_game_or_state(tmp_path):
    package = archive(tmp_path / "mod.zip", {"mod.dll": b"one"})
    mod = Mod("ExampleMod", "1", "https://example.com/mod.zip")
    pack = adapter_pack(tmp_path, [mod])
    before = {item.relative_to(pack.install_directory).as_posix() for item in pack.install_directory.rglob("*")}
    report = Installer(CopyDownloader({"ExampleMod": package}), source_registry=registry({"ExampleMod": resolved()})).install_modpack(pack, dry_run=True)
    after = {item.relative_to(pack.install_directory).as_posix() for item in pack.install_directory.rglob("*")}
    assert not report.failures and report.planned_files == 1
    assert before == after
    assert not (pack.install_directory / ".modsync-state.json").exists()


def test_cli_dry_run_smoke(tmp_path, monkeypatch, capsys):
    root = game_root(tmp_path)
    package = archive(tmp_path / "mod.zip", {"mod.dll": b"one"})
    config = tmp_path / "pack.json"
    config.write_text(
        json.dumps(
            {
                "name": "Pack",
                "version": "1",
                "game": "valheim",
                "install_directory": str(root),
                "mods": [{"name": "ExampleMod", "version": "1", "url": "https://example.com/mod.zip"}],
            }
        ),
        encoding="utf-8",
    )
    installer = Installer(CopyDownloader({"ExampleMod": package}))
    monkeypatch.setattr("modsync.cli.Installer", lambda: installer)
    assert main(["install", str(config), "--dry-run"], profile_store=ProfileStore(tmp_path / "data")) == 0
    output = capsys.readouterr().out
    assert "1 packages resolved" in output
    assert "No conflicts detected." in output
    assert not (root / ".modsync-state.json").exists()


def test_profile_state_remains_separate_in_adapter_mode(tmp_path):
    package = archive(tmp_path / "mod.zip", {"mod.dll": b"one"})
    mod = Mod("ExampleMod", "1", "https://example.com/mod.zip")
    state_path = tmp_path / "profile" / "state.json"
    backups = tmp_path / "profile" / "backups"
    pack = adapter_pack(tmp_path, [mod], state_path=state_path, backup_directory=backups)
    report = Installer(CopyDownloader({"ExampleMod": package}), source_registry=registry({"ExampleMod": resolved()})).install_modpack(pack)
    assert not report.failures and state_path.is_file()
    assert not (pack.install_directory / ".modsync-state.json").exists()


def test_mixed_sources_and_dependency_install_with_adapter(tmp_path):
    dependency = resolved("Dependency", source="thunderstore")
    values = {
        "Direct": resolved("Direct"),
        "GitHub": resolved("GitHub", source="github"),
        "Root": resolved("Root", source="thunderstore", dependencies=(dependency,)),
    }
    mods = (
        Mod("Direct", "1", "https://example.com/d.zip"),
        Mod("GitHub", None, None, source=SourceSpec("github", {})),
        Mod("Root", None, None, source=SourceSpec("thunderstore", {})),
    )
    archives = {name: archive(tmp_path / f"{name}.zip", {f"{name}.dll": name.encode()}) for name in (*values, "Dependency")}
    values["Dependency"] = dependency
    pack = adapter_pack(tmp_path, mods)
    report = Installer(CopyDownloader(archives), source_registry=registry(values)).install_modpack(pack)
    assert not report.failures and report.installed == 4
    state = load_state_file(pack.install_directory / ".modsync-state.json")
    assert state["mods"]["Dependency"]["role"] == "dependency"


def test_two_packages_conflicting_in_config_stop_before_apply(tmp_path):
    mods = (Mod("A", "1", "https://example.com/a"), Mod("B", "1", "https://example.com/b"))
    values = {"A": resolved("A"), "B": resolved("B")}
    archives = {name: archive(tmp_path / f"{name}.zip", {"config/shared.cfg": name.encode()}) for name in values}
    pack = adapter_pack(tmp_path, mods)
    report = Installer(CopyDownloader(archives), source_registry=registry(values)).install_modpack(pack)
    assert report.failures and "Installation conflict" in report.failures[0].message
    assert not (pack.install_directory / "BepInEx" / "config" / "shared.cfg").exists()
