"""Safe installation and transactional update operations."""

from __future__ import annotations

import shutil
import stat
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .backup import DEFAULT_RETENTION, BackupManager
from .config import mod_directory_name
from .downloader import Downloader
from .exceptions import BackupError, InstallError, ModSyncError
from .hashing import sha256_file
from .models import InstallFailure, InstallReport, Mod, Modpack, ResolvedMod
from .sources import SourceRegistry, build_default_registry
from .state import STATE_FILENAME, load_state_file, save_state_file
from .verifier import verify_mod_record, verify_modpack

MAX_ZIP_FILES = 20_000
MAX_ZIP_UNCOMPRESSED_BYTES = 4 * 1024 * 1024 * 1024
InstallProgressCallback = Callable[[Mod, int, int | None], None]
BackupManagerFactory = Callable[[Modpack], BackupManager]


@dataclass(frozen=True, slots=True)
class PreparedMod:
    """A downloaded and verified mod waiting in transaction-owned staging."""

    mod: Mod
    resolved: ResolvedMod
    staged_directory: Path
    state_record: dict[str, Any]


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
    """Coordinate downloads, staged installation, updates, and rollback."""

    def __init__(
        self,
        downloader: Downloader | None = None,
        backup_manager_factory: BackupManagerFactory = BackupManager,
        source_registry: SourceRegistry | None = None,
    ) -> None:
        self.downloader = downloader or Downloader()
        self.backup_manager_factory = backup_manager_factory
        session = getattr(self.downloader, "session", None)
        self.source_registry = source_registry or build_default_registry(session=session)

    def install_modpack(
        self,
        modpack: Modpack,
        progress: InstallProgressCallback | None = None,
    ) -> InstallReport:
        """Install missing, changed, or damaged mods with v0.1-compatible behavior."""
        root = modpack.install_directory
        root.mkdir(parents=True, exist_ok=True)
        state_path = modpack.state_path or root / STATE_FILENAME
        state = load_state_file(state_path)
        records: dict[str, Any] = state["mods"]
        report = InstallReport()

        for mod in modpack.mods:
            if not mod.enabled:
                report.skipped += 1
                continue
            try:
                resolved = self.source_registry.resolve(mod)
                existing = records.get(mod.name)
                if existing is not None and not verify_mod_record(
                    root, mod, existing, resolved=resolved
                ):
                    report.skipped += 1
                    continue
                with tempfile.TemporaryDirectory(prefix=".modsync-", dir=root) as name:
                    workspace = Path(name)
                    prepared = self.prepare_mod(workspace, mod, resolved, progress)
                    self.apply_prepared_mod(root, prepared, workspace / "previous")
                records[mod.name] = prepared.state_record
                state["modpack"] = {"name": modpack.name, "version": modpack.version}
                save_state_file(state_path, state)
                report.installed += 1
            except ModSyncError as exc:
                report.failures.append(InstallFailure(mod_name=mod.name, message=str(exc)))
            except OSError as exc:
                report.failures.append(
                    InstallFailure(mod_name=mod.name, message=f"Filesystem error: {exc}")
                )
        return report

    def update_modpack(
        self,
        modpack: Modpack,
        progress: InstallProgressCallback | None = None,
    ) -> InstallReport:
        """Update all changed mods as one backup-protected transaction."""
        root = modpack.install_directory
        root.mkdir(parents=True, exist_ok=True)
        state_path = modpack.state_path or root / STATE_FILENAME
        state = load_state_file(state_path)
        records: dict[str, Any] = state["mods"]
        report = InstallReport()
        changed: list[tuple[Mod, ResolvedMod]] = []

        for mod in modpack.mods:
            if not mod.enabled:
                report.skipped += 1
                continue
            try:
                resolved = self.source_registry.resolve(mod)
            except (ModSyncError, OSError) as exc:
                report.failures.append(
                    InstallFailure(mod_name=mod.name, message=self._error_message(exc))
                )
                return report
            existing = records.get(mod.name)
            if existing is not None and not verify_mod_record(
                root, mod, existing, resolved=resolved
            ):
                report.skipped += 1
            else:
                changed.append((mod, resolved))
        if not changed:
            return report

        with tempfile.TemporaryDirectory(prefix=".modsync-update-", dir=root) as name:
            transaction = Path(name)
            prepared_mods: list[PreparedMod] = []
            for mod, resolved in changed:
                try:
                    prepared_mods.append(
                        self.prepare_mod(
                            transaction / mod_directory_name(mod.name), mod, resolved, progress
                        )
                    )
                except (ModSyncError, OSError) as exc:
                    report.failures.append(
                        InstallFailure(mod_name=mod.name, message=self._error_message(exc))
                    )
                    return report

            backup_manager = self.backup_manager_factory(modpack)
            try:
                backup = backup_manager.create(
                    [mod for mod, _resolved in changed], state, reason="update"
                )
                report.backup_id = backup.backup_id
            except (BackupError, OSError) as exc:
                report.failures.append(
                    InstallFailure(mod_name="update", message=self._error_message(exc))
                )
                return report

            current_mod = "update"
            try:
                displaced = transaction / "displaced"
                for prepared in prepared_mods:
                    current_mod = prepared.mod.name
                    self.apply_prepared_mod(root, prepared, displaced)
                    records[prepared.mod.name] = prepared.state_record
                state["modpack"] = {"name": modpack.name, "version": modpack.version}
                current_mod = "state"
                save_state_file(state_path, state)
                current_mod = "verification"
                verification = verify_modpack(
                    modpack,
                    resolved_by_name={item.resolved.name: item.resolved for item in prepared_mods},
                )
                if not verification.ok:
                    first = verification.issues[0]
                    raise InstallError(
                        f"Post-update verification failed for {first.mod_name}: {first.message}"
                    )
            except Exception as exc:
                report.failures.append(
                    InstallFailure(mod_name=current_mod, message=self._error_message(exc))
                )
                report.rollback_attempted = True
                try:
                    backup_manager.restore(backup.backup_id)
                    report.rollback_succeeded = True
                except Exception as rollback_exc:
                    report.rollback_succeeded = False
                    report.rollback_error = self._error_message(rollback_exc)
                return report

            report.installed = len(prepared_mods)
            try:
                backup_manager.prune(DEFAULT_RETENTION)
            except BackupError as exc:
                report.warnings.append(f"Could not apply backup retention: {exc}")
            return report

    def prepare_mod(
        self,
        workspace: Path,
        mod: Mod,
        resolved: ResolvedMod,
        progress: InstallProgressCallback | None,
    ) -> PreparedMod:
        """Download, verify, and extract a mod without touching the installation."""
        workspace.mkdir(parents=True, exist_ok=True)
        download_directory = workspace / "download"
        download_directory.mkdir()
        download_progress = None
        if progress is not None:
            download_progress = lambda downloaded, total: progress(mod, downloaded, total)
        artifact = self.downloader.download(resolved, download_directory, download_progress)
        artifact_digest = sha256_file(artifact)
        if resolved.sha256 is not None and artifact_digest != resolved.sha256:
            raise InstallError(
                f"SHA256 mismatch for {mod.name}: expected {resolved.sha256}, got {artifact_digest}"
            )

        staged = workspace / "staged"
        staged.mkdir()
        if zipfile.is_zipfile(artifact):
            safe_extract_zip(artifact, staged)
        else:
            shutil.copy2(artifact, staged / artifact.name)

        files = _file_manifest(staged)
        if not files:
            raise InstallError(f"The downloaded artifact for {mod.name} contains no files")
        return PreparedMod(
            mod=mod,
            resolved=resolved,
            staged_directory=staged,
            state_record={
                "version": resolved.version,
                "source_sha256": artifact_digest,
                "source": {
                    **resolved.source_metadata,
                    "identity": resolved.source_identity,
                    "release": resolved.release_metadata,
                    "sha256": artifact_digest,
                },
                "directory": mod_directory_name(mod.name),
                "files": files,
            },
        )

    def apply_prepared_mod(
        self,
        root: Path,
        prepared: PreparedMod,
        displaced_root: Path,
    ) -> None:
        """Swap one prepared mod into place; overridable for failure-injection tests."""
        directory = mod_directory_name(prepared.mod.name)
        target = root / directory
        if target.is_symlink():
            raise InstallError(f"Refusing to replace symbolic link: {directory}")
        displaced_root.mkdir(parents=True, exist_ok=True)
        previous = displaced_root / directory
        if target.exists():
            target.replace(previous)
        try:
            prepared.staged_directory.replace(target)
        except OSError:
            if previous.exists() and not target.exists():
                previous.replace(target)
            raise

    @staticmethod
    def _error_message(error: Exception) -> str:
        if isinstance(error, ModSyncError):
            return str(error)
        if isinstance(error, OSError):
            return f"Filesystem error: {error}"
        return f"Unexpected installation error: {error}"
