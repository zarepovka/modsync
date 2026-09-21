"""Command-line entry point for ModSync."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from contextlib import ExitStack, nullcontext
from dataclasses import dataclass
from pathlib import Path

from . import __version__
from .backup import BackupManager
from .config import has_explicit_install_directory, load_modpack
from .discovery import DiscoveryRegistry, build_default_discovery_registry
from .discovery.base import canonical_path_key
from .exceptions import DiscoveryError, DiscoverySelectionError, ModSyncError, ProfileError
from .installer import Installer
from .models import GameInstallation, Mod, Modpack, Profile
from .profiles import ProfileStore
from .services import ModSyncService
from .state import (
    STATE_FILENAME,
    load_state_file,
    record_install_reason,
    record_status,
)
from .switching import ProfileSwitcher
from .verifier import verify_modpack


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="modsync",
        description="Install and verify small, shareable modpacks.",
    )
    parser.add_argument("--version", action="version", version=f"ModSync {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("install", "Install missing, changed, or damaged mods"),
        ("verify", "Verify installed mod versions and file integrity"),
        ("update", "Update mods that differ from the modpack"),
        ("info", "Show modpack information"),
    ):
        command = subparsers.add_parser(name, help=help_text)
        command.add_argument("modpack", nargs="?", type=Path, help="Path to modpack.json")
        command.add_argument("--profile", help="Use a stored profile instead of modpack.json")
        if name in {"install", "update"}:
            command.add_argument(
                "--dry-run",
                action="store_true",
                help="Resolve, download, validate, and print the plan without changing the game",
            )

    for name, help_text in (
        ("uninstall", "Safely remove one ownership-tracked mod"),
        ("disable", "Move a mod out of the game runtime"),
        ("enable", "Restore a disabled mod to the game runtime"),
    ):
        command = subparsers.add_parser(name, help=help_text)
        command.add_argument("mod_name", help="Installed mod/package name")
        command.add_argument("modpack", nargs="?", type=Path, help="Path to modpack.json")
        command.add_argument("--profile", help="Use a stored profile instead of modpack.json")
        command.add_argument(
            "--dry-run", action="store_true", help="Validate and show changes without applying"
        )
        if name in {"uninstall", "disable"}:
            command.add_argument(
                "--force",
                action="store_true",
                help="Allow a modified runtime file after backup; security checks still apply",
            )

    backup = subparsers.add_parser("backup", help="List or restore installation backups")
    backup_commands = backup.add_subparsers(dest="backup_command", required=True)
    backup_list = backup_commands.add_parser("list", help="List available backups")
    backup_list.add_argument("modpack", nargs="?", type=Path, help="Path to modpack.json")
    backup_list.add_argument("--profile", help="Use a stored profile")
    backup_restore = backup_commands.add_parser("restore", help="Restore a verified backup")
    backup_restore.add_argument(
        "target", nargs="+", help="Legacy: modpack.json backup-id; profile mode: backup-id"
    )
    backup_restore.add_argument("--profile", help="Use a stored profile")

    game = subparsers.add_parser("game", help="Discover local game installations")
    game_commands = game.add_subparsers(dest="game_command", required=True)
    game_discover = game_commands.add_parser(
        "discover", help="Discover and validate local installations"
    )
    game_discover.add_argument("game", nargs="?", help="Game identifier, for example valheim")
    game_discover.add_argument("--provider", default="steam", help="Discovery provider")

    profile = subparsers.add_parser("profile", help="Create and manage stored profiles")
    profile_commands = profile.add_subparsers(dest="profile_command", required=True)
    profile_commands.add_parser("list", help="List profiles")
    profile_create = profile_commands.add_parser("create", help="Create a profile")
    profile_create.add_argument("name")
    profile_create.add_argument("modpack", type=Path)
    profile_create.add_argument(
        "--discover", action="store_true", help="Discover a missing installation path"
    )
    profile_create.add_argument(
        "--installation",
        help="Select a discovered installation by 1-based index or exact path",
    )
    profile_info = profile_commands.add_parser("info", help="Show profile information")
    profile_info.add_argument("name")
    profile_activate = profile_commands.add_parser("activate", help="Set the active profile")
    profile_activate.add_argument("name")
    profile_switch = profile_commands.add_parser(
        "switch", help="Reconcile the physical game root with a profile"
    )
    profile_switch.add_argument("name")
    profile_switch.add_argument(
        "--dry-run", action="store_true", help="Validate and show the transition"
    )
    profile_relocate = profile_commands.add_parser(
        "relocate", help="Rebind a profile to a rediscovered game installation"
    )
    profile_relocate.add_argument("name")
    profile_relocate.add_argument("--discover", action="store_true", required=True)
    profile_relocate.add_argument(
        "--installation",
        help="Select a discovered installation by 1-based index or exact path",
    )
    profile_commands.add_parser("status", help="Show active and physical profile status")
    profile_delete = profile_commands.add_parser("delete", help="Delete ModSync profile data")
    profile_delete.add_argument("name")
    profile_delete.add_argument("--yes", action="store_true", help="Skip confirmation")
    return parser


@dataclass(frozen=True, slots=True)
class _Target:
    modpack: Modpack
    profile_name: str | None = None


def _progress(mod: Mod) -> Callable[[int, int | None], None]:
    def display(downloaded: int, total: int | None) -> None:
        downloaded_mb = downloaded / (1024 * 1024)
        if total:
            percent = downloaded / total * 100
            total_mb = total / (1024 * 1024)
            text = (
                f"\r  {mod.name}: {downloaded_mb:.1f} MiB / "
                f"{total_mb:.1f} MiB ({percent:.0f}%)"
            )
        else:
            text = f"\r  {mod.name}: {downloaded_mb:.1f} MiB"
        print(text, end="", flush=True)

    return display


def _run_install(modpack: Modpack, *, updating: bool, dry_run: bool = False) -> int:
    label = "Updating" if updating else "Installing"
    if dry_run:
        label = "Planning"
    print(f"{label} {modpack.name} {modpack.version} into {modpack.install_directory}")
    last_mod: str | None = None

    def progress(mod: Mod, downloaded: int, total: int | None) -> None:
        nonlocal last_mod
        if last_mod is not None and last_mod != mod.name:
            print()
        last_mod = mod.name
        _progress(mod)(downloaded, total)

    installer = Installer()
    if updating:
        report = installer.update_modpack(modpack, progress, dry_run=dry_run)
    else:
        report = installer.install_modpack(modpack, progress, dry_run=dry_run)
    if last_mod is not None:
        print()
    if report.backup_id is not None:
        print(f"Backup created: {report.backup_id}")
    if dry_run and not report.failures:
        print(f"{report.resolved} packages resolved")
        print(f"{report.planned_files} files will be installed")
        for entry in report.plan_entries:
            print(f"  {entry.destination.as_posix()}")
        print("No conflicts detected.")
        return 0
    print(
        f"Done: {report.installed} installed, {report.skipped} skipped, "
        f"{len(report.failures)} failed."
    )
    for failure in report.failures:
        print(f"  ERROR {failure.mod_name}: {failure.message}", file=sys.stderr)
    if report.rollback_attempted:
        if report.rollback_succeeded:
            print("Automatic rollback completed successfully.")
            print("Your previous installation has been restored.")
        else:
            print("Automatic rollback also failed.", file=sys.stderr)
            if report.backup_id is not None:
                print(f"Backup preserved: {report.backup_id}", file=sys.stderr)
            if report.rollback_error:
                print(f"Rollback error: {report.rollback_error}", file=sys.stderr)
            print("No further destructive actions were performed.", file=sys.stderr)
    for warning in report.warnings:
        print(f"  WARNING: {warning}", file=sys.stderr)
    return 1 if report.failures else 0


def _run_verify(modpack: Modpack) -> int:
    report = verify_modpack(modpack)
    if report.ok:
        print(f"OK: {report.checked} enabled mod(s) verified successfully.")
        return 0
    print(f"Verification failed with {len(report.issues)} issue(s):")
    for issue in report.issues:
        print(f"  {issue.mod_name}: {issue.message}")
    return 1


def _run_info(modpack: Modpack) -> int:
    state_path = modpack.state_path or modpack.install_directory / STATE_FILENAME
    records = load_state_file(state_path)["mods"]
    print(f"{modpack.name} {modpack.version}")
    print(f"Game: {modpack.game}")
    if modpack.description:
        print(f"Description: {modpack.description}")
    print(f"Install directory: {modpack.install_directory}")
    explicit_names = {mod.name.casefold() for mod in modpack.mods}
    dependency_records = {
        name: record
        for name, record in records.items()
        if isinstance(name, str)
        and name.casefold() not in explicit_names
        and isinstance(record, dict)
        and record.get("role") == "dependency"
    }
    print(
        f"Mods: {len(modpack.mods)} configured, "
        f"{len(dependency_records)} dependencies recorded"
    )
    print("NAME | SOURCE | VERSION | STATUS | REASON")
    for mod in modpack.mods:
        source_type = mod.source.type if mod.source is not None else "direct"
        record = records.get(mod.name)
        resolved_version = record.get("version") if isinstance(record, dict) else None
        version = resolved_version or mod.version or "not resolved"
        options = mod.source.options if mod.source is not None else {}
        selector = options.get("version") or options.get("release")
        policy = "latest" if selector == "latest" else "pinned"
        status = record_status(record) if isinstance(record, dict) else "not-installed"
        reason = record_install_reason(record) if isinstance(record, dict) else "explicit"
        configured = policy if mod.enabled else f"configured-disabled/{policy}"
        print(
            f"{mod.name} | source: {source_type} | {version} | "
            f"{configured}; {status} | {reason}"
        )
    for name, record in sorted(dependency_records.items(), key=lambda item: item[0].casefold()):
        source = record.get("source")
        source_type = source.get("type") if isinstance(source, dict) else "unknown"
        version = record.get("version") or "unknown"
        print(
            f"{name} | source: {source_type} | {version} | "
            f"dependency; {record_status(record)} | {record_install_reason(record)}"
        )
    return 0


def _run_lifecycle(
    modpack: Modpack,
    *,
    action: str,
    mod_name: str,
    dry_run: bool,
    force: bool,
) -> int:
    installer = Installer()
    if action == "uninstall":
        report = installer.uninstall_mod(
            modpack, mod_name, dry_run=dry_run, force=force
        )
    elif action == "disable":
        report = installer.disable_mod(
            modpack, mod_name, dry_run=dry_run, force=force
        )
    else:
        report = installer.enable_mod(modpack, mod_name, dry_run=dry_run)

    label = "DRY RUN" if dry_run else action.capitalize()
    print(f"{label}: {report.mod_name}")
    for path in report.paths:
        verb = "would change" if dry_run else "changed"
        print(f"  {verb}: {path}")
    for path in report.preserved_files:
        print(f"Preserved modified configuration: {path}")
    if report.backup_id is not None:
        print(f"Backup created: {report.backup_id}")
    if report.orphan_dependencies:
        print("The following dependencies may now be unused:")
        for dependency in report.orphan_dependencies:
            print(f"- {dependency}")
        print("Run: modsync cleanup")
    if dry_run:
        print("No game files, state, disabled storage, or backups were changed.")
    return 0


def _run_backup_list(modpack: Modpack) -> int:
    backups = BackupManager(modpack).list_backups()
    if not backups:
        print("No backups found.")
        return 0
    print(f"Backups for {modpack.name}:")
    for backup in backups:
        print(
            f"  {backup.backup_id} | {backup.created_at} | "
            f"modpack {backup.modpack_version} | {backup.file_count} file(s) | "
            f"reason: {backup.reason}"
        )
    return 0


def _run_backup_restore(modpack: Modpack, backup_id: str) -> int:
    report = BackupManager(modpack).restore(backup_id)
    print(f"Backup restored: {report.backup_id}")
    print(f"Files restored: {report.restored_files}")
    print("Installation state restored and verified.")
    return 0


def _resolve_target(
    store: ProfileStore, modpack_path: Path | None, profile_name: str | None
) -> _Target:
    if modpack_path is not None and profile_name is not None:
        raise ProfileError("Use either modpack.json or --profile, not both")
    if modpack_path is not None:
        return _Target(load_modpack(modpack_path))
    selected = profile_name or store.active_name()
    if selected is None:
        raise ProfileError(
            "No active profile. Use --profile NAME, activate a profile, or provide modpack.json"
        )
    profile = store.get(selected)
    if profile.installation is not None and not profile.install_directory.is_dir():
        raise ProfileError(
            f"The discovered game installation is no longer available: "
            f"{profile.install_directory}. Run 'modsync profile relocate "
            f"{profile.name} --discover'."
        )
    _warn_shared_install(store, profile.name)
    return _Target(store.load_modpack(profile.name), profile.name)


def _warn_shared_install(store: ProfileStore, profile_name: str) -> None:
    shared = store.shared_install_profiles(profile_name)
    if not shared:
        return
    print(
        "WARNING: Profiles share the same physical mod directory. "
        "Use 'modsync profile switch NAME' to reconcile physical files safely. "
        "Profile state and backups remain separate. "
        f"Other profile(s): {', '.join(shared)}",
        file=sys.stderr,
    )


def _print_profile(profile: Profile, *, active: bool) -> None:
    print(f"Profile: {profile.name}{' (active)' if active else ''}")
    print(f"Game: {profile.game}")
    print(f"Mods: {profile.mod_count}")
    print(f"Install directory: {profile.install_directory}")
    print(f"Created: {profile.created_at}")
    print(f"Updated: {profile.updated_at}")
    print(f"Modpack source: {profile.modpack_source}")
    if profile.installation is not None:
        print(f"Installation provider: {profile.installation['provider']}")
        if profile.installation.get("app_id") is not None:
            print(f"Provider app ID: {profile.installation['app_id']}")


def _provenance(installation: GameInstallation) -> dict[str, object]:
    value: dict[str, object] = {
        "provider": installation.provider,
        "game_id": installation.game_id,
        "app_id": installation.app_id,
        "platform": installation.platform,
    }
    if installation.library_path is not None:
        value["library_path"] = str(installation.library_path)
    return value


def _print_installations(installations: Sequence[GameInstallation]) -> None:
    print("INDEX | GAME | PROVIDER | APP ID | INSTALL PATH | STATUS")
    for index, installation in enumerate(installations, 1):
        status = "Valid" if installation.validated else "Invalid"
        print(
            f"{index} | {installation.display_name} | {installation.provider} | "
            f"{installation.app_id or '-'} | {installation.install_path} | {status}"
        )


def _discover_installations(
    registry: DiscoveryRegistry, game: str, provider: str = "steam"
) -> list[GameInstallation]:
    results = registry.discover(game, provider=provider)
    if not results:
        selected_provider = registry.get(provider)
        if provider.casefold() == "steam" and not getattr(selected_provider, "steam_found", True):
            raise DiscoveryError("Steam was not found.")
        display_name = registry.games.get(game).display_name
        raise DiscoveryError(f"Steam was found, but {display_name} is not installed.")
    return results


def _select_installation(
    installations: Sequence[GameInstallation], selection: str | None
) -> GameInstallation:
    valid = [installation for installation in installations if installation.validated]
    if not valid:
        details = next(
            (
                str(item.metadata["validation_error"])
                for item in installations
                if item.metadata.get("validation_error")
            ),
            "no candidate passed game validation",
        )
        raise DiscoverySelectionError(f"No valid discovered installation: {details}")
    if selection is not None:
        try:
            index = int(selection)
        except ValueError:
            selected_path = Path(selection).expanduser().resolve(strict=False)
            matches = [
                item
                for item in valid
                if canonical_path_key(selected_path, item.platform)
                == canonical_path_key(item.install_path, item.platform)
            ]
            if len(matches) != 1:
                raise DiscoverySelectionError(
                    f"No discovered installation matches path: {selection}"
                )
            return matches[0]
        if index < 1 or index > len(installations) or not installations[index - 1].validated:
            raise DiscoverySelectionError("Selected installation index is not valid")
        return installations[index - 1]
    if len(valid) == 1:
        return valid[0]
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        _print_installations(installations)
        raise DiscoverySelectionError(
            f"Multiple {valid[0].display_name} installations were found. "
            "Select one explicitly with --installation INDEX or an exact path."
        )
    _print_installations(installations)
    answer = input("Select installation number: ").strip()
    return _select_installation(installations, answer)


def _run_profile_command(
    args: argparse.Namespace,
    store: ProfileStore,
    discovery: DiscoveryRegistry,
    service: ModSyncService,
) -> int:
    if args.profile_command == "list":
        profiles = store.list()
        active = store.active_name()
        if not profiles:
            print("No profiles found.")
            return 0
        print("NAME | GAME | MODS | ACTIVE")
        for profile in profiles:
            marker = "yes" if profile.name == active else ""
            print(f"{profile.name} | {profile.game} | {profile.mod_count} | {marker}")
        return 0
    if args.profile_command == "create":
        if args.installation is not None and not args.discover:
            raise ProfileError("--installation requires --discover")
        installation = None
        if args.discover:
            probe = load_modpack(
                args.modpack, install_directory_override=Path.cwd().resolve()
            )
            if has_explicit_install_directory(args.modpack):
                discovery.games.get(probe.game).validate_game(probe.install_directory)
                print("Using explicit install_directory; automatic discovery was not needed.")
            else:
                candidates = _discover_installations(discovery, probe.game)
                installation = _select_installation(candidates, args.installation)
        profile = service.create_profile(
            args.name,
            args.modpack,
            installation=installation,
            manual_path=(
                probe.install_directory
                if args.discover and installation is None
                else None
            ),
        )
        print(f"Profile created: {profile.name}")
        _warn_shared_install(store, profile.name)
        print(f"Activate it: modsync profile activate {profile.name}")
        print(f"Install it: modsync install --profile {profile.name}")
        return 0
    if args.profile_command == "info":
        profile = store.get(args.name)
        _print_profile(profile, active=profile.name == store.active_name())
        return 0
    if args.profile_command == "activate":
        profile = store.activate(args.name)
        print(f"Active profile: {profile.name}")
        print("Physical game files were not changed. Use 'profile switch' to reconcile them.")
        return 0
    if args.profile_command == "switch":
        selected_profile = store.get(args.name)
        if (
            selected_profile.installation is not None
            and not selected_profile.install_directory.is_dir()
        ):
            raise ProfileError(
                f"The discovered game installation is no longer available: "
                f"{selected_profile.install_directory}. Run 'modsync profile relocate "
                f"{selected_profile.name} --discover'."
            )
        report = ProfileSwitcher(store).switch(args.name, dry_run=args.dry_run)
        print(f"Switch: {report.source_profile} -> {report.target_profile}")
        print(f"Keep: {report.kept} files")
        print(f"Remove: {report.removed} files")
        print(f"Install: {report.installed} files")
        print(f"Restore profile configs: {report.restored_configs} files")
        print(f"Disable: {len(report.disabled_packages)} packages")
        print(f"Download required: {len(report.download_packages)} packages")
        print("No unmanaged conflicts detected.")
        if report.download_packages:
            print("Packages: " + ", ".join(report.download_packages))
        if report.dry_run:
            print("No active profile, files, state, storage, or backups were changed.")
        else:
            print(f"Active profile: {report.target_profile}")
            if report.backup_id:
                print(f"Backup created: {report.backup_id}")
        return 0
    if args.profile_command == "relocate":
        profile = store.get(args.name)
        candidates = _discover_installations(discovery, profile.game)
        installation = _select_installation(candidates, args.installation)
        if discovery.games.get(profile.game).game_id != installation.game_id:
            raise DiscoverySelectionError("Discovered installation is for a different game")
        relocated = store.relocate(
            profile.name, installation.install_path, _provenance(installation)
        )
        print(f"Profile relocated: {relocated.name}")
        print(f"Install directory: {relocated.install_directory}")
        print("No game or mod files were moved or changed.")
        return 0
    if args.profile_command == "status":
        active = store.active_name()
        if active is None:
            print("Active profile: none")
            return 0
        profile = store.get(active)
        verification = verify_modpack(store.load_modpack(active))
        marker = store.switch_marker(profile.install_directory)
        physical = "verified" if verification.ok else "does not match active profile"
        if marker.exists() or marker.is_symlink():
            physical = "previous switch may be incomplete"
        print(f"Active profile: {active}")
        print(f"Game root: {profile.install_directory}")
        print(f"Physical state: {physical}")
        print("Profiles using this game root:")
        names = [active, *store.shared_install_profiles(active)]
        for name in sorted(set(names), key=str.casefold):
            print(f"- {name}{' *' if name == active else ''}")
        return 0
    if not args.yes:
        answer = input(
            f"Delete ModSync data for profile {args.name!r}? Game files will not be deleted. "
            "[y/N] "
        )
        if answer.strip().casefold() not in {"y", "yes"}:
            print("Profile deletion cancelled.")
            return 0
    profile = store.delete(args.name)
    print(f"Profile deleted: {profile.name}")
    print("Installed game and mod files were not removed.")
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    profile_store: ProfileStore | None = None,
    discovery_registry: DiscoveryRegistry | None = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    store = profile_store or ProfileStore()
    discovery = discovery_registry or build_default_discovery_registry()
    service = ModSyncService(
        profile_store=store,
        discovery_registry=discovery,
        game_registry=discovery.games,
    )
    try:
        if args.command == "profile":
            return _run_profile_command(args, store, discovery, service)
        if args.command == "game":
            installations = (
                service.discover_games(args.game)
                if args.provider.casefold() == "steam"
                else discovery.discover(args.game, provider=args.provider)
            )
            if not installations:
                selected_provider = discovery.get(args.provider)
                if args.provider.casefold() == "steam" and not getattr(
                    selected_provider, "steam_found", True
                ):
                    raise DiscoveryError("Steam was not found.")
                if args.game is None:
                    raise DiscoveryError("Steam was found, but no supported games are installed.")
                game_name = discovery.games.get(args.game).display_name
                raise DiscoveryError(f"Steam was found, but {game_name} is not installed.")
            _print_installations(installations)
            return 0

        if args.command == "backup" and args.backup_command == "restore":
            if args.profile is not None:
                if len(args.target) != 1:
                    raise ProfileError("Profile restore expects one backup ID")
                target = _resolve_target(store, None, args.profile)
                backup_id = args.target[0]
            elif len(args.target) == 2:
                target = _resolve_target(store, Path(args.target[0]), None)
                backup_id = args.target[1]
            elif len(args.target) == 1:
                target = _resolve_target(store, None, None)
                backup_id = args.target[0]
            else:
                raise ProfileError("Restore expects modpack.json and backup ID")
        else:
            target = _resolve_target(store, args.modpack, args.profile)
            backup_id = None

        dry_run = bool(getattr(args, "dry_run", False))
        mutating = (
            args.command in {"install", "update", "uninstall", "disable", "enable"}
            and not dry_run
        ) or (
            args.command == "backup" and args.backup_command == "restore"
        )
        with ExitStack() as locks:
            if mutating and target.profile_name:
                locks.enter_context(store.game_root_lock(target.modpack.install_directory))
            if mutating and target.profile_name:
                locks.enter_context(store.lock(target.profile_name))
            if not mutating:
                locks.enter_context(nullcontext())
            if args.command == "install":
                result = _run_install(target.modpack, updating=False, dry_run=dry_run)
            elif args.command == "update":
                result = _run_install(target.modpack, updating=True, dry_run=dry_run)
            elif args.command == "verify":
                result = _run_verify(target.modpack)
            elif args.command == "info":
                result = _run_info(target.modpack)
            elif args.command in {"uninstall", "disable", "enable"}:
                result = _run_lifecycle(
                    target.modpack,
                    action=args.command,
                    mod_name=args.mod_name,
                    dry_run=dry_run,
                    force=bool(getattr(args, "force", False)),
                )
            elif args.backup_command == "list":
                result = _run_backup_list(target.modpack)
            else:
                result = _run_backup_restore(target.modpack, backup_id or "")
            if mutating and target.profile_name is not None:
                store.touch(target.profile_name)
            return result
    except ModSyncError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
