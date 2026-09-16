"""Creation, validation, retention, and restoration of ModSync backups."""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import stat
import tempfile
from copy import deepcopy
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from .config import mod_directory_name
from .exceptions import (
    BackupError,
    BackupIntegrityError,
    BackupNotFoundError,
    RollbackError,
    StateError,
)
from .hashing import sha256_file
from .models import BackupInfo, Mod, Modpack, RestoreReport
from .state import (
    STATE_FILENAME,
    atomic_write_bytes,
    atomic_write_json,
    load_state_file,
    save_state_file,
    validate_state,
)

BACKUP_DIRECTORY = ".modsync-backups"
BACKUP_SCHEMA_VERSION = 1
DEFAULT_RETENTION = 5
_BACKUP_ID_RE = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{6}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _safe_relative_path(value: object, label: str, *, single: bool = False) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise BackupIntegrityError(f"Invalid {label} in backup metadata")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise BackupIntegrityError(f"Unsafe {label} in backup metadata: {value}")
    if any(":" in part for part in path.parts):
        raise BackupIntegrityError(f"Unsafe {label} in backup metadata: {value}")
    if single and len(path.parts) != 1:
        raise BackupIntegrityError(f"Invalid {label} in backup metadata: {value}")
    if path.parts[0] in {BACKUP_DIRECTORY, STATE_FILENAME}:
        raise BackupIntegrityError(f"Reserved {label} in backup metadata: {value}")
    return path


def _ensure_regular_file(path: Path, label: str, *, reject_hardlinks: bool = True) -> None:
    try:
        details = path.lstat()
    except OSError as exc:
        raise BackupIntegrityError(f"Missing or unreadable {label}: {path.name}") from exc
    if not stat.S_ISREG(details.st_mode) or path.is_symlink():
        raise BackupIntegrityError(f"{label} is not a regular file: {path.name}")
    if reject_hardlinks and details.st_nlink > 1:
        raise BackupIntegrityError(f"Hard links are not allowed in backups: {path.name}")


def _walk_regular_files(directory: Path, label: str) -> Iterator[Path]:
    """Yield regular files without following links anywhere in the tree."""
    for current, directory_names, filenames in os.walk(directory, followlinks=False):
        current_path = Path(current)
        for name in directory_names:
            child = current_path / name
            if child.is_symlink():
                raise BackupIntegrityError(f"Symbolic links are not allowed in {label}: {child}")
        for name in filenames:
            child = current_path / name
            _ensure_regular_file(child, label, reject_hardlinks=label == "backup files")
            yield child


class BackupManager:
    """Manage verified backups for one modpack installation directory."""

    def __init__(self, modpack: Modpack) -> None:
        self.modpack = modpack
        self.root = modpack.install_directory
        self.state_path = modpack.state_path or self.root / STATE_FILENAME
        self.backup_root = modpack.backup_directory or self.root / BACKUP_DIRECTORY

    def create(
        self,
        affected_mods: Sequence[Mod],
        state: dict[str, Any],
        *,
        reason: str = "update",
    ) -> BackupInfo:
        """Create a durable backup of affected mod directories and state."""
        if not affected_mods:
            raise BackupError("Cannot create a backup without affected mods")
        if not reason.strip():
            raise BackupError("Backup reason must not be empty")
        try:
            validate_state(state)
            self._prepare_backup_root()
            created = datetime.now(UTC)
            backup_id = self._new_backup_id(created)
            temporary = Path(tempfile.mkdtemp(prefix=".creating-", dir=self.backup_root))
        except (OSError, StateError) as exc:
            raise BackupError(f"Could not prepare backup storage: {exc}") from exc

        try:
            files_root = temporary / "files"
            files_root.mkdir()
            saved_files: list[dict[str, str]] = []
            affected_entries: list[dict[str, object]] = []
            records: dict[str, Any] = state["mods"]

            for mod in affected_mods:
                directory = mod_directory_name(mod.name)
                source = self.root / directory
                existed = source.exists() or source.is_symlink()
                if source.is_symlink():
                    raise BackupError(f"Refusing to back up symbolic link: {directory}")
                if existed and not source.is_dir():
                    raise BackupError(f"Installed mod path is not a directory: {directory}")

                record = records.get(mod.name)
                installed_version = record.get("version") if isinstance(record, dict) else None
                affected_entries.append(
                    {
                        "name": mod.name,
                        "version": (
                            installed_version if isinstance(installed_version, str) else None
                        ),
                        "directory": directory,
                        "existed": existed,
                    }
                )

                if not existed:
                    continue
                destination_root = files_root / directory
                destination_root.mkdir()
                for source_file in _walk_regular_files(source, "installed mod"):
                    relative_in_mod = source_file.relative_to(source)
                    destination = destination_root / relative_in_mod
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source_file, destination, follow_symlinks=False)
                    _ensure_regular_file(destination, "new backup file")
                    relative_to_install = (Path(directory) / relative_in_mod).as_posix()
                    saved_files.append(
                        {"path": relative_to_install, "sha256": sha256_file(destination)}
                    )

            state_path = temporary / "state.json"
            atomic_write_json(state_path, state)
            saved_files.sort(key=lambda entry: entry["path"])
            installed_pack = state.get("modpack", {})
            installed_version = installed_pack.get("version")
            if not isinstance(installed_version, str) or not installed_version:
                installed_version = self.modpack.version
            metadata = {
                "schema_version": BACKUP_SCHEMA_VERSION,
                "backup_id": backup_id,
                "created_at": created.isoformat().replace("+00:00", "Z"),
                "modpack": {"name": self.modpack.name, "version": installed_version},
                "target_modpack_version": self.modpack.version,
                "reason": reason,
                "file_count": len(saved_files),
                "state_sha256": sha256_file(state_path),
                "affected_mods": affected_entries,
                "saved_files": saved_files,
            }
            atomic_write_json(temporary / "metadata.json", metadata)
            final_directory = self.backup_root / backup_id
            temporary.replace(final_directory)
            return self._info_from_metadata(metadata)
        except BackupError:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        except (OSError, BackupIntegrityError) as exc:
            shutil.rmtree(temporary, ignore_errors=True)
            raise BackupError(f"Could not create backup {backup_id}: {exc}") from exc

    def list_backups(self) -> list[BackupInfo]:
        """Return validated backup summaries, newest first."""
        if not self.backup_root.exists():
            return []
        if self.backup_root.is_symlink() or not self.backup_root.is_dir():
            raise BackupIntegrityError("Backup storage is not a safe directory")

        backups: list[BackupInfo] = []
        try:
            children = list(self.backup_root.iterdir())
        except OSError as exc:
            raise BackupError(f"Could not list backups: {exc}") from exc
        for child in children:
            if not _BACKUP_ID_RE.fullmatch(child.name):
                continue
            metadata, _ = self._load_and_validate(child.name, verify_files=False)
            backups.append(self._info_from_metadata(metadata))
        return sorted(backups, key=lambda item: (item.created_at, item.backup_id), reverse=True)

    def restore(self, backup_id: str) -> RestoreReport:
        """Fully validate and safely restore a backup with best-effort atomicity."""
        metadata, state_snapshot = self._load_and_validate(backup_id, verify_files=True)
        backup_directory = self.backup_root / backup_id
        affected = metadata["affected_mods"]
        saved_files = metadata["saved_files"]
        self.root.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory(prefix=".modsync-restore-", dir=self.root) as name:
            transaction = Path(name)
            staged = transaction / "staged"
            current = transaction / "current"
            staged.mkdir()
            current.mkdir()

            try:
                for entry in affected:
                    if entry["existed"]:
                        (staged / entry["directory"]).mkdir()
                for file_entry in saved_files:
                    relative = _safe_relative_path(file_entry["path"], "saved file path")
                    source = backup_directory / "files" / Path(*relative.parts)
                    destination = staged / Path(*relative.parts)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination, follow_symlinks=False)
                    _ensure_regular_file(destination, "staged restore file")
                    if sha256_file(destination) != file_entry["sha256"]:
                        raise BackupIntegrityError(
                            f"Staged restore file failed verification: {relative.as_posix()}"
                        )
            except BackupError:
                raise
            except OSError as exc:
                raise BackupError(f"Could not prepare backup restore: {exc}") from exc

            state_path = self.state_path
            previous_state = self._read_current_state_bytes(state_path)
            restored_state = self._restored_state(
                metadata, state_snapshot, load_state_file(state_path)
            )
            try:
                self._apply_restore(staged, current, affected, restored_state)
                self._verify_restored(metadata, restored_state)
            except Exception as original:
                try:
                    self._restore_interrupted_restore(current, affected, state_path, previous_state)
                except Exception as recovery:
                    raise RollbackError(
                        f"Restore of {backup_id} failed: {original}. "
                        f"Restoring the pre-restore installation also failed: {recovery}"
                    ) from recovery
                if isinstance(original, BackupError):
                    raise
                raise RollbackError(
                    f"Restore of {backup_id} failed; the pre-restore installation was preserved: "
                    f"{original}"
                ) from original

        return RestoreReport(
            backup_id=backup_id,
            restored_files=metadata["file_count"],
            state_restored=True,
        )

    def prune(self, retention: int = DEFAULT_RETENTION) -> list[str]:
        """Delete backups older than the retention count after a successful update."""
        if retention < 1:
            raise BackupError("Backup retention must be at least 1")
        backups = self.list_backups()
        removed: list[str] = []
        for backup in backups[retention:]:
            directory = self.backup_root / backup.backup_id
            if directory.is_symlink() or directory.parent != self.backup_root:
                raise BackupIntegrityError(f"Unsafe backup directory: {backup.backup_id}")
            try:
                shutil.rmtree(directory)
            except OSError as exc:
                raise BackupError(f"Could not remove old backup {backup.backup_id}: {exc}") from exc
            removed.append(backup.backup_id)
        return removed

    def _prepare_backup_root(self) -> None:
        self.backup_root.parent.mkdir(parents=True, exist_ok=True)
        if self.backup_root.is_symlink():
            raise BackupError("Backup storage must not be a symbolic link")
        self.backup_root.mkdir(exist_ok=True)
        if not self.backup_root.is_dir():
            raise BackupError("Backup storage is not a directory")

    def _new_backup_id(self, created: datetime) -> str:
        prefix = created.strftime("%Y%m%dT%H%M%SZ")
        for _ in range(100):
            candidate = f"{prefix}-{secrets.token_hex(3)}"
            if not (self.backup_root / candidate).exists():
                return candidate
        raise BackupError("Could not allocate a unique backup ID")

    def _backup_directory(self, backup_id: str) -> Path:
        if not isinstance(backup_id, str) or not _BACKUP_ID_RE.fullmatch(backup_id):
            raise BackupNotFoundError(f"Invalid backup ID: {backup_id}")
        directory = self.backup_root / backup_id
        if not directory.exists():
            raise BackupNotFoundError(f"Backup not found: {backup_id}")
        if directory.is_symlink() or not directory.is_dir():
            raise BackupIntegrityError(f"Backup is not a safe directory: {backup_id}")
        return directory

    def _load_and_validate(
        self, backup_id: str, *, verify_files: bool
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        directory = self._backup_directory(backup_id)
        metadata_path = directory / "metadata.json"
        _ensure_regular_file(metadata_path, "backup metadata")
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BackupIntegrityError(f"Invalid metadata for backup {backup_id}: {exc}") from exc
        self._validate_metadata(metadata, backup_id)

        if not verify_files:
            return metadata, {}

        state_path = directory / "state.json"
        _ensure_regular_file(state_path, "backup state")
        if sha256_file(state_path) != metadata["state_sha256"]:
            raise BackupIntegrityError(f"State checksum mismatch in backup {backup_id}")
        try:
            state_value = json.loads(state_path.read_text(encoding="utf-8"))
            state_snapshot = validate_state(state_value, f"state in backup {backup_id}")
        except (OSError, json.JSONDecodeError, StateError) as exc:
            raise BackupIntegrityError(f"Invalid state in backup {backup_id}: {exc}") from exc

        files_root = directory / "files"
        if files_root.is_symlink() or not files_root.is_dir():
            raise BackupIntegrityError(f"Backup files directory is invalid: {backup_id}")
        expected: set[str] = set()
        for entry in metadata["saved_files"]:
            relative = _safe_relative_path(entry["path"], "saved file path")
            expected.add(relative.as_posix())
            candidate = files_root / Path(*relative.parts)
            _ensure_regular_file(candidate, "backup file")
            if sha256_file(candidate) != entry["sha256"]:
                raise BackupIntegrityError(
                    f"Checksum mismatch in backup {backup_id}: {relative.as_posix()}"
                )

        actual = {
            item.relative_to(files_root).as_posix()
            for item in _walk_regular_files(files_root, "backup files")
        }
        if actual != expected:
            unexpected = sorted(actual - expected)
            missing = sorted(expected - actual)
            details = unexpected[0] if unexpected else missing[0]
            raise BackupIntegrityError(f"Backup file manifest mismatch: {details}")
        return metadata, state_snapshot

    def _validate_metadata(self, metadata: object, backup_id: str) -> None:
        if not isinstance(metadata, dict) or metadata.get("schema_version") != 1:
            raise BackupIntegrityError(f"Unsupported metadata for backup {backup_id}")
        if metadata.get("backup_id") != backup_id:
            raise BackupIntegrityError(f"Backup ID mismatch in metadata: {backup_id}")
        created_at = metadata.get("created_at")
        if not isinstance(created_at, str) or not created_at.endswith("Z"):
            raise BackupIntegrityError(f"Missing creation time in backup {backup_id}")
        try:
            parsed_time = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise BackupIntegrityError(f"Invalid creation time in backup {backup_id}") from exc
        if parsed_time.tzinfo is None:
            raise BackupIntegrityError(f"Backup creation time must include UTC: {backup_id}")
        if parsed_time.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ") != backup_id[:16]:
            raise BackupIntegrityError(f"Backup creation time does not match its ID: {backup_id}")

        modpack = metadata.get("modpack")
        if (
            not isinstance(modpack, dict)
            or not isinstance(modpack.get("name"), str)
            or not isinstance(modpack.get("version"), str)
        ):
            raise BackupIntegrityError(f"Invalid modpack metadata in backup {backup_id}")
        if modpack["name"] != self.modpack.name:
            raise BackupIntegrityError(
                f"Backup {backup_id} belongs to a different modpack: {modpack['name']}"
            )
        if not isinstance(metadata.get("reason"), str) or not metadata["reason"]:
            raise BackupIntegrityError(f"Invalid reason in backup {backup_id}")
        if not isinstance(metadata.get("target_modpack_version"), str):
            raise BackupIntegrityError(f"Invalid target modpack version in backup {backup_id}")
        if not isinstance(metadata.get("state_sha256"), str) or not _SHA256_RE.fullmatch(
            metadata["state_sha256"]
        ):
            raise BackupIntegrityError(f"Invalid state checksum in backup {backup_id}")

        affected = metadata.get("affected_mods")
        if not isinstance(affected, list) or not affected:
            raise BackupIntegrityError(f"Backup {backup_id} has no affected mods")
        directories: set[str] = set()
        existing_directories: set[str] = set()
        for entry in affected:
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("name"), str)
                or not isinstance(entry.get("existed"), bool)
                or (entry.get("version") is not None and not isinstance(entry.get("version"), str))
            ):
                raise BackupIntegrityError(f"Invalid affected mod in backup {backup_id}")
            directory = _safe_relative_path(entry.get("directory"), "mod directory", single=True)
            if directory.as_posix() in directories:
                raise BackupIntegrityError(f"Duplicate mod directory in backup {backup_id}")
            directories.add(directory.as_posix())
            if entry["existed"]:
                existing_directories.add(directory.as_posix())

        saved_files = metadata.get("saved_files")
        if not isinstance(saved_files, list):
            raise BackupIntegrityError(f"Invalid file list in backup {backup_id}")
        paths: set[str] = set()
        for entry in saved_files:
            if not isinstance(entry, dict):
                raise BackupIntegrityError(f"Invalid file entry in backup {backup_id}")
            relative = _safe_relative_path(entry.get("path"), "saved file path")
            checksum = entry.get("sha256")
            if not isinstance(checksum, str) or not _SHA256_RE.fullmatch(checksum):
                raise BackupIntegrityError(f"Invalid file checksum in backup {backup_id}")
            if relative.as_posix() in paths or relative.parts[0] not in existing_directories:
                raise BackupIntegrityError(f"Invalid saved file path in backup {backup_id}")
            paths.add(relative.as_posix())
        if metadata.get("file_count") != len(saved_files):
            raise BackupIntegrityError(f"File count mismatch in backup {backup_id}")

    def _info_from_metadata(self, metadata: dict[str, Any]) -> BackupInfo:
        return BackupInfo(
            backup_id=metadata["backup_id"],
            created_at=metadata["created_at"],
            modpack_name=metadata["modpack"]["name"],
            modpack_version=metadata["modpack"]["version"],
            reason=metadata["reason"],
            file_count=metadata["file_count"],
        )

    def _read_current_state_bytes(self, state_path: Path) -> bytes | None:
        if state_path.is_symlink():
            raise RollbackError("Current state file must not be a symbolic link")
        if not state_path.exists():
            return None
        _ensure_regular_file(state_path, "current state", reject_hardlinks=False)
        try:
            return state_path.read_bytes()
        except OSError as exc:
            raise RollbackError(f"Could not preserve current state: {exc}") from exc

    def _restored_state(
        self,
        metadata: dict[str, Any],
        snapshot: dict[str, Any],
        current: dict[str, Any],
    ) -> dict[str, Any]:
        """Restore affected records while preserving unrelated later updates."""
        restored = deepcopy(current)
        snapshot_records: dict[str, Any] = snapshot["mods"]
        restored_records: dict[str, Any] = restored["mods"]
        for entry in metadata["affected_mods"]:
            name = entry["name"]
            if entry["existed"] and name in snapshot_records:
                restored_records[name] = deepcopy(snapshot_records[name])
            else:
                restored_records.pop(name, None)

        current_version = current.get("modpack", {}).get("version")
        snapshot_version = snapshot.get("modpack", {}).get("version")
        if current_version in {snapshot_version, metadata["target_modpack_version"]}:
            restored["modpack"] = deepcopy(snapshot["modpack"])
        return restored

    def _apply_restore(
        self,
        staged: Path,
        current: Path,
        affected: list[dict[str, Any]],
        state_snapshot: dict[str, Any],
    ) -> None:
        for entry in affected:
            directory = entry["directory"]
            target = self.root / directory
            if target.is_symlink():
                raise RollbackError(f"Refusing to replace symbolic link: {directory}")
            if target.exists():
                if not target.is_dir():
                    raise RollbackError(f"Installed mod path is not a directory: {directory}")
                target.replace(current / directory)
            if entry["existed"]:
                (staged / directory).replace(target)
        save_state_file(self.state_path, state_snapshot)

    def _restore_interrupted_restore(
        self,
        current: Path,
        affected: list[dict[str, Any]],
        state_path: Path,
        previous_state: bytes | None,
    ) -> None:
        for entry in affected:
            directory = entry["directory"]
            target = self.root / directory
            if target.is_symlink():
                target.unlink()
            elif target.exists():
                shutil.rmtree(target)
            preserved = current / directory
            if preserved.exists():
                preserved.replace(target)
        if previous_state is None:
            state_path.unlink(missing_ok=True)
        else:
            atomic_write_bytes(state_path, previous_state)

    def _verify_restored(
        self, metadata: dict[str, Any], state_snapshot: dict[str, Any]
    ) -> None:
        expected_by_directory: dict[str, dict[str, str]] = {}
        for entry in metadata["saved_files"]:
            relative = _safe_relative_path(entry["path"], "saved file path")
            in_mod = PurePosixPath(*relative.parts[1:]).as_posix()
            expected_by_directory.setdefault(relative.parts[0], {})[in_mod] = entry["sha256"]

        for affected in metadata["affected_mods"]:
            directory = affected["directory"]
            target = self.root / directory
            if not affected["existed"]:
                if target.exists() or target.is_symlink():
                    raise RollbackError(f"Unexpected restored path: {directory}")
                continue
            if target.is_symlink() or not target.is_dir():
                raise RollbackError(f"Restored mod directory is invalid: {directory}")
            expected = expected_by_directory.get(directory, {})
            actual: dict[str, str] = {}
            for item in _walk_regular_files(target, "restored mod"):
                relative = item.relative_to(target).as_posix()
                actual[relative] = sha256_file(item)
            if actual != expected:
                raise RollbackError(f"Restored files failed verification: {directory}")

        if load_state_file(self.state_path) != state_snapshot:
            raise RollbackError("Restored state failed verification")
