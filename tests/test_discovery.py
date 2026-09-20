import json
import os
import shutil
from pathlib import Path

import pytest

from modsync.cli import _select_installation, main
from modsync.config import load_modpack
from modsync.discovery import (
    DiscoveryContext,
    DiscoveryRegistry,
    GameDiscoveryProvider,
    SteamDiscoveryProvider,
    parse_keyvalues,
)
from modsync.discovery.base import canonical_path_key
from modsync.exceptions import (
    DiscoveryMetadataError,
    DiscoveryProviderError,
    DiscoverySelectionError,
)
from modsync.games.registry import build_default_game_registry
from modsync.games.valheim import ValheimAdapter
from modsync.models import GameInstallation
from modsync.profiles import ProfileStore

_TEST_PLATFORM = "windows" if os.name == "nt" else "linux"


def _quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _manifest(library: Path, install_dir: str = "Valheim", app_id: str = "892970") -> Path:
    steamapps = library / "steamapps"
    steamapps.mkdir(parents=True, exist_ok=True)
    path = steamapps / "appmanifest_892970.acf"
    path.write_text(
        f'"AppState"\n{{\n"appid" "{app_id}"\n"installdir" "{_quote(install_dir)}"\n}}',
        encoding="utf-8",
    )
    return path


def _game(library: Path, install_dir: str = "Valheim") -> Path:
    root = library / "steamapps" / "common" / install_dir
    root.mkdir(parents=True, exist_ok=True)
    (root / "valheim.x86_64").write_bytes(b"game")
    return root.resolve()


def _library_file(root: Path, paths: list[str], *, legacy: bool = False) -> Path:
    steamapps = root / "steamapps"
    steamapps.mkdir(parents=True, exist_ok=True)
    entries = []
    for index, path in enumerate(paths):
        if legacy:
            entries.append(f'"{index}" "{_quote(path)}"')
        else:
            entries.append(f'"{index}" {{ "path" "{_quote(path)}" "apps" {{ }} }}')
    target = steamapps / "libraryfolders.vdf"
    target.write_text('"libraryfolders"\n{\n' + "\n".join(entries) + "\n}", encoding="utf-8")
    return target


def _provider(root: Path, platform: str | None = None, **kwargs) -> SteamDiscoveryProvider:
    return SteamDiscoveryProvider(
        DiscoveryContext(
            platform=platform or _TEST_PLATFORM,
            home=root.parent,
            steam_roots=(root,),
            **kwargs,
        )
    )


def _registry(provider: SteamDiscoveryProvider) -> DiscoveryRegistry:
    registry = DiscoveryRegistry(build_default_game_registry())
    registry.register(provider)
    return registry


class _StaticProvider(GameDiscoveryProvider):
    provider_id = "steam"

    def __init__(self, installations):
        self.installations = installations
        self.steam_found = True

    def discover(self, adapter):
        return list(self.installations)


def _modpack(path: Path, install_directory: str | None = None, game: str = "valheim") -> Path:
    data = {"name": "Pack", "version": "1", "game": game, "mods": []}
    if install_directory is not None:
        data["install_directory"] = install_directory
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('"a" "b"', {"a": "b"}),
        ('a b', {"a": "b"}),
        ('"a" { "b" "c" }', {"a": {"b": "c"}}),
        ('// comment\n"a" "b"', {"a": "b"}),
        ('\ufeff"a" "b"', {"a": "b"}),
        ('"a" "line\\nnext"', {"a": "line\nnext"}),
        ('"a" "D:\\SteamLibrary"', {"a": "D:\\SteamLibrary"}),
        ('"a" "D:\\\\SteamLibrary"', {"a": "D:\\SteamLibrary"}),
        ('"a" "say \\"yes\\""', {"a": 'say "yes"'}),
        ('"a" {}', {"a": {}}),
    ],
)
def test_keyvalues_valid_forms(text, expected):
    assert parse_keyvalues(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        '"a"',
        '"a" {',
        '}',
        '{',
        '"a" }',
        '"a" "b" "A" "c"',
        '"" "b"',
        '"a" "unterminated',
        '"a" "escape\\',
        '"a" { } }',
        '"a" { "b" }',
        '"a" { { } }',
        '"a" "b"\x00',
        "\n".join(f'"a{i}" {{' for i in range(34)) + "\n" + "}" * 34,
    ],
)
def test_keyvalues_rejects_malformed_input(text):
    with pytest.raises(DiscoveryMetadataError):
        parse_keyvalues(text)


@pytest.mark.parametrize(
    ("platform", "suffix"),
    [
        ("linux", ".local/share/Steam"),
        ("linux", ".steam/steam"),
        ("linux", ".var/app/com.valvesoftware.Steam/.local/share/Steam"),
        ("darwin", "Library/Application Support/Steam"),
    ],
)
def test_platform_default_roots_include_native_locations(tmp_path, platform, suffix):
    provider = SteamDiscoveryProvider(DiscoveryContext(platform=platform, home=tmp_path))
    assert tmp_path / suffix in provider._root_candidates()


def test_windows_roots_include_registry_and_both_program_files(tmp_path):
    registered = tmp_path / "RegistrySteam"
    context = DiscoveryContext(
        platform="win32",
        home=tmp_path,
        environ={"PROGRAMFILES(X86)": "C:/PF86", "PROGRAMFILES": "C:/PF"},
        registry_reader=lambda: (registered,),
    )
    roots = SteamDiscoveryProvider(context)._root_candidates()
    assert registered in roots
    assert Path("C:/PF86") / "Steam" in roots
    assert Path("C:/PF") / "Steam" in roots


def test_windows_registry_access_error_falls_back_to_defaults(tmp_path):
    def unavailable():
        raise PermissionError("registry denied")

    provider = SteamDiscoveryProvider(
        DiscoveryContext(
            platform="windows",
            home=tmp_path,
            environ={},
            registry_reader=unavailable,
        )
    )
    assert Path("C:/Program Files (x86)/Steam") in provider._root_candidates()


def test_valheim_declares_steam_app_id():
    assert ValheimAdapter.steam_app_id == 892970


def test_discovers_valheim_in_primary_library(tmp_path):
    root = tmp_path / "Steam"
    root.mkdir()
    _library_file(root, [str(root)])
    expected = _game(root)
    _manifest(root)
    results = _provider(root).discover(ValheimAdapter())
    assert len(results) == 1
    assert results[0].install_path == expected
    assert results[0].validated is True
    assert results[0].app_id == 892970


@pytest.mark.parametrize("platform", ["windows", "macos", "linux"])
def test_synthetic_discovery_runs_for_each_platform(tmp_path, platform):
    root = tmp_path / "Steam"
    root.mkdir()
    _game(root)
    _manifest(root)
    result = _provider(root, platform=platform).discover(ValheimAdapter())
    assert result[0].platform == platform


def test_discovers_valheim_in_additional_library(tmp_path):
    root = tmp_path / "Steam"
    library = tmp_path / "Games"
    root.mkdir()
    library.mkdir()
    _library_file(root, [str(root), str(library)])
    expected = _game(library)
    _manifest(library)
    assert _provider(root).discover(ValheimAdapter())[0].install_path == expected


def test_discovers_legacy_libraryfolders_format(tmp_path):
    root = tmp_path / "Steam"
    library = tmp_path / "Games"
    root.mkdir()
    library.mkdir()
    _library_file(root, [str(library)], legacy=True)
    _game(library)
    _manifest(library)
    assert len(_provider(root).discover(ValheimAdapter())) == 1


def test_missing_libraryfolders_still_checks_primary_library(tmp_path):
    root = tmp_path / "Steam"
    root.mkdir()
    _game(root)
    _manifest(root)
    assert len(_provider(root).discover(ValheimAdapter())) == 1


def test_missing_manifest_is_not_an_error(tmp_path):
    root = tmp_path / "Steam"
    root.mkdir()
    _library_file(root, [str(root)])
    provider = _provider(root)
    assert provider.discover(ValheimAdapter()) == []
    assert provider.steam_found is True


def test_missing_steam_root_is_not_an_error(tmp_path):
    provider = _provider(tmp_path / "missing")
    assert provider.discover(ValheimAdapter()) == []
    assert provider.steam_found is False


def test_malformed_libraryfolders_is_controlled_error(tmp_path):
    root = tmp_path / "Steam"
    (root / "steamapps").mkdir(parents=True)
    (root / "steamapps" / "libraryfolders.vdf").write_text('"bad" {', encoding="utf-8")
    with pytest.raises(DiscoveryMetadataError, match="could not be used safely"):
        _provider(root).discover(ValheimAdapter())


def test_malformed_libraryfolders_does_not_hide_valid_primary_manifest(tmp_path):
    root = tmp_path / "Steam"
    (root / "steamapps").mkdir(parents=True)
    (root / "steamapps" / "libraryfolders.vdf").write_text('"bad" {', encoding="utf-8")
    _game(root)
    _manifest(root)
    assert len(_provider(root).discover(ValheimAdapter())) == 1


@pytest.mark.parametrize("install_dir", ["../escape", "a/b", r"a\b", "/tmp/game", "C:game", ".", ".."])
def test_unsafe_manifest_install_directory_is_rejected(tmp_path, install_dir):
    root = tmp_path / "Steam"
    root.mkdir()
    _manifest(root, install_dir)
    with pytest.raises(DiscoveryMetadataError, match="Unsafe installdir"):
        _provider(root).discover(ValheimAdapter())


def test_wrong_app_id_is_rejected(tmp_path):
    root = tmp_path / "Steam"
    root.mkdir()
    _manifest(root, app_id="1")
    with pytest.raises(DiscoveryMetadataError, match="Unexpected app ID"):
        _provider(root).discover(ValheimAdapter())


def test_malformed_appmanifest_is_controlled_error(tmp_path):
    root = tmp_path / "Steam"
    root.mkdir()
    manifest = _manifest(root)
    manifest.write_text('"AppState" {', encoding="utf-8")
    with pytest.raises(DiscoveryMetadataError, match="Steam metadata"):
        _provider(root).discover(ValheimAdapter())


def test_missing_installdir_is_rejected(tmp_path):
    root = tmp_path / "Steam"
    root.mkdir()
    manifest = _manifest(root)
    manifest.write_text('"AppState" { "appid" "892970" }', encoding="utf-8")
    with pytest.raises(DiscoveryMetadataError, match="Unsafe installdir"):
        _provider(root).discover(ValheimAdapter())


def test_invalid_game_root_is_returned_as_unvalidated(tmp_path):
    root = tmp_path / "Steam"
    root.mkdir()
    (root / "steamapps" / "common" / "Valheim").mkdir(parents=True)
    _manifest(root)
    result = _provider(root).discover(ValheimAdapter())[0]
    assert result.validated is False
    assert "validation_error" in result.metadata


def test_duplicate_roots_are_deduplicated(tmp_path):
    root = tmp_path / "Steam"
    root.mkdir()
    _game(root)
    _manifest(root)
    context = DiscoveryContext(platform="linux", steam_roots=(root, root))
    assert len(SteamDiscoveryProvider(context).discover(ValheimAdapter())) == 1


@pytest.mark.skipif(os.name == "nt", reason="symlink creation is not guaranteed on Windows")
def test_symlinked_roots_are_deduplicated(tmp_path):
    root = tmp_path / "Steam"
    root.mkdir()
    alias = tmp_path / "SteamAlias"
    alias.symlink_to(root, target_is_directory=True)
    _game(root)
    _manifest(root)
    context = DiscoveryContext(platform="linux", steam_roots=(root, alias))
    assert len(SteamDiscoveryProvider(context).discover(ValheimAdapter())) == 1


@pytest.mark.skipif(os.name == "nt", reason="symlink creation is not guaranteed on Windows")
def test_duplicate_install_targets_across_libraries_are_deduplicated(tmp_path):
    root = tmp_path / "Steam"
    other = tmp_path / "Other"
    root.mkdir()
    other.mkdir()
    _library_file(root, [str(root), str(other)])
    target = _game(root)
    common = other / "steamapps" / "common"
    common.mkdir(parents=True)
    (common / "Valheim").symlink_to(target, target_is_directory=True)
    _manifest(root)
    _manifest(other)
    assert len(_provider(root).discover(ValheimAdapter())) == 1


def test_multiple_real_installations_are_returned(tmp_path):
    root = tmp_path / "Steam"
    other = tmp_path / "Other"
    root.mkdir()
    other.mkdir()
    _library_file(root, [str(root), str(other)])
    _game(root, "ValheimOne")
    _manifest(root, "ValheimOne")
    _game(other, "ValheimTwo")
    _manifest(other, "ValheimTwo")
    assert len(_provider(root).discover(ValheimAdapter())) == 2


def test_windows_canonical_identity_is_case_insensitive(tmp_path):
    assert canonical_path_key(tmp_path / "VALHEIM", "windows") == canonical_path_key(
        tmp_path / "valheim", "windows"
    )


def test_windows_metadata_path_uses_injected_resolver(tmp_path):
    root = tmp_path / "Steam"
    library = tmp_path / "Library"
    root.mkdir()
    library.mkdir()
    _library_file(root, [r"D:\SteamLibrary"])
    _game(library)
    _manifest(library)
    provider = _provider(
        root,
        platform="windows",
        path_resolver=lambda raw: library if raw == r"D:\SteamLibrary" else Path(raw),
    )
    assert provider.discover(ValheimAdapter())[0].library_path == library.resolve()


@pytest.mark.parametrize("path", ["relative", "../escape", "", ".."])
def test_unsafe_library_paths_are_ignored(tmp_path, path):
    root = tmp_path / "Steam"
    root.mkdir()
    _library_file(root, [path])
    assert _provider(root).discover(ValheimAdapter()) == []


def test_control_character_in_library_metadata_is_rejected(tmp_path):
    root = tmp_path / "Steam"
    root.mkdir()
    _library_file(root, ["/tmp/ok\x00bad"])
    with pytest.raises(DiscoveryMetadataError, match="Control character"):
        _provider(root).discover(ValheimAdapter())


def test_unsupported_platform_is_controlled_error(tmp_path):
    provider = SteamDiscoveryProvider(DiscoveryContext(platform="plan9", home=tmp_path))
    with pytest.raises(DiscoveryProviderError, match="unsupported"):
        provider.discover(ValheimAdapter())


def test_discovery_is_read_only(tmp_path):
    root = tmp_path / "Steam"
    root.mkdir()
    _library_file(root, [str(root)])
    _game(root)
    _manifest(root)
    before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    _provider(root).discover(ValheimAdapter())
    after = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    assert after == before


def test_discovery_does_not_require_network(tmp_path, monkeypatch):
    import socket

    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network used")),
    )
    root = tmp_path / "Steam"
    root.mkdir()
    _game(root)
    _manifest(root)
    assert len(_provider(root).discover(ValheimAdapter())) == 1


def test_registry_discovers_all_registered_games(tmp_path):
    root = tmp_path / "Steam"
    root.mkdir()
    _game(root)
    _manifest(root)
    results = _registry(_provider(root)).discover(provider="steam")
    assert [item.game_id for item in results] == ["valheim"]


def test_registry_rejects_unknown_provider():
    registry = DiscoveryRegistry(build_default_game_registry())
    with pytest.raises(DiscoveryProviderError):
        registry.get("unknown")


def test_registry_rejects_duplicate_provider(tmp_path):
    registry = _registry(_provider(tmp_path / "one"))
    with pytest.raises(ValueError):
        registry.register(_provider(tmp_path / "two"))


def test_cli_game_discover_prints_table(tmp_path, capsys):
    root = tmp_path / "Steam"
    root.mkdir()
    _game(root)
    _manifest(root)
    assert main(["game", "discover", "valheim"], discovery_registry=_registry(_provider(root))) == 0
    output = capsys.readouterr().out
    assert "Valheim | steam | 892970" in output
    assert "Valid" in output


def test_cli_reports_steam_not_found(tmp_path, capsys):
    registry = _registry(_provider(tmp_path / "missing"))
    assert main(["game", "discover", "valheim"], discovery_registry=registry) == 2
    assert "Steam was not found" in capsys.readouterr().err


def test_cli_reports_game_not_installed(tmp_path, capsys):
    root = tmp_path / "Steam"
    root.mkdir()
    registry = _registry(_provider(root))
    assert main(["game", "discover", "valheim"], discovery_registry=registry) == 2
    assert "Steam was found, but Valheim is not installed" in capsys.readouterr().err


def test_profile_create_with_discovery_persists_provenance(tmp_path):
    root = tmp_path / "Steam"
    root.mkdir()
    game = _game(root)
    _manifest(root)
    store = ProfileStore(tmp_path / "data")
    config = _modpack(tmp_path / "modpack.json")
    result = main(
        ["profile", "create", "friends", str(config), "--discover"],
        profile_store=store,
        discovery_registry=_registry(_provider(root)),
    )
    profile = store.get("friends")
    assert result == 0
    assert profile.install_directory == game
    assert profile.installation == {
        "provider": "steam",
        "game_id": "valheim",
        "app_id": 892970,
        "platform": _TEST_PLATFORM,
        "library_path": str(root.resolve()),
    }
    assert "account" not in json.loads((profile.directory / "profile.json").read_text())


def test_discovered_profile_modpack_uses_adapter_mode(tmp_path):
    config = _modpack(tmp_path / "modpack.json", game="Valheim")
    parsed = load_modpack(config, install_directory_override=tmp_path / "game")
    assert parsed.game_adapter_id == "valheim"


def test_profile_create_without_discover_keeps_legacy_error(tmp_path, capsys):
    config = _modpack(tmp_path / "modpack.json")
    assert main(
        ["profile", "create", "friends", str(config)],
        profile_store=ProfileStore(tmp_path / "data"),
    ) == 2
    assert "install_directory" in capsys.readouterr().err


def test_legacy_profile_has_no_discovery_metadata(tmp_path):
    game = tmp_path / "game"
    config = _modpack(tmp_path / "modpack.json", str(game))
    store = ProfileStore(tmp_path / "data")
    store.create("legacy", config)
    assert store.get("legacy").installation is None
    assert store.load_modpack("legacy").install_directory == game.resolve()


def test_explicit_valid_path_has_priority_over_discovery(tmp_path, capsys):
    game = tmp_path / "ManualValheim"
    game.mkdir()
    (game / "valheim.exe").write_bytes(b"game")
    config = _modpack(tmp_path / "modpack.json", str(game))
    store = ProfileStore(tmp_path / "data")
    registry = _registry(_provider(tmp_path / "missing"))
    assert main(
        ["profile", "create", "manual", str(config), "--discover"],
        profile_store=store,
        discovery_registry=registry,
    ) == 0
    assert store.get("manual").install_directory == game.resolve()
    assert store.get("manual").installation is None
    assert "explicit install_directory" in capsys.readouterr().out


def test_explicit_invalid_path_does_not_fall_back_to_discovery(tmp_path, capsys):
    manual = tmp_path / "not-game"
    manual.mkdir()
    root = tmp_path / "Steam"
    root.mkdir()
    _game(root)
    _manifest(root)
    config = _modpack(tmp_path / "modpack.json", str(manual))
    assert main(
        ["profile", "create", "bad", str(config), "--discover"],
        profile_store=ProfileStore(tmp_path / "data"),
        discovery_registry=_registry(_provider(root)),
    ) == 2
    assert "does not appear" in capsys.readouterr().err


def test_installation_option_requires_discovery(tmp_path, capsys):
    config = _modpack(tmp_path / "modpack.json", str(tmp_path / "game"))
    assert main(
        ["profile", "create", "bad", str(config), "--installation", "1"],
        profile_store=ProfileStore(tmp_path / "data"),
    ) == 2
    assert "requires --discover" in capsys.readouterr().err


def test_select_installation_by_index_and_path(tmp_path):
    one = GameInstallation("valheim", "Valheim", "steam", tmp_path / "one", 892970, "linux", tmp_path, True)
    two = GameInstallation("valheim", "Valheim", "steam", tmp_path / "two", 892970, "linux", tmp_path, True)
    assert _select_installation([one, two], "2") is two
    assert _select_installation([one, two], str(one.install_path)) is one


@pytest.mark.parametrize("selection", ["0", "3", "missing"])
def test_select_installation_rejects_invalid_choice(tmp_path, selection):
    item = GameInstallation("valheim", "Valheim", "steam", tmp_path / "one", 892970, "linux", tmp_path, True)
    with pytest.raises(DiscoverySelectionError):
        _select_installation([item], selection)


def test_noninteractive_multiple_selection_requires_option(tmp_path, capsys):
    one = GameInstallation("valheim", "Valheim", "steam", tmp_path / "one", 892970, "linux", tmp_path, True)
    two = GameInstallation("valheim", "Valheim", "steam", tmp_path / "two", 892970, "linux", tmp_path, True)
    with pytest.raises(DiscoverySelectionError, match="Multiple Valheim installations"):
        _select_installation([one, two], None)
    assert "INDEX | GAME" in capsys.readouterr().out


def test_profile_relocate_changes_binding_without_moving_files(tmp_path):
    old = tmp_path / "Old"
    old.mkdir()
    (old / "valheim.exe").write_bytes(b"old")
    config = _modpack(tmp_path / "modpack.json", str(old))
    store = ProfileStore(tmp_path / "data")
    store.create("friends", config)
    root = tmp_path / "Steam"
    root.mkdir()
    new = _game(root)
    _manifest(root)
    (new / "keep.txt").write_text("new", encoding="utf-8")
    assert main(
        ["profile", "relocate", "friends", "--discover"],
        profile_store=store,
        discovery_registry=_registry(_provider(root)),
    ) == 0
    assert store.get("friends").install_directory == new
    assert (old / "valheim.exe").read_bytes() == b"old"
    assert (new / "keep.txt").read_text(encoding="utf-8") == "new"


def test_profile_relocate_rejects_invalid_discovered_target(tmp_path, capsys):
    old = tmp_path / "Old"
    old.mkdir()
    config = _modpack(tmp_path / "modpack.json", str(old))
    store = ProfileStore(tmp_path / "data")
    store.create("friends", config)
    root = tmp_path / "Steam"
    root.mkdir()
    (root / "steamapps" / "common" / "Valheim").mkdir(parents=True)
    _manifest(root)
    assert main(
        ["profile", "relocate", "friends", "--discover"],
        profile_store=store,
        discovery_registry=_registry(_provider(root)),
    ) == 2
    assert store.get("friends").install_directory == old.resolve()
    assert "No valid discovered installation" in capsys.readouterr().err


def test_profile_relocate_rejects_wrong_game_identity(tmp_path, capsys):
    old = tmp_path / "Old"
    old.mkdir()
    config = _modpack(tmp_path / "modpack.json", str(old))
    store = ProfileStore(tmp_path / "data")
    store.create("friends", config)
    candidate = GameInstallation(
        "other-game", "Other", "steam", tmp_path / "Other", 1, "linux", tmp_path, True
    )
    registry = _registry(_StaticProvider([candidate]))
    assert main(
        ["profile", "relocate", "friends", "--discover"],
        profile_store=store,
        discovery_registry=registry,
    ) == 2
    assert "different game" in capsys.readouterr().err


def test_profile_rediscovery_after_steam_library_move(tmp_path):
    steam = tmp_path / "Steam"
    steam.mkdir()
    old_game = _game(steam)
    _manifest(steam)
    store = ProfileStore(tmp_path / "data")
    config = _modpack(tmp_path / "modpack.json")
    registry = _registry(_provider(steam))
    assert main(
        ["profile", "create", "friends", str(config), "--discover"],
        profile_store=store,
        discovery_registry=registry,
    ) == 0
    shutil.rmtree(old_game)
    (steam / "steamapps" / "appmanifest_892970.acf").unlink()
    moved = tmp_path / "MovedLibrary"
    moved.mkdir()
    new_game = _game(moved)
    _manifest(moved)
    _library_file(steam, [str(moved)])
    assert main(
        ["profile", "relocate", "friends", "--discover"],
        profile_store=store,
        discovery_registry=registry,
    ) == 0
    assert store.get("friends").install_directory == new_game


def test_missing_discovered_root_suggests_relocation(tmp_path, capsys):
    root = tmp_path / "Steam"
    root.mkdir()
    _game(root)
    _manifest(root)
    store = ProfileStore(tmp_path / "data")
    config = _modpack(tmp_path / "modpack.json")
    registry = _registry(_provider(root))
    assert main(
        ["profile", "create", "friends", str(config), "--discover"],
        profile_store=store,
        discovery_registry=registry,
    ) == 0
    shutil.rmtree(root / "steamapps" / "common" / "Valheim")
    assert main(["info", "--profile", "friends"], profile_store=store) == 2
    assert "profile relocate friends --discover" in capsys.readouterr().err


def test_profile_info_displays_provider_metadata(tmp_path, capsys):
    root = tmp_path / "Steam"
    root.mkdir()
    _game(root)
    _manifest(root)
    store = ProfileStore(tmp_path / "data")
    config = _modpack(tmp_path / "modpack.json")
    registry = _registry(_provider(root))
    main(["profile", "create", "friends", str(config), "--discover"], profile_store=store, discovery_registry=registry)
    assert main(["profile", "info", "friends"], profile_store=store, discovery_registry=registry) == 0
    output = capsys.readouterr().out
    assert "Installation provider: steam" in output
    assert "Provider app ID: 892970" in output
