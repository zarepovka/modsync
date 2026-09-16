"""Command-line entry point for ModSync."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from . import __version__
from .config import load_modpack
from .exceptions import ModSyncError
from .installer import Installer
from .models import Mod, Modpack
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
        command.add_argument("modpack", type=Path, help="Path to modpack.json")
    return parser


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
    # Keep output readable while still exposing byte-level progress from Downloader.
    report = installer.install_modpack(modpack, progress)
    if report.installed:
        print()
    print(
        f"Done: {report.installed} installed, {report.skipped} skipped, "
        f"{len(report.failures)} failed."
    )
    for failure in report.failures:
        print(f"  ERROR {failure.mod_name}: {failure.message}", file=sys.stderr)
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
    print(f"{modpack.name} {modpack.version}")
    print(f"Game: {modpack.game}")
    if modpack.description:
        print(f"Description: {modpack.description}")
    print(f"Install directory: {modpack.install_directory}")
    print(f"Mods: {len(modpack.mods)}")
    for mod in modpack.mods:
        status = "enabled" if mod.enabled else "disabled"
        print(f"  - {mod.name} {mod.version} [{status}]")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        modpack = load_modpack(args.modpack)
        if args.command == "install":
            return _run_install(modpack, updating=False)
        if args.command == "update":
            return _run_install(modpack, updating=True)
        if args.command == "verify":
            return _run_verify(modpack)
        return _run_info(modpack)
    except ModSyncError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
