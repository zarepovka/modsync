"""Ownership-driven uninstall and enable/disable operations."""

from __future__ import annotations

import os
import re
import shutil
import stat
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from .backup import BackupManager
from .config import mod_directory_name
from .exceptions import (
    BackupError,
    DependencySafetyError,
    InstallationConflictError,
    LifecycleError,
)
from .games.base import collision_key, validate_relative_destination
from .hashing import sha256_file
from .models import (
    DisablePlan,
    EnablePlan,
    LifecycleReport,
    ManagedFileAction,
    Modpack,
    RemovalPlan,
)
from .state import (
    STATE_FILENAME,
    disabled_storage_path,
    load_state_file,
    record_install_reason,
    record_status,
    save_state_file,
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _is_config(path: PurePosixPath) -> bool:
    return len(path.parts) >= 2 and tuple(part.casefold() for part in path.parts[:2]) == (
        "bepinex",
        "config",
    )


class LifecycleManager:
    """Build, validate, back up, apply, and verify package lifecycle plans."""

    def __init__(self, modpack: Modpack, *, case_sensitive: bool | None = None) -> None:
        if modpack.game_adapter_id is None:
            raise LifecycleError(
                "Uninstall and enable/disable require an adapter-based modpack"
            )
        self.modpack = modpack
        self.game_root = modpack.install_directory
        self.state_path = modpack.state_path or self.game_root / STATE_FILENAME
        self.disabled_root = disabled_storage_path(modpack)
        self.backups = BackupManager(modpack)
        self.case_sensitive = os.name != "nt" if case_sensitive is None else case_sensitive

    def uninstall(
        self, mod_name: str, *, dry_run: bool = False, force: bool = False
    ) -> LifecycleReport:
        state = load_state_file(self.state_path)
        actual_name, record = self._record(state, mod_name)
        plan = self.build_removal_plan(actual_name, record, state, force=force)
        report = LifecycleReport(
            "uninstall",
            actual_name,
            dry_run=dry_run,
            paths=tuple(item.path.as_posix() for item in plan.files),
            preserved_files=tuple(path.as_posix() for path in plan.preserved_files),
            orphan_dependencies=plan.orphan_dependencies,
        )
        if dry_run:
            return report
        affected = [self._backup_path(item.source_storage, item) for item in plan.files]
        affected.extend(("game", path) for path in plan.preserved_files)
        backup = self.backups.create_mutation(
            affected, [actual_name], state, reason="uninstall"
        )
        report.backup_id = backup.backup_id
        try:
            self.apply_removal(plan)
            state["mods"].pop(actual_name)
            save_state_file(self.state_path, state)
            self._verify_absent(plan)
            self._cleanup_directories(plan.cleanup_directories)
            self._cleanup_disabled_directories(plan.files)
            report.changed = True
            return report
        except Exception as exc:
            self._rollback_or_raise(backup.backup_id, exc)

    def disable(
        self, mod_name: str, *, dry_run: bool = False, force: bool = False
    ) -> LifecycleReport:
        state = load_state_file(self.state_path)
        actual_name, record = self._record(state, mod_name)
        plan = self.build_disable_plan(actual_name, record, state, force=force)
        report = LifecycleReport(
            "disable",
            actual_name,
            dry_run=dry_run,
            paths=tuple(item.path.as_posix() for item in plan.files),
        )
        if dry_run:
            return report
        affected = []
        for item in plan.files:
            affected.append(self._backup_path("game", item))
            affected.append(self._backup_path("disabled", item))
        backup = self.backups.create_mutation(
            affected, [actual_name], state, reason="disable"
        )
        report.backup_id = backup.backup_id
        try:
            self.apply_disable(plan)
            self._set_storage(record, plan.files, "disabled")
            record["status"] = "disabled"
            record["install_reason"] = record_install_reason(record)
            save_state_file(self.state_path, state)
            self._verify_transition(plan.files, "disabled")
            self._cleanup_directories(self._cleanup_candidates(plan.files))
            report.changed = True
            return report
        except Exception as exc:
            self._rollback_or_raise(backup.backup_id, exc)

    def enable(self, mod_name: str, *, dry_run: bool = False) -> LifecycleReport:
        state = load_state_file(self.state_path)
        actual_name, record = self._record(state, mod_name)
        plan = self.build_enable_plan(actual_name, record, state)
        report = LifecycleReport(
            "enable",
            actual_name,
            dry_run=dry_run,
            paths=tuple(item.path.as_posix() for item in plan.files),
        )
        if dry_run:
            return report
        affected = []
        for item in plan.files:
            affected.append(self._backup_path("disabled", item))
            affected.append(self._backup_path("game", item))
        backup = self.backups.create_mutation(
            affected, [actual_name], state, reason="enable"
        )
        report.backup_id = backup.backup_id
        try:
            self.apply_enable(plan)
            self._set_storage(record, plan.files, "game")
            record["status"] = "enabled"
            record["install_reason"] = record_install_reason(record)
            save_state_file(self.state_path, state)
            self._verify_transition(plan.files, "game")
            self._cleanup_disabled_directories(plan.files)
            report.changed = True
            return report
        except Exception as exc:
            self._rollback_or_raise(backup.backup_id, exc)

    def build_removal_plan(
        self,
        mod_name: str,
        record: dict[str, Any],
        state: dict[str, Any],
        *,
        force: bool = False,
    ) -> RemovalPlan:
        dependents = self._dependents(state, mod_name, enabled_only=False)
        if dependents:
            raise DependencySafetyError(
                f"Cannot uninstall {mod_name}.\n\nRequired by:\n"
                + "\n".join(f"- {name}" for name in dependents)
            )
        actions = self._managed_actions(mod_name, record, state)
        remove: list[ManagedFileAction] = []
        preserved: list[PurePosixPath] = []
        for action in actions:
            candidate = self._source_path(action)
            if not candidate.exists():
                raise LifecycleError(
                    f"Managed file is missing; run verify/repair first: {action.path}"
                )
            actual_digest = self._validate_file(candidate, action.source_storage, action)
            if actual_digest != action.sha256:
                if _is_config(action.path):
                    preserved.append(action.path)
                    continue
                if not force:
                    raise LifecycleError(
                        "Modified managed file detected.\n\n"
                        f"{action.path.as_posix()}\n\nUse --force to remove it after backup."
                    )
            remove.append(action)
        orphans = self._orphan_dependencies(record, state, excluding=mod_name)
        directories = self._cleanup_candidates(remove)
        package = self._package(mod_name, record)
        return RemovalPlan(
            mod_name,
            package,
            tuple(remove),
            tuple(preserved),
            tuple(orphans),
            tuple(directories),
        )

    def build_disable_plan(
        self,
        mod_name: str,
        record: dict[str, Any],
        state: dict[str, Any],
        *,
        force: bool = False,
    ) -> DisablePlan:
        if record_status(record) == "disabled":
            raise LifecycleError(f"{mod_name} is already disabled")
        dependents = self._dependents(state, mod_name, enabled_only=True)
        if dependents:
            raise DependencySafetyError(
                f"Cannot disable {mod_name}.\n\nRequired by:\n"
                + "\n".join(f"- {name}" for name in dependents)
            )
        runtime = [
            action
            for action in self._managed_actions(mod_name, record, state)
            if not _is_config(action.path)
        ]
        if not runtime:
            raise LifecycleError(f"{mod_name} has no runtime files to disable")
        seen: set[str] = set()
        for index, action in enumerate(runtime):
            key = collision_key(action.path, case_sensitive=self.case_sensitive)
            if key in seen:
                raise InstallationConflictError(
                    f"Case-colliding disabled destination: {action.path}"
                )
            seen.add(key)
            source = self._source_path(action)
            digest = self._validate_file(source, "game", action)
            if digest != action.sha256 and not force:
                raise LifecycleError(
                    "Modified managed file detected.\n\n"
                    f"{action.path.as_posix()}\n\nUse --force to disable it after backup."
                )
            if digest != action.sha256:
                action = ManagedFileAction(
                    action.path,
                    action.owner,
                    digest,
                    action.source_storage,
                    action.destination_storage,
                )
                runtime[index] = action
            destination = self._storage_path("disabled", action)
            self._validate_destination_available(destination, "disabled", action)
        return DisablePlan(
            mod_name, self._package(mod_name, record), tuple(runtime), tuple(dependents)
        )

    def build_enable_plan(
        self, mod_name: str, record: dict[str, Any], state: dict[str, Any]
    ) -> EnablePlan:
        if record_status(record) == "enabled":
            raise LifecycleError(f"{mod_name} is already enabled")
        unavailable: list[str] = []
        for dependency in self._dependencies(record):
            match = self._optional_record(state, dependency)
            if match is None or record_status(match[1]) != "enabled":
                unavailable.append(dependency)
        if unavailable:
            raise DependencySafetyError(
                f"Cannot enable {mod_name}.\n\nRequired dependency is unavailable:\n"
                + "\n".join(f"- {name}" for name in unavailable)
            )
        runtime = [
            action
            for action in self._managed_actions(mod_name, record, state)
            if action.source_storage == "disabled"
        ]
        if not runtime:
            raise LifecycleError(f"{mod_name} has no disabled runtime files")
        seen: set[str] = set()
        for action in runtime:
            key = collision_key(action.path, case_sensitive=self.case_sensitive)
            if key in seen:
                raise InstallationConflictError(
                    f"Case-colliding enable destination: {action.path}"
                )
            seen.add(key)
            source = self._source_path(action)
            digest = self._validate_file(source, "disabled", action)
            if digest != action.sha256:
                raise LifecycleError(
                    f"Disabled SHA256 mismatch: {action.path.as_posix()}"
                )
            destination = self._storage_path("game", action)
            self._validate_destination_available(destination, "game", action)
        return EnablePlan(
            mod_name,
            self._package(mod_name, record),
            tuple(runtime),
            tuple(self._dependencies(record)),
        )

    def apply_removal(self, plan: RemovalPlan) -> None:
        for action in plan.files:
            self._source_path(action).unlink()

    def apply_disable(self, plan: DisablePlan) -> None:
        for action in plan.files:
            self._move(self._source_path(action), self._storage_path("disabled", action))

    def apply_enable(self, plan: EnablePlan) -> None:
        for action in plan.files:
            self._move(self._source_path(action), self._storage_path("game", action))

    def _managed_actions(
        self, mod_name: str, record: dict[str, Any], state: dict[str, Any]
    ) -> list[ManagedFileAction]:
        files = record.get("installed_files")
        if not isinstance(files, list) or not files:
            raise LifecycleError(
                f"{mod_name} has no v0.6 ownership data; run install/repair first"
            )
        expected_owner = record.get("owner", record.get("package"))
        if not isinstance(expected_owner, str) or mod_directory_name(expected_owner) != expected_owner:
            raise LifecycleError(f"Invalid package owner in state for {mod_name}")
        actions: list[ManagedFileAction] = []
        seen: set[tuple[str, str]] = set()
        for entry in files:
            if not isinstance(entry, dict):
                raise LifecycleError(f"Invalid managed file state for {mod_name}")
            raw_path = entry.get("path")
            owner = entry.get("owner")
            digest = entry.get("sha256")
            storage = entry.get("storage", "game")
            if owner != expected_owner or storage not in {"game", "disabled"}:
                raise LifecycleError(f"Ownership mismatch in state for {mod_name}")
            if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
                raise LifecycleError(f"Invalid managed SHA256 in state for {mod_name}")
            try:
                path = validate_relative_destination(raw_path)
            except Exception as exc:
                raise LifecycleError(f"Unsafe managed path in state for {mod_name}") from exc
            key = (storage, collision_key(path, case_sensitive=self.case_sensitive))
            if key in seen:
                raise InstallationConflictError(
                    f"Duplicate or case-colliding managed path: {path.as_posix()}"
                )
            seen.add(key)
            actions.append(ManagedFileAction(path, owner, digest, storage))
        self._reject_shared_files(mod_name, actions, state)
        return actions

    def _reject_shared_files(
        self,
        mod_name: str,
        actions: Iterable[ManagedFileAction],
        state: dict[str, Any],
    ) -> None:
        keys = {
            (
                action.source_storage,
                collision_key(action.path, case_sensitive=self.case_sensitive),
            )
            for action in actions
        }
        for other_name, other in state["mods"].items():
            if other_name == mod_name or not isinstance(other, dict):
                continue
            other_files = other.get("installed_files")
            if not isinstance(other_files, list):
                continue
            for entry in other_files:
                if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                    continue
                try:
                    key = (
                        entry.get("storage", "game"),
                        collision_key(
                            entry["path"], case_sensitive=self.case_sensitive
                        ),
                    )
                except Exception:
                    continue
                if key in keys:
                    raise InstallationConflictError(
                        "Shared managed file has multiple owners; run verify/repair:\n"
                        f"{entry['path']}"
                    )

    def _record(
        self, state: dict[str, Any], requested: str
    ) -> tuple[str, dict[str, Any]]:
        match = self._optional_record(state, requested)
        if match is None:
            raise LifecycleError(f"Installed mod not found: {requested}")
        return match

    @staticmethod
    def _optional_record(
        state: dict[str, Any], requested: str
    ) -> tuple[str, dict[str, Any]] | None:
        folded = requested.casefold()
        matches: list[tuple[str, dict[str, Any]]] = []
        for name, record in state["mods"].items():
            if not isinstance(name, str) or not isinstance(record, dict):
                continue
            aliases = {name.casefold()}
            source = record.get("source")
            identity = source.get("identity") if isinstance(source, dict) else None
            if isinstance(identity, dict):
                namespace = identity.get("namespace")
                package = identity.get("package")
                if isinstance(namespace, str) and isinstance(package, str):
                    aliases.add(f"{namespace}-{package}".casefold())
            if folded in aliases:
                matches.append((name, record))
        if len(matches) > 1:
            raise LifecycleError(f"Ambiguous installed mod name: {requested}")
        return matches[0] if matches else None

    @staticmethod
    def _dependencies(record: dict[str, Any]) -> list[str]:
        values = record.get("dependencies")
        if values is None:
            source = record.get("source")
            release = source.get("release") if isinstance(source, dict) else None
            raw_values = release.get("dependencies", []) if isinstance(release, dict) else []
            if not isinstance(raw_values, list) or any(
                not isinstance(value, str) for value in raw_values
            ):
                raise LifecycleError("Invalid legacy dependency metadata in state")
            values = []
            for value in raw_values:
                parts = value.rsplit("-", 2)
                if len(parts) != 3 or not all(parts):
                    raise LifecycleError("Invalid legacy dependency metadata in state")
                values.append(f"{parts[0]}-{parts[1]}")
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise LifecycleError("Invalid dependency metadata in state")
        return values

    def _dependents(
        self, state: dict[str, Any], target: str, *, enabled_only: bool
    ) -> list[str]:
        result = []
        for name, record in state["mods"].items():
            if not isinstance(name, str) or not isinstance(record, dict):
                continue
            if enabled_only and record_status(record) != "enabled":
                continue
            for dependency in self._dependencies(record):
                match = self._optional_record(state, dependency)
                if match is not None and match[0].casefold() == target.casefold():
                    result.append(name)
                    break
        return sorted(result, key=str.casefold)

    def _orphan_dependencies(
        self, record: dict[str, Any], state: dict[str, Any], *, excluding: str
    ) -> list[str]:
        result: list[str] = []
        for dependency in self._dependencies(record):
            match = self._optional_record(state, dependency)
            if match is None or record_install_reason(match[1]) != "dependency":
                continue
            remaining = {
                name: value
                for name, value in state["mods"].items()
                if name != excluding
            }
            temporary = {**state, "mods": remaining}
            if not self._dependents(temporary, match[0], enabled_only=False):
                version = match[1].get("version")
                result.append(f"{match[0]} {version}" if isinstance(version, str) else match[0])
        return sorted(result, key=str.casefold)

    @staticmethod
    def _package(mod_name: str, record: dict[str, Any]) -> str:
        value = record.get("package", record.get("owner", mod_name))
        return value if isinstance(value, str) else mod_name

    def _storage_path(self, storage: str, action: ManagedFileAction) -> Path:
        if storage == "game":
            root = self.game_root
            parts = action.path.parts
        elif storage == "disabled":
            root = self.disabled_root
            parts = (action.owner, *action.path.parts)
        else:
            raise LifecycleError(f"Unsupported storage root: {storage}")
        if root.is_symlink():
            raise LifecycleError(f"Refusing symbolic-link {storage} storage root")
        candidate = root.joinpath(*parts)
        current = root
        for part in parts[:-1]:
            current = current / part
            if current.is_symlink():
                raise LifecycleError(f"Refusing symbolic-link path component: {current}")
        try:
            candidate.resolve(strict=False).relative_to(root.resolve(strict=False))
        except ValueError as exc:
            raise LifecycleError(f"Managed path escapes {storage} storage") from exc
        return candidate

    def _source_path(self, action: ManagedFileAction) -> Path:
        return self._storage_path(action.source_storage, action)

    def _validate_file(
        self, path: Path, storage: str, action: ManagedFileAction
    ) -> str:
        expected = self._storage_path(storage, action)
        if expected != path:
            raise LifecycleError("Managed storage mismatch")
        try:
            details = path.lstat()
        except OSError as exc:
            raise LifecycleError(f"Managed file is missing: {action.path}") from exc
        if path.is_symlink() or not stat.S_ISREG(details.st_mode):
            raise LifecycleError(f"Managed file is not a safe regular file: {action.path}")
        if details.st_nlink > 1:
            raise LifecycleError(f"Hard-linked managed file refused: {action.path}")
        return sha256_file(path)

    def _validate_destination_available(
        self, path: Path, storage: str, action: ManagedFileAction
    ) -> None:
        expected = self._storage_path(storage, action)
        if path != expected:
            raise LifecycleError("Managed destination mismatch")
        if path.exists() or path.is_symlink():
            label = "game" if storage == "game" else "disabled storage"
            raise InstallationConflictError(
                f"Destination is occupied by an unmanaged file in {label}:\n"
                f"{action.path.as_posix()}"
            )
        if not self.case_sensitive and path.parent.is_dir():
            folded = path.name.casefold()
            for sibling in path.parent.iterdir():
                if sibling.name.casefold() == folded:
                    raise InstallationConflictError(
                        "Destination has a case-insensitive collision:\n"
                        f"{action.path.as_posix()}"
                    )

    @staticmethod
    def _move(source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            shutil.copy2(source, temporary, follow_symlinks=False)
            os.replace(temporary, destination)
            source.unlink()
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _set_storage(
        record: dict[str, Any], actions: Iterable[ManagedFileAction], storage: str
    ) -> None:
        action_by_path = {action.path.as_posix(): action for action in actions}
        for entry in record["installed_files"]:
            if isinstance(entry, dict) and entry.get("path") in action_by_path:
                entry["storage"] = storage
                entry["sha256"] = action_by_path[entry["path"]].sha256
                files = record.get("files")
                if isinstance(files, dict):
                    files[entry["path"]] = entry["sha256"]

    def _backup_path(
        self, storage: str, action: ManagedFileAction
    ) -> tuple[str, PurePosixPath]:
        if storage == "disabled":
            return storage, PurePosixPath(action.owner, *action.path.parts)
        return storage, action.path

    def _verify_transition(
        self, actions: Iterable[ManagedFileAction], storage: str
    ) -> None:
        for action in actions:
            target = self._storage_path(storage, action)
            if sha256_file(target) != action.sha256:
                raise LifecycleError(
                    f"Post-{storage} verification failed: {action.path.as_posix()}"
                )
            other = "disabled" if storage == "game" else "game"
            if self._storage_path(other, action).exists():
                raise LifecycleError(
                    f"Source remained after transition: {action.path.as_posix()}"
                )

    def _verify_absent(self, plan: RemovalPlan) -> None:
        for action in plan.files:
            if self._source_path(action).exists():
                raise LifecycleError(
                    f"Post-uninstall verification failed: {action.path.as_posix()}"
                )

    @staticmethod
    def _cleanup_candidates(actions: Iterable[ManagedFileAction]) -> list[PurePosixPath]:
        result: set[PurePosixPath] = set()
        for action in actions:
            parts = action.path.parts
            if action.source_storage == "game" and len(parts) >= 4:
                folded = tuple(part.casefold() for part in parts[:2])
                if folded[0] == "bepinex" and folded[1] != "config":
                    for size in range(3, len(parts)):
                        result.add(PurePosixPath(*parts[:size]))
        return sorted(result, key=lambda path: len(path.parts), reverse=True)

    def _cleanup_directories(self, directories: Iterable[PurePosixPath]) -> None:
        for relative in directories:
            candidate = self.game_root.joinpath(*relative.parts)
            if candidate.is_symlink():
                continue
            try:
                candidate.rmdir()
            except OSError:
                pass

    def _cleanup_disabled_directories(
        self, actions: Iterable[ManagedFileAction]
    ) -> None:
        candidates: set[Path] = set()
        for action in actions:
            if action.source_storage != "disabled":
                continue
            current = self.disabled_root / action.owner / Path(*action.path.parts[:-1])
            owner_root = self.disabled_root / action.owner
            while current != self.disabled_root and current != current.parent:
                candidates.add(current)
                if current == owner_root:
                    break
                current = current.parent
        for candidate in sorted(candidates, key=lambda path: len(path.parts), reverse=True):
            if candidate.is_symlink():
                continue
            try:
                candidate.rmdir()
            except OSError:
                pass

    def _rollback_or_raise(self, backup_id: str, original: Exception) -> None:
        try:
            self.backups.restore(backup_id)
        except Exception as rollback:
            raise BackupError(
                f"Lifecycle operation failed: {original}. Rollback also failed: {rollback}"
            ) from rollback
        raise LifecycleError(
            f"Lifecycle operation failed and was rolled back: {original}"
        ) from original
