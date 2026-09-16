"""Safe installation and incremental update operations."""

from __future__ import annotations

import shutil
import stat
import tempfile
import zipfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

from .config import mod_directory_name
from .downloader import Downloader
from .exceptions import InstallError, ModSyncError
from .hashing import sha256_file
from .models import InstallFailure, InstallReport, Mod, Modpack
from .state import load_state, save_state
from .verifier import verify_mod_record

MAX_ZIP_FILES = 20_000
MAX_ZIP_UNCOMPRESSED_BYTES = 4 * 1024 * 1024 * 1024
InstallProgressCallback = Callable[[Mod, int, int | None], None]


def _validated_zip_path(filename: str) -> PurePosixPath:
    normalized = filename.replace("\\", "/")
    if "\x00" in normalized:
        raise InstallError("ZIP contains a filename with a null byte")
    path = PurePosixPath(normalized)
    if path.is_absolute() or any(part == ".." for part in path.parts):
        raise InstallError(f"Unsafe path in ZIP archive: {filename}")
    if path.parts and len(path.parts[0]) >= 2 and path.parts[0][1] == ":":
        raise InstallError(f"Unsafe drive-qualified path in ZIP archive: {filename}")
    return path


def safe_extract_zip(archive: Path, destination: Path) -> None:
    """Extract a ZIP while rejecting traversal, links, and oversized archives."""
    try:
        with zipfile.ZipFile(archive) as bundle:
            members = bundle.infolist()
            if len(members) > MAX_ZIP_FILES:
                raise InstallError(f"ZIP contains more than {MAX_ZIP_FILES:,} entries")
            if sum(item.file_size for item in members) > MAX_ZIP_UNCOMPRESSED_BYTES:
                raise InstallError("ZIP exceeds the 4 GiB uncompressed safety limit")

            destination_resolved = destination.resolve()
            for member in members:
                relative = _validated_zip_path(member.filename)
                mode = member.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise InstallError(
                        f"Symbolic links are not allowed in ZIP files: {member.filename}"
                    )
                if not relative.parts:
                    continue
                target = destination / Path(*relative.parts)
                try:
                    target.resolve().relative_to(destination_resolved)
                except ValueError as exc:
                    raise InstallError(f"Unsafe path in ZIP archive: {member.filename}") from exc
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(member) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
    except zipfile.BadZipFile as exc:
        raise InstallError(f"Invalid ZIP archive {archive.name}: {exc}") from exc
    except OSError as exc:
        raise InstallError(f"Could not extract {archive.name}: {exc}") from exc


def _file_manifest(directory: Path) -> dict[str, str]:
    return {
        item.relative_to(directory).as_posix(): sha256_file(item)
        for item in sorted(directory.rglob("*"))
        if item.is_file()
    }


class Installer:
    """Coordinate download, verification, staged installation, and state updates."""

    def __init__(self, downloader: Downloader | None = None) -> None:
        self.downloader = downloader or Downloader()

    def install_modpack(
        self,
        modpack: Modpack,
        progress: InstallProgressCallback | None = None,
    ) -> InstallReport:
        """Install missing, changed, or damaged enabled mods from a modpack."""
        root = modpack.install_directory
        root.mkdir(parents=True, exist_ok=True)
        state = load_state(root)
        records: dict[str, Any] = state["mods"]
        report = InstallReport()

        for mod in modpack.mods:
            if not mod.enabled:
                report.skipped += 1
                continue
            existing = records.get(mod.name)
            if existing is not None and not verify_mod_record(root, mod, existing):
                report.skipped += 1
                continue
            try:
                records[mod.name] = self._install_one(root, mod, progress)
                state["modpack"] = {"name": modpack.name, "version": modpack.version}
                save_state(root, state)
                report.installed += 1
            except ModSyncError as exc:
                report.failures.append(InstallFailure(mod_name=mod.name, message=str(exc)))
            except OSError as exc:
                report.failures.append(
                    InstallFailure(mod_name=mod.name, message=f"Filesystem error: {exc}")
                )
        return report

    def _install_one(
        self,
        root: Path,
        mod: Mod,
        progress: InstallProgressCallback | None,
    ) -> dict[str, Any]:
        slug = mod_directory_name(mod.name)
        with tempfile.TemporaryDirectory(prefix=".modsync-", dir=root) as temporary_name:
            temporary = Path(temporary_name)
            download_directory = temporary / "download"
            download_directory.mkdir()
            download_progress = None
            if progress is not None:
                download_progress = lambda downloaded, total: progress(mod, downloaded, total)
            artifact = self.downloader.download(mod, download_directory, download_progress)
            artifact_digest = sha256_file(artifact)
            if mod.sha256 is not None and artifact_digest != mod.sha256:
                raise InstallError(
                    f"SHA256 mismatch for {mod.name}: expected {mod.sha256}, got {artifact_digest}"
                )

            staged = temporary / "staged"
            staged.mkdir()
            if zipfile.is_zipfile(artifact):
                safe_extract_zip(artifact, staged)
            else:
                shutil.copy2(artifact, staged / artifact.name)

            files = _file_manifest(staged)
            if not files:
                raise InstallError(f"The downloaded artifact for {mod.name} contains no files")

            target = root / slug
            backup = temporary / "previous"
            if target.exists():
                target.replace(backup)
            try:
                staged.replace(target)
            except OSError:
                if backup.exists() and not target.exists():
                    backup.replace(target)
                raise

            return {
                "version": mod.version,
                "source_sha256": artifact_digest,
                "directory": slug,
                "files": files,
            }
