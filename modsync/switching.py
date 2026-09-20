"""Transactional reconciliation of one physical game root between profiles."""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import uuid
from contextlib import ExitStack
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

from .backup import BackupManager, DEFAULT_RETENTION
from .exceptions import (
    BackupError,
    ProfileSwitchError,
    SwitchConflictError,
)
from .games.base import collision_key, validate_relative_destination
from .hashing import sha256_file
from .installer import Installer
from .models import Mod, Modpack, ResolvedMod, SwitchFile, SwitchPlan, SwitchReport
from .profiles import ProfileStore
from .state import (
    atomic_write_json,
    disabled_storage_path,
    load_state_file,
    record_status,
    save_state_file,
)

_SHA256_LENGTH = 64


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _SHA256_LENGTH
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_config(path: PurePosixPath) -> bool:
    return len(path.parts) >= 2 and tuple(part.casefold() for part in path.parts[:2]) == (
        "bepinex",
        "config",
    )


class ProfileSwitcher:
    """Build and apply complete profile transitions under cross-profile locks."""

    def __init__(
        self,
        store: ProfileStore,
        *,
        installer: Installer | None = None,
        case_sensitive: bool | None = None,
    ) -> None:
        self.store = store
        self.installer = installer or Installer()
        self.case_sensitive = os.name != "nt" if case_sensitive is None else case_sensitive

    def switch(self, target_name: str, *, dry_run: bool = False) -> SwitchReport:
        """Physically reconcile the active profile's game root with ``target_name``."""
        with self.store.global_lock():
            source_name = self.store.active_name()
            if source_name is None:
                raise ProfileSwitchError(
                    "No active source profile. Activate the profile that currently owns "
                    "the physical game state before switching."
                )
            source_profile = self.store.get(source_name)
            target_profile = self.store.get(target_name)
            self._validate_compatible(source_profile, target_profile)
            marker = self.store.switch_marker(source_profile.install_directory)
            if marker.exists() or marker.is_symlink():
                raise ProfileSwitchError(
                    "A previous profile switch may not have completed cleanly. "
                    "Run verify or restore the latest backup."
                )

            with ExitStack() as locks:
                locks.enter_context(self.store.game_root_lock(source_profile.install_directory))
                for name in sorted(
                    {source_profile.name, target_profile.name}, key=str.casefold
                ):
                    locks.enter_context(self.store.lock(name))
                return self._switch_locked(
                    source_profile.name, target_profile.name, marker, dry_run=dry_run
                )

    def build_plan(self, source_name: str, target_name: str) -> SwitchPlan:
        """Build and fully validate a read-only SwitchPlan."""
        source_profile = self.store.get(source_name)
        target_profile = self.store.get(target_name)
        self._validate_compatible(source_profile, target_profile)
        source_pack = self.store.load_modpack(source_profile.name)
        target_pack = self.store.load_modpack(target_profile.name)
        source_state = load_state_file(source_pack.state_path or Path())
        target_state = load_state_file(target_pack.state_path or Path())
        self._validate_configured_mods(target_pack, target_state)
        self._validate_dependencies(target_state)

        source_files = self._state_files(source_pack, source_state)
        target_files = self._state_files(target_pack, target_state)
        source_game = {
            key.removeprefix("game:"): value
            for key, value in source_files.items()
            if value.source == "game"
        }
        target_game = {
            key.removeprefix("game:"): value
            for key, value in target_files.items()
            if value.source == "game"
        }
        target_preserved = self._preserved_manifest(target_profile.name)

        current_digests: dict[str, str] = {}
        preserve: list[SwitchFile] = []
        modified_runtime: list[PurePosixPath] = []
        for key, item in source_game.items():
            candidate = self._safe_target(source_pack.install_directory, item.path, "game")
            digest = self._safe_digest(candidate, item.path, "source profile")
            current_digests[key] = digest
            if digest != item.sha256:
                if _is_config(item.path):
                    preserve.append(item)
                else:
                    modified_runtime.append(item.path)

        keep: list[SwitchFile] = []
        install: list[SwitchFile] = []
        restore: list[SwitchFile] = []
        downloads: set[str] = set()
        unmanaged: list[PurePosixPath] = []
        for key, item in target_game.items():
            source_item = source_game.get(key)
            if (
                source_item is not None
                and source_item.owner == item.owner
                and current_digests.get(key) == item.sha256
            ):
                keep.append(item)
                continue

            destination = self._safe_target(target_pack.install_directory, item.path, "game")
            if source_item is None and (destination.exists() or destination.is_symlink()):
                unmanaged.append(item.path)
                continue
            preserved = target_preserved.get(item.path.as_posix())
            if _is_config(item.path) and preserved is not None:
                restore.append(
                    SwitchFile(item.path, item.owner, preserved[0], item.mod_name, "preserved")
                )
                continue
            artifact = self._safe_target(
                self._artifact_root(target_profile.name), item.path, "profile artifact"
            )
            if artifact.is_file() and not artifact.is_symlink():
                details = artifact.lstat()
                if stat.S_ISREG(details.st_mode) and details.st_nlink == 1:
                    if sha256_file(artifact) == item.sha256:
                        install.append(
                            SwitchFile(
                                item.path,
                                item.owner,
                                item.sha256,
                                item.mod_name,
                                "artifact",
                            )
                        )
                        continue
            install.append(
                SwitchFile(item.path, item.owner, item.sha256, item.mod_name, "download")
            )
            downloads.add(item.mod_name)

        keep_keys = {
            collision_key(item.path, case_sensitive=self.case_sensitive) for item in keep
        }
        remove = [item for key, item in source_game.items() if key not in keep_keys]
        disabled = tuple(
            sorted(
                name
                for name, record in target_state["mods"].items()
                if isinstance(name, str)
                and isinstance(record, dict)
                and record_status(record) == "disabled"
            )
        )
        dependencies = tuple(
            sorted(
                {
                    dependency
                    for record in target_state["mods"].values()
                    if isinstance(record, dict)
                    for dependency in self._dependencies(record)
                },
                key=str.casefold,
            )
        )
        return SwitchPlan(
            source_profile.name,
            target_profile.name,
            source_pack.install_directory,
            tuple(sorted(keep, key=lambda item: item.path.as_posix().casefold())),
            tuple(sorted(remove, key=lambda item: item.path.as_posix().casefold())),
            tuple(sorted(install, key=lambda item: item.path.as_posix().casefold())),
            tuple(sorted(preserve, key=lambda item: item.path.as_posix().casefold())),
            tuple(sorted(restore, key=lambda item: item.path.as_posix().casefold())),
            disabled,
            dependencies,
            tuple(sorted(unmanaged, key=lambda item: item.as_posix().casefold())),
            tuple(sorted(modified_runtime, key=lambda item: item.as_posix().casefold())),
            tuple(sorted(downloads, key=str.casefold)),
        )

    def _switch_locked(
        self, source_name: str, target_name: str, marker: Path, *, dry_run: bool
    ) -> SwitchReport:
        plan = self.build_plan(source_name, target_name)
        if plan.modified_runtime_files:
            raise SwitchConflictError(
                "Modified managed runtime file detected:\n"
                + "\n".join(path.as_posix() for path in plan.modified_runtime_files)
            )
        if plan.unmanaged_conflicts:
            raise SwitchConflictError(
                "Target destination is occupied by an unmanaged file:\n"
                + "\n".join(path.as_posix() for path in plan.unmanaged_conflicts)
            )
        report = SwitchReport(
            plan.source_profile,
            plan.target_profile,
            dry_run=dry_run,
            kept=len(plan.keep),
            removed=len(plan.remove),
            installed=len(plan.install) + len(plan.restore_configs),
            restored_configs=len(plan.restore_configs),
            disabled_packages=plan.disabled_packages,
            download_packages=plan.download_packages,
        )
        if dry_run:
            return report
        if source_name.casefold() == target_name.casefold():
            self._verify_target(self.store.load_modpack(target_name))
            return report

        source_pack = self.store.load_modpack(source_name)
        target_pack = self.store.load_modpack(target_name)
        source_state = load_state_file(source_pack.state_path or Path())
        target_state = load_state_file(target_pack.state_path or Path())
        source_files = self._state_files(source_pack, source_state)

        transaction_parent = self.store.root
        transaction_parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=".modsync-switch-", dir=transaction_parent
        ) as temporary_name:
            temporary = Path(temporary_name)
            prepared = self._prepare_downloads(plan, target_pack, target_state, temporary)
            affected_game = tuple(
                sorted(
                    {item.path for item in (*plan.remove, *plan.install, *plan.restore_configs)},
                    key=lambda path: path.as_posix().casefold(),
                )
            )
            source_artifact_paths = tuple(
                item.path for item in source_files.values() if item.source == "game"
            )
            source_config_paths = tuple(
                item.path
                for item in source_files.values()
                if item.source == "game" and _is_config(item.path)
            )
            backup_manager = BackupManager(source_pack)
            backup = backup_manager.create_switch(
                source_profile=source_name,
                target_profile=target_name,
                active_profile=source_name,
                game_root_identity=self.store.game_root_identity(
                    source_pack.install_directory
                ),
                affected_game_paths=affected_game,
                source_artifact_paths=source_artifact_paths,
                source_config_paths=source_config_paths,
                source_state=source_state,
                target_state=target_state,
                config_snapshot=self.store._load_config(),
            )
            report.backup_id = backup.backup_id
            marker.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(
                marker,
                {
                    "schema_version": 1,
                    "source_profile": source_name,
                    "target_profile": target_name,
                    "backup_id": backup.backup_id,
                    "started_at": _utc_now(),
                },
            )
            try:
                source_state_copy = deepcopy(source_state)
                target_state_copy = deepcopy(target_state)
                self._capture_source(source_name, source_files, source_state_copy)
                self.apply_removals(plan)
                self.apply_target(plan, target_name, prepared, target_state_copy)
                self._verify_state(target_pack, target_state_copy)
                save_state_file(source_pack.state_path or Path(), source_state_copy)
                save_state_file(target_pack.state_path or Path(), target_state_copy)
                self.store._save_config(target_name)
                self._verify_target(target_pack)
                report.changed = True
            except Exception as exc:
                try:
                    backup_manager.restore(backup.backup_id)
                except Exception as rollback:
                    raise BackupError(
                        f"Profile switch failed: {exc}. Rollback also failed: {rollback}"
                    ) from rollback
                raise ProfileSwitchError(
                    f"Profile switch failed and was rolled back: {exc}"
                ) from exc
            finally:
                marker.unlink(missing_ok=True)
            try:
                backup_manager.prune(DEFAULT_RETENTION)
            except BackupError:
                pass
        return report

    def apply_removals(self, plan: SwitchPlan) -> None:
        """Remove source-owned paths; kept paths are never touched."""
        for item in plan.remove:
            target = self._safe_target(plan.game_root, item.path, "game")
            target.unlink()

    def apply_target(
        self,
        plan: SwitchPlan,
        target_name: str,
        prepared: dict[str, Path],
        target_state: dict[str, Any],
    ) -> None:
        """Install target artifacts and profile-specific preserved configs."""
        restored_paths = {item.path.as_posix() for item in plan.restore_configs}
        for item in (*plan.install, *plan.restore_configs):
            if item.path.as_posix() in restored_paths:
                source = self._safe_target(
                    self._config_root(target_name), item.path, "preserved config"
                )
            elif item.source == "artifact":
                source = self._safe_target(
                    self._artifact_root(target_name), item.path, "profile artifact"
                )
            else:
                source = prepared.get(item.path.as_posix())
                if source is None:
                    raise ProfileSwitchError(
                        f"Prepared target artifact is missing: {item.path.as_posix()}"
                    )
            digest = self._safe_digest(source, item.path, "target artifact")
            if digest != item.sha256:
                raise ProfileSwitchError(
                    f"Target artifact SHA256 mismatch: {item.path.as_posix()}"
                )
            destination = self._safe_target(plan.game_root, item.path, "game")
            if destination.exists() or destination.is_symlink():
                raise SwitchConflictError(
                    "Target destination became occupied before apply: "
                    f"{item.path.as_posix()}"
                )
            self._atomic_copy(source, destination)
            if item.path.as_posix() in restored_paths:
                self._set_state_digest(target_state, item.mod_name, item.path, digest)

    def _capture_source(
        self,
        source_name: str,
        source_files: dict[str, SwitchFile],
        source_state: dict[str, Any],
    ) -> None:
        artifact_root = self._artifact_root(source_name)
        config_root = self._config_root(source_name)
        manifest = self._preserved_manifest(source_name)
        for item in source_files.values():
            if item.source != "game":
                continue
            game_file = self._safe_target(
                self.store.get(source_name).install_directory, item.path, "game"
            )
            digest = self._safe_digest(game_file, item.path, "source profile")
            preserve = _is_config(item.path) and (
                digest != item.sha256 or item.path.as_posix() in manifest
            )
            root = config_root if preserve else artifact_root
            destination = self._safe_target(
                root, item.path, "preserved config" if preserve else "profile artifact"
            )
            self._atomic_copy(game_file, destination)
            if preserve:
                manifest[item.path.as_posix()] = (digest, item.mod_name)
                self._set_state_digest(source_state, item.mod_name, item.path, digest)
        self._write_preserved_manifest(source_name, manifest)

    def _prepare_downloads(
        self,
        plan: SwitchPlan,
        target_pack: Modpack,
        target_state: dict[str, Any],
        temporary: Path,
    ) -> dict[str, Path]:
        needed = {
            item.mod_name: []
            for item in plan.install
            if item.source == "download"
        }
        for item in plan.install:
            if item.source == "download":
                needed[item.mod_name].append(item)
        if not needed:
            return {}
        adapter = self.installer.game_registry.get(
            target_pack.game_adapter_id or target_pack.game
        )
        prepared_paths: dict[str, Path] = {}
        for mod_name, actions in needed.items():
            record = target_state["mods"].get(mod_name)
            if not isinstance(record, dict):
                raise ProfileSwitchError(f"Target package state is missing: {mod_name}")
            resolved = self._resolved_from_state(mod_name, record)
            mod = Mod(mod_name, resolved.version, resolved.download_url, resolved.sha256)
            prepared = self.installer.prepare_mod(
                temporary / "downloads" / self._safe_component(mod_name),
                mod,
                resolved,
                None,
            )
            installation = adapter.build_installation_plan(
                target_pack.install_directory,
                ((mod_name, resolved, prepared.staged_directory),),
            )
            by_path = {
                entry.destination.as_posix(): entry for entry in installation.entries
            }
            for action in actions:
                entry = by_path.get(action.path.as_posix())
                if entry is None or entry.owner != action.owner:
                    raise ProfileSwitchError(
                        f"Downloaded package no longer matches target state: {mod_name}"
                    )
                if sha256_file(entry.staged_file) != action.sha256:
                    raise ProfileSwitchError(
                        f"Downloaded file differs from target state: {action.path.as_posix()}"
                    )
                prepared_paths[action.path.as_posix()] = entry.staged_file
        return prepared_paths

    @staticmethod
    def _resolved_from_state(mod_name: str, record: dict[str, Any]) -> ResolvedMod:
        source = record.get("source")
        version = record.get("version")
        if not isinstance(source, dict) or not isinstance(version, str):
            raise ProfileSwitchError(f"Target source metadata is incomplete: {mod_name}")
        source_type = source.get("type")
        release = source.get("release")
        identity = source.get("identity")
        if not isinstance(source_type, str):
            raise ProfileSwitchError(f"Target source type is missing: {mod_name}")
        release = release if isinstance(release, dict) else {}
        identity = identity if isinstance(identity, dict) else {}
        if source_type == "direct":
            url = source.get("url")
            filename = PurePosixPath(unquote(urlsplit(str(url)).path)).name
        elif source_type == "github":
            url = release.get("download_url")
            filename = release.get("asset")
        elif source_type == "thunderstore":
            url = source.get("download_url")
            namespace = source.get("namespace")
            package = source.get("package")
            filename = f"{namespace}-{package}-{version}.zip"
        else:
            raise ProfileSwitchError(f"Unsupported stored source type: {source_type}")
        if (
            not isinstance(url, str)
            or urlsplit(url).scheme not in {"http", "https"}
            or not isinstance(filename, str)
            or not filename
            or Path(filename).name != filename
        ):
            raise ProfileSwitchError(f"Stored download metadata is unsafe: {mod_name}")
        source_metadata = {
            key: value
            for key, value in source.items()
            if key not in {"identity", "release", "sha256"}
        }
        source_sha = record.get("source_sha256")
        if not _is_sha256(source_sha):
            source_sha = source.get("sha256")
        if not _is_sha256(source_sha):
            raise ProfileSwitchError(f"Stored artifact checksum is missing: {mod_name}")
        headers: dict[str, str] = {}
        if source_type == "github":
            token = os.environ.get("MODSYNC_GITHUB_TOKEN")
            if token:
                headers["Authorization"] = f"Bearer {token}"
        return ResolvedMod(
            mod_name,
            version,
            url,
            filename,
            source_sha,
            source_metadata,
            release,
            identity,
            request_headers=headers,
        )

    def _state_files(
        self, modpack: Modpack, state: dict[str, Any]
    ) -> dict[str, SwitchFile]:
        result: dict[str, SwitchFile] = {}
        disabled_root = disabled_storage_path(modpack)
        for mod_name, record in state["mods"].items():
            if not isinstance(mod_name, str) or not isinstance(record, dict):
                raise ProfileSwitchError("Profile state contains an invalid package record")
            status = record_status(record)
            if status not in {"enabled", "disabled"}:
                raise ProfileSwitchError(f"Invalid package status: {mod_name}")
            owner = record.get("owner", record.get("package"))
            files = record.get("installed_files")
            if not isinstance(owner, str) or not owner or not isinstance(files, list):
                raise ProfileSwitchError(f"Package lacks ownership state: {mod_name}")
            for entry in files:
                if not isinstance(entry, dict) or entry.get("owner") != owner:
                    raise ProfileSwitchError(f"Ownership mismatch in target state: {mod_name}")
                try:
                    path = validate_relative_destination(entry.get("path"))
                except Exception as exc:
                    raise ProfileSwitchError(
                        f"Unsafe managed path in profile state: {mod_name}"
                    ) from exc
                digest = entry.get("sha256")
                storage = entry.get("storage", "game")
                if not _is_sha256(digest) or storage not in {"game", "disabled"}:
                    raise ProfileSwitchError(f"Invalid managed file state: {mod_name}")
                if status == "enabled" and storage != "game":
                    raise ProfileSwitchError(
                        f"Enabled package has files outside the game: {mod_name}"
                    )
                key = f"{storage}:{collision_key(path, case_sensitive=self.case_sensitive)}"
                if key in result:
                    raise SwitchConflictError(
                        f"Profile state has a shared or case-colliding destination: {path}"
                    )
                item = SwitchFile(path, owner, digest, mod_name, storage)
                result[key] = item
                if storage == "disabled":
                    candidate = self._safe_target(
                        disabled_root, PurePosixPath(owner, *path.parts), "disabled storage"
                    )
                    actual = self._safe_digest(candidate, path, "disabled storage")
                    if actual != digest:
                        raise ProfileSwitchError(
                            f"Disabled file SHA256 mismatch: {path.as_posix()}"
                        )
        return result

    @staticmethod
    def _dependencies(record: dict[str, Any]) -> list[str]:
        values = record.get("dependencies")
        if values is None:
            source = record.get("source")
            release = source.get("release") if isinstance(source, dict) else None
            raw = release.get("dependencies", []) if isinstance(release, dict) else []
            if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
                raise ProfileSwitchError("Invalid legacy dependency metadata")
            values = []
            for item in raw:
                parts = item.rsplit("-", 2)
                if len(parts) != 3 or not all(parts):
                    raise ProfileSwitchError("Invalid legacy dependency metadata")
                values.append(f"{parts[0]}-{parts[1]}")
        if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
            raise ProfileSwitchError("Invalid dependency metadata in target profile")
        return values

    def _validate_dependencies(self, state: dict[str, Any]) -> None:
        records = state["mods"]
        aliases: dict[str, dict[str, Any]] = {}
        for name, record in records.items():
            if not isinstance(name, str) or not isinstance(record, dict):
                raise ProfileSwitchError("Invalid target profile package state")
            for alias in self._record_aliases(name, record):
                if alias in aliases and aliases[alias] is not record:
                    raise ProfileSwitchError(f"Ambiguous target dependency: {name}")
                aliases[alias] = record
        for name, record in records.items():
            if record_status(record) != "enabled":
                continue
            for dependency in self._dependencies(record):
                target = aliases.get(dependency.casefold())
                if target is None or record_status(target) != "enabled":
                    raise ProfileSwitchError(
                        f"Cannot switch to {name}. Required dependency is unavailable: "
                        f"{dependency}"
                    )

    @staticmethod
    def _record_aliases(name: str, record: dict[str, Any]) -> set[str]:
        aliases = {name.casefold()}
        source = record.get("source")
        identity = source.get("identity") if isinstance(source, dict) else None
        if isinstance(identity, dict):
            namespace = identity.get("namespace")
            package = identity.get("package")
            if isinstance(namespace, str) and isinstance(package, str):
                aliases.add(f"{namespace}-{package}".casefold())
        return aliases

    def _validate_configured_mods(
        self, modpack: Modpack, state: dict[str, Any]
    ) -> None:
        records = state["mods"]
        available = {
            alias
            for name, record in records.items()
            if isinstance(name, str) and isinstance(record, dict)
            for alias in self._record_aliases(name, record)
        }
        missing = [
            mod.name
            for mod in modpack.mods
            if mod.enabled and mod.name.casefold() not in available
        ]
        if missing:
            raise ProfileSwitchError(
                "Target profile has no installed state for: " + ", ".join(missing)
            )

    @staticmethod
    def _validate_compatible(source: Any, target: Any) -> None:
        if source.game.casefold() != target.game.casefold():
            raise ProfileSwitchError(
                f"Cannot physically switch between different games: "
                f"{source.game} and {target.game}"
            )
        if source.install_directory.resolve() != target.install_directory.resolve():
            raise ProfileSwitchError(
                "Profile switch requires the same physical game root. "
                "Use profile activate for logical selection across different roots."
            )

    def _verify_target(self, modpack: Modpack) -> None:
        state = load_state_file(modpack.state_path or Path())
        self._verify_state(modpack, state)

    def _verify_state(self, modpack: Modpack, state: dict[str, Any]) -> None:
        files = self._state_files(modpack, state)
        for item in files.values():
            if item.source != "game":
                continue
            target = self._safe_target(modpack.install_directory, item.path, "game")
            if self._safe_digest(target, item.path, "target profile") != item.sha256:
                raise ProfileSwitchError(
                    f"Post-switch verification failed: {item.path.as_posix()}"
                )

    def _preserved_manifest(self, profile_name: str) -> dict[str, tuple[str, str]]:
        profile = self.store.get(profile_name)
        path = profile.directory / "preserved-config.json"
        if not path.exists():
            return {}
        if path.is_symlink() or not path.is_file():
            raise ProfileSwitchError(f"Unsafe preserved config metadata: {profile_name}")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProfileSwitchError(
                f"Cannot read preserved config metadata: {profile_name}"
            ) from exc
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise ProfileSwitchError(f"Invalid preserved config metadata: {profile_name}")
        result: dict[str, tuple[str, str]] = {}
        entries = value.get("files")
        if not isinstance(entries, list):
            raise ProfileSwitchError(f"Invalid preserved config metadata: {profile_name}")
        for entry in entries:
            if not isinstance(entry, dict):
                raise ProfileSwitchError("Invalid preserved config record")
            try:
                relative = validate_relative_destination(entry.get("path"))
            except Exception as exc:
                raise ProfileSwitchError("Unsafe preserved config path") from exc
            digest = entry.get("sha256")
            origin = entry.get("originating_profile")
            if not _is_config(relative) or not _is_sha256(digest) or origin != profile_name:
                raise ProfileSwitchError("Invalid preserved config record")
            candidate = self._safe_target(
                self._config_root(profile_name), relative, "preserved config"
            )
            if self._safe_digest(candidate, relative, "preserved config") != digest:
                raise ProfileSwitchError(
                    f"Preserved config SHA256 mismatch: {relative.as_posix()}"
                )
            result[relative.as_posix()] = (digest, entry.get("mod_name", "config"))
        return result

    def _write_preserved_manifest(
        self, profile_name: str, entries: dict[str, tuple[str, str]]
    ) -> None:
        profile = self.store.get(profile_name)
        atomic_write_json(
            profile.directory / "preserved-config.json",
            {
                "schema_version": 1,
                "files": [
                    {
                        "path": path,
                        "sha256": digest,
                        "originating_profile": profile_name,
                        "mod_name": mod_name,
                        "updated_at": _utc_now(),
                    }
                    for path, (digest, mod_name) in sorted(entries.items())
                ],
            },
        )

    def _artifact_root(self, profile_name: str) -> Path:
        return self.store.get(profile_name).directory / "artifacts"

    def _config_root(self, profile_name: str) -> Path:
        return self.store.get(profile_name).directory / "preserved-config"

    @staticmethod
    def _safe_component(value: str) -> str:
        return uuid.uuid5(uuid.NAMESPACE_URL, value).hex

    @staticmethod
    def _set_state_digest(
        state: dict[str, Any], mod_name: str, path: PurePosixPath, digest: str
    ) -> None:
        record = state["mods"].get(mod_name)
        if not isinstance(record, dict):
            raise ProfileSwitchError(f"Package state disappeared: {mod_name}")
        found = False
        for entry in record.get("installed_files", []):
            if isinstance(entry, dict) and entry.get("path") == path.as_posix():
                entry["sha256"] = digest
                found = True
        files = record.get("files")
        if isinstance(files, dict) and path.as_posix() in files:
            files[path.as_posix()] = digest
        if not found:
            raise ProfileSwitchError(f"Managed config state disappeared: {path}")

    @staticmethod
    def _safe_target(root: Path, relative: PurePosixPath, label: str) -> Path:
        safe = validate_relative_destination(relative)
        if root.is_symlink():
            raise ProfileSwitchError(f"Unsafe {label} root")
        target = root.joinpath(*safe.parts)
        current = root
        for part in safe.parts[:-1]:
            current = current / part
            if current.is_symlink():
                raise ProfileSwitchError(
                    f"Symbolic link in {label} path: {safe.as_posix()}"
                )
        try:
            target.resolve(strict=False).relative_to(root.resolve(strict=False))
        except ValueError as exc:
            raise ProfileSwitchError(f"Path escapes {label}: {safe.as_posix()}") from exc
        return target

    @staticmethod
    def _safe_digest(path: Path, relative: PurePosixPath, label: str) -> str:
        try:
            details = path.lstat()
        except OSError as exc:
            raise ProfileSwitchError(
                f"Missing {label} file: {relative.as_posix()}"
            ) from exc
        if path.is_symlink() or not stat.S_ISREG(details.st_mode):
            raise ProfileSwitchError(f"Unsafe {label} file: {relative.as_posix()}")
        if details.st_nlink > 1:
            raise ProfileSwitchError(f"Hard-linked {label} file: {relative.as_posix()}")
        return sha256_file(path)

    @staticmethod
    def _atomic_copy(source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            shutil.copy2(source, temporary, follow_symlinks=False)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
