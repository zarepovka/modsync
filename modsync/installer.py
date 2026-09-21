"""Safe installation and transactional update operations."""

from __future__ import annotations

import shutil
import stat
import tempfile
import uuid
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
from .games import GameRegistry, build_default_game_registry, validate_plan
from .models import (
    InstallFailure,
    InstallReport,
    Mod,
    Modpack,
    ResolvedMod,
    ResolvedPlanItem,
    InstallationPlan,
    LifecycleReport,
)
from .sources import SourceRegistry, build_default_registry
from .state import (
    STATE_FILENAME,
    atomic_write_bytes,
    load_state_file,
    record_status,
    save_state_file,
)
from .verifier import verify_mod_record, verify_resolved_plan

MAX_ZIP_FILES = 20_000
MAX_ZIP_UNCOMPRESSED_BYTES = 4 * 1024 * 1024 * 1024
InstallProgressCallback = Callable[[Mod, int, int | None], None]
OperationPhaseCallback = Callable[[str, bool], None]
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
        game_registry: GameRegistry | None = None,
    ) -> None:
        self.downloader = downloader or Downloader()
        self.backup_manager_factory = backup_manager_factory
        session = getattr(self.downloader, "session", None)
        self.source_registry = source_registry or build_default_registry(session=session)
        self.game_registry = game_registry or build_default_game_registry()

    def uninstall_mod(
        self, modpack: Modpack, mod_name: str, *, dry_run: bool = False, force: bool = False
    ) -> LifecycleReport:
        """Safely remove one ownership-tracked adapter package."""
        from .lifecycle import LifecycleManager

        return LifecycleManager(modpack).uninstall(
            mod_name, dry_run=dry_run, force=force
        )

    def disable_mod(
        self, modpack: Modpack, mod_name: str, *, dry_run: bool = False, force: bool = False
    ) -> LifecycleReport:
        """Move one package's runtime files into protected disabled storage."""
        from .lifecycle import LifecycleManager

        return LifecycleManager(modpack).disable(
            mod_name, dry_run=dry_run, force=force
        )

    def enable_mod(
        self, modpack: Modpack, mod_name: str, *, dry_run: bool = False
    ) -> LifecycleReport:
        """Restore one disabled package after dependency and conflict validation."""
        from .lifecycle import LifecycleManager

        return LifecycleManager(modpack).enable(mod_name, dry_run=dry_run)

    def install_modpack(
        self,
        modpack: Modpack,
        progress: InstallProgressCallback | None = None,
        *,
        dry_run: bool = False,
        phase: OperationPhaseCallback | None = None,
    ) -> InstallReport:
        """Install missing, changed, or damaged mods with v0.1-compatible behavior."""
        if modpack.game_adapter_id is not None:
            return self._install_with_adapter(
                modpack,
                progress=progress,
                updating=False,
                dry_run=dry_run,
                phase=phase,
            )
        if dry_run:
            raise InstallError("Dry-run is available for adapter-based modpacks")
        root = modpack.install_directory
        root.mkdir(parents=True, exist_ok=True)
        state_path = modpack.state_path or root / STATE_FILENAME
        state = load_state_file(state_path)
        records: dict[str, Any] = state["mods"]
        report = InstallReport(
            skipped=sum(1 for mod in modpack.mods if not mod.enabled)
        )
        self._notify_phase(phase, "Resolving packages…", True)
        plan = self._resolve_plan(modpack, report)
        if plan is None:
            return report
        self._collect_warnings(plan, report)
        changed: list[ResolvedPlanItem] = []
        for item in plan:
            existing = records.get(item.mod.name)
            if existing is not None and not verify_mod_record(
                root, item.mod, existing, resolved=item.resolved
            ):
                report.skipped += 1
            else:
                changed.append(item)
        if not changed:
            return report

        with tempfile.TemporaryDirectory(prefix=".modsync-install-", dir=root) as name:
            transaction = Path(name)
            self._notify_phase(phase, "Downloading and verifying packages…", True)
            prepared_mods = self._prepare_plan(transaction, changed, progress, report)
            if prepared_mods is None:
                return report
            self._notify_phase(phase, "Installing files…", False)
            try:
                displaced = transaction / "displaced"
                for prepared in prepared_mods:
                    self.apply_prepared_mod(root, prepared, displaced)
                    records[prepared.mod.name] = prepared.state_record
                state["modpack"] = {"name": modpack.name, "version": modpack.version}
                save_state_file(state_path, state)
                verification = verify_resolved_plan(modpack, plan)
                if not verification.ok:
                    first = verification.issues[0]
                    raise InstallError(
                        f"Post-install verification failed for {first.mod_name}: "
                        f"{first.message}"
                    )
            except (ModSyncError, OSError) as exc:
                report.failures.append(
                    InstallFailure(mod_name="install", message=self._error_message(exc))
                )
                return report
        report.installed = len(prepared_mods)
        return report

    def update_modpack(
        self,
        modpack: Modpack,
        progress: InstallProgressCallback | None = None,
        *,
        dry_run: bool = False,
        phase: OperationPhaseCallback | None = None,
    ) -> InstallReport:
        """Update all changed mods as one backup-protected transaction."""
        if modpack.game_adapter_id is not None:
            return self._install_with_adapter(
                modpack,
                progress=progress,
                updating=True,
                dry_run=dry_run,
                phase=phase,
            )
        if dry_run:
            raise InstallError("Dry-run is available for adapter-based modpacks")
        root = modpack.install_directory
        root.mkdir(parents=True, exist_ok=True)
        state_path = modpack.state_path or root / STATE_FILENAME
        state = load_state_file(state_path)
        records: dict[str, Any] = state["mods"]
        report = InstallReport(
            skipped=sum(1 for mod in modpack.mods if not mod.enabled)
        )
        self._notify_phase(phase, "Resolving packages…", True)
        plan = self._resolve_plan(modpack, report)
        if plan is None:
            return report
        self._collect_warnings(plan, report)
        changed: list[ResolvedPlanItem] = []

        for item in plan:
            existing = records.get(item.mod.name)
            if existing is not None and not verify_mod_record(
                root, item.mod, existing, resolved=item.resolved
            ):
                report.skipped += 1
            else:
                changed.append(item)
        if not changed:
            return report

        with tempfile.TemporaryDirectory(prefix=".modsync-update-", dir=root) as name:
            transaction = Path(name)
            self._notify_phase(phase, "Downloading and verifying packages…", True)
            prepared_mods = self._prepare_plan(transaction, changed, progress, report)
            if prepared_mods is None:
                return report

            self._notify_phase(phase, "Creating backup…", False)
            backup_manager = self.backup_manager_factory(modpack)
            try:
                backup = backup_manager.create(
                    [item.mod for item in changed], state, reason="update"
                )
                report.backup_id = backup.backup_id
            except (BackupError, OSError) as exc:
                report.failures.append(
                    InstallFailure(mod_name="update", message=self._error_message(exc))
                )
                return report

            current_mod = "update"
            try:
                self._notify_phase(phase, "Applying update…", False)
                displaced = transaction / "displaced"
                for prepared in prepared_mods:
                    current_mod = prepared.mod.name
                    self.apply_prepared_mod(root, prepared, displaced)
                    records[prepared.mod.name] = prepared.state_record
                state["modpack"] = {"name": modpack.name, "version": modpack.version}
                current_mod = "state"
                save_state_file(state_path, state)
                current_mod = "verification"
                verification = verify_resolved_plan(modpack, plan)
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
        self.source_registry.validate_staged(resolved, staged)

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
                "role": "explicit",
                "required_by": [],
                "directory": mod_directory_name(mod.name),
                "files": files,
            },
        )

    def _install_with_adapter(
        self,
        modpack: Modpack,
        *,
        progress: InstallProgressCallback | None,
        updating: bool,
        dry_run: bool,
        phase: OperationPhaseCallback | None,
    ) -> InstallReport:
        """Run the provider-neutral, adapter-planned installation pipeline."""
        report = InstallReport(skipped=sum(1 for mod in modpack.mods if not mod.enabled))
        try:
            adapter = self.game_registry.get(modpack.game_adapter_id or modpack.game)
            adapter.validate_game(modpack.install_directory)
        except (ModSyncError, OSError) as exc:
            report.failures.append(InstallFailure("game", self._error_message(exc)))
            return report

        self._notify_phase(phase, "Resolving packages…", True)
        resolved_plan = self._resolve_plan(modpack, report)
        if resolved_plan is None:
            return report
        report.resolved = len(resolved_plan)
        self._collect_warnings(resolved_plan, report)
        state_path = modpack.state_path or modpack.install_directory / STATE_FILENAME
        state = load_state_file(state_path)
        records: dict[str, Any] = state["mods"]
        changed: list[ResolvedPlanItem] = []
        for item in resolved_plan:
            existing = records.get(item.mod.name)
            if existing is not None and record_status(existing) == "disabled":
                report.skipped += 1
                continue
            if existing is not None and not verify_mod_record(
                modpack.install_directory, item.mod, existing, resolved=item.resolved
            ):
                report.skipped += 1
            else:
                changed.append(item)
        if not changed:
            return report

        temporary_parent = modpack.install_directory if not dry_run else None
        prefix = ".modsync-plan-" if dry_run else ".modsync-adapter-"
        with tempfile.TemporaryDirectory(prefix=prefix, dir=temporary_parent) as name:
            transaction = Path(name)
            self._notify_phase(phase, "Downloading and verifying packages…", True)
            prepared = self._prepare_plan(transaction, changed, progress, report)
            if prepared is None:
                return report
            try:
                plan = adapter.build_installation_plan(
                    modpack.install_directory,
                    ((item.mod.name, item.resolved, item.staged_directory) for item in prepared),
                )
                managed = self._managed_owners(records)
                validate_plan(plan, managed_owners=managed)
            except (ModSyncError, OSError) as exc:
                report.failures.append(InstallFailure("plan", self._error_message(exc)))
                return report
            report.planned_files = len(plan.entries)
            report.plan_entries = plan.entries
            if dry_run:
                return report

            self._notify_phase(
                phase,
                "Creating backup…" if updating else "Installing files…",
                False,
            )
            backup_manager = self.backup_manager_factory(modpack)
            if updating:
                try:
                    backup = backup_manager.create_paths(
                        plan.entries, state, reason="update"
                    )
                    report.backup_id = backup.backup_id
                except (BackupError, OSError) as exc:
                    report.failures.append(
                        InstallFailure("update", self._error_message(exc))
                    )
                    return report

            applied: list[tuple[Path, Path | None]] = []
            previous_state = state_path.read_bytes() if state_path.exists() else None
            try:
                self._notify_phase(phase, "Applying files…", False)
                applied = self._apply_adapter_plan(plan, transaction / "rollback")
                entries_by_mod: dict[str, list[Any]] = {}
                for entry in plan.entries:
                    entries_by_mod.setdefault(entry.mod_name, []).append(entry)
                for item, prepared_item in zip(changed, prepared, strict=True):
                    record = prepared_item.state_record
                    owned_entries = entries_by_mod.get(item.mod.name, [])
                    record.pop("directory", None)
                    record["adapter"] = adapter.game_id
                    record["owner"] = owned_entries[0].owner if owned_entries else mod_directory_name(item.mod.name)
                    record["package"] = record["owner"]
                    record["status"] = "enabled"
                    record["install_reason"] = (
                        "explicit" if item.explicit else "dependency"
                    )
                    record["dependencies"] = [
                        dependency.name for dependency in item.resolved.dependencies
                    ]
                    record["installed_files"] = [
                        {
                            "path": entry.destination.as_posix(),
                            "owner": entry.owner,
                            "sha256": sha256_file(entry.staged_file),
                        }
                        for entry in owned_entries
                    ]
                    record["files"] = {
                        entry.destination.as_posix(): sha256_file(entry.staged_file)
                        for entry in owned_entries
                    }
                    records[item.mod.name] = record
                state["modpack"] = {"name": modpack.name, "version": modpack.version}
                save_state_file(state_path, state)
                verification = verify_resolved_plan(modpack, resolved_plan)
                if not verification.ok:
                    first = verification.issues[0]
                    raise InstallError(
                        f"Post-install verification failed for {first.mod_name}: {first.message}"
                    )
            except Exception as exc:
                report.failures.append(InstallFailure("install", self._error_message(exc)))
                if updating and report.backup_id is not None:
                    report.rollback_attempted = True
                    try:
                        backup_manager.restore(report.backup_id)
                        report.rollback_succeeded = True
                    except Exception as rollback_exc:
                        report.rollback_succeeded = False
                        report.rollback_error = self._error_message(rollback_exc)
                elif applied:
                    try:
                        self._rollback_adapter_apply(applied)
                        if previous_state is None:
                            state_path.unlink(missing_ok=True)
                        else:
                            atomic_write_bytes(state_path, previous_state)
                    except OSError as rollback_exc:
                        report.warnings.append(
                            f"Could not fully roll back installation: {rollback_exc}"
                        )
                return report
            report.installed = len(prepared)
            if updating and report.backup_id is not None:
                try:
                    backup_manager.prune(DEFAULT_RETENTION)
                except BackupError as exc:
                    report.warnings.append(f"Could not apply backup retention: {exc}")
            return report

    @staticmethod
    def _notify_phase(
        callback: OperationPhaseCallback | None,
        message: str,
        cancellable: bool,
    ) -> None:
        if callback is not None:
            callback(message, cancellable)

    @staticmethod
    def _managed_owners(records: dict[str, Any]) -> dict[str, str]:
        owners: dict[str, str] = {}
        for record in records.values():
            if not isinstance(record, dict):
                continue
            files = record.get("installed_files")
            if not isinstance(files, list):
                continue
            for item in files:
                if (
                    isinstance(item, dict)
                    and isinstance(item.get("path"), str)
                    and isinstance(item.get("owner"), str)
                ):
                    owners[item["path"]] = item["owner"]
        return owners

    @staticmethod
    def _apply_adapter_plan(
        plan: InstallationPlan, rollback_root: Path
    ) -> list[tuple[Path, Path | None]]:
        """Apply validated files atomically one-by-one, restoring on any failure."""
        rollback_root.mkdir(parents=True, exist_ok=True)
        applied: list[tuple[Path, Path | None]] = []
        try:
            for index, entry in enumerate(plan.entries):
                destination = plan.game_root.joinpath(*entry.destination.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                previous: Path | None = None
                if destination.exists():
                    previous = rollback_root / str(index)
                    destination.replace(previous)
                applied.append((destination, previous))
                temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
                try:
                    shutil.copy2(entry.staged_file, temporary, follow_symlinks=False)
                    temporary.replace(destination)
                finally:
                    temporary.unlink(missing_ok=True)
        except Exception:
            Installer._rollback_adapter_apply(applied)
            raise
        return applied

    @staticmethod
    def _rollback_adapter_apply(applied: list[tuple[Path, Path | None]]) -> None:
        for destination, previous in reversed(applied):
            destination.unlink(missing_ok=True)
            if previous is not None and previous.exists():
                previous.replace(destination)

    def _resolve_plan(
        self, modpack: Modpack, report: InstallReport
    ) -> list[ResolvedPlanItem] | None:
        try:
            return self.source_registry.resolve_plan(modpack.mods)
        except (ModSyncError, OSError) as exc:
            report.failures.append(
                InstallFailure(mod_name="resolution", message=self._error_message(exc))
            )
            return None

    def _prepare_plan(
        self,
        transaction: Path,
        plan: list[ResolvedPlanItem],
        progress: InstallProgressCallback | None,
        report: InstallReport,
    ) -> list[PreparedMod] | None:
        prepared_mods: list[PreparedMod] = []
        for item in plan:
            try:
                prepared = self.prepare_mod(
                    transaction / mod_directory_name(item.mod.name),
                    item.mod,
                    item.resolved,
                    progress,
                )
                prepared.state_record["role"] = (
                    "explicit" if item.explicit else "dependency"
                )
                prepared.state_record["install_reason"] = prepared.state_record["role"]
                prepared.state_record["status"] = "enabled"
                prepared.state_record["package"] = mod_directory_name(item.mod.name)
                prepared.state_record["dependencies"] = [
                    dependency.name for dependency in item.resolved.dependencies
                ]
                prepared.state_record["required_by"] = list(item.required_by)
                prepared_mods.append(prepared)
            except (ModSyncError, OSError) as exc:
                report.failures.append(
                    InstallFailure(
                        mod_name=item.mod.name, message=self._error_message(exc)
                    )
                )
                return None
        return prepared_mods

    @staticmethod
    def _collect_warnings(
        plan: list[ResolvedPlanItem], report: InstallReport
    ) -> None:
        for item in plan:
            for warning in item.resolved.warnings:
                if warning not in report.warnings:
                    report.warnings.append(warning)

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
