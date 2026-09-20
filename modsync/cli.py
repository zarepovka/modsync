"""Command-line entry point for ModSync."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path

from . import __version__
from .backup import BackupManager
from .config import load_modpack
from .exceptions import ModSyncError, ProfileError
from .installer import Installer
from .models import Mod, Modpack, Profile
from .profiles import ProfileStore
from .state import STATE_FILENAME, load_state_file
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

    profile = subparsers.add_parser("profile", help="Create and manage stored profiles")
    profile_commands = profile.add_subparsers(dest="profile_command", required=True)
    profile_commands.add_parser("list", help="List profiles")
    profile_create = profile_commands.add_parser("create", help="Create a profile")
    profile_create.add_argument("name")
    profile_create.add_argument("modpack", type=Path)
    profile_info = profile_commands.add_parser("info", help="Show profile information")
    profile_info.add_argument("name")
    profile_activate = profile_commands.add_parser("activate", help="Set the active profile")
    profile_activate.add_argument("name")
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


def _run_install(modpack: Modpack, *, updating: bool) -> int:
    label = "Updating" if updating else "Installing"
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
        report = installer.update_modpack(modpack, progress)
    else:
        report = installer.install_modpack(modpack, progress)
    if last_mod is not None:
        print()
    if report.backup_id is not None:
        print(f"Backup created: {report.backup_id}")
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
    print("NAME | SOURCE | VERSION | STATUS")
    for mod in modpack.mods:
        source_type = mod.source.type if mod.source is not None else "direct"
        record = records.get(mod.name)
        resolved_version = record.get("version") if isinstance(record, dict) else None
        version = resolved_version or mod.version or "not resolved"
        options = mod.source.options if mod.source is not None else {}
        selector = options.get("version") or options.get("release")
        policy = "latest" if selector == "latest" else "pinned"
        status = policy if mod.enabled else f"disabled/{policy}"
        print(f"{mod.name} | source: {source_type} | {version} | {status}")
    for name, record in sorted(dependency_records.items(), key=lambda item: item[0].casefold()):
        source = record.get("source")
        source_type = source.get("type") if isinstance(source, dict) else "unknown"
        version = record.get("version") or "unknown"
        print(f"{name} | source: {source_type} | {version} | dependency")
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
    _warn_shared_install(store, profile.name)
    return _Target(store.load_modpack(profile.name), profile.name)


def _warn_shared_install(store: ProfileStore, profile_name: str) -> None:
    shared = store.shared_install_profiles(profile_name)
    if not shared:
        return
    print(
        "WARNING: Profiles share the same physical mod directory. "
        "Their ModSync state and backups remain separate, but installed files may overlap. "
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


def _run_profile_command(args: argparse.Namespace, store: ProfileStore) -> int:
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
        profile = store.create(args.name, args.modpack)
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
    argv: Sequence[str] | None = None, *, profile_store: ProfileStore | None = None
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    store = profile_store or ProfileStore()
    try:
        if args.command == "profile":
            return _run_profile_command(args, store)

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

        mutating = args.command in {"install", "update"} or (
            args.command == "backup" and args.backup_command == "restore"
        )
        lock = (
            store.lock(target.profile_name)
            if mutating and target.profile_name
            else nullcontext()
        )
        with lock:
            if args.command == "install":
                result = _run_install(target.modpack, updating=False)
            elif args.command == "update":
                result = _run_install(target.modpack, updating=True)
            elif args.command == "verify":
                result = _run_verify(target.modpack)
            elif args.command == "info":
                result = _run_info(target.modpack)
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
