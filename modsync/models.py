"""Core immutable data models used by ModSync."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any


@dataclass(frozen=True, slots=True)
class SourceSpec:
    """Validated provider configuration for a mod artifact."""

    type: str
    options: dict[str, str]


@dataclass(frozen=True, slots=True)
class Mod:
    """A mod declared in a modpack."""

    name: str
    version: str | None
    url: str | None
    sha256: str | None = None
    enabled: bool = True
    source: SourceSpec | None = None


@dataclass(frozen=True, slots=True)
class ResolvedMod:
    """Provider-independent description of one downloadable mod release."""

    name: str
    version: str
    download_url: str
    filename: str
    sha256: str | None
    source_metadata: dict[str, Any]
    release_metadata: dict[str, Any]
    source_identity: dict[str, Any]
    request_headers: dict[str, str] = field(default_factory=dict, repr=False)
    dependencies: tuple["ResolvedMod", ...] = ()
    warnings: tuple[str, ...] = ()
    provider_key: str | None = None


@dataclass(frozen=True, slots=True)
class ResolvedPlanItem:
    """One provider-neutral installation-plan entry."""

    mod: Mod
    resolved: ResolvedMod
    explicit: bool
    required_by: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Modpack:
    """A validated modpack and its resolved installation directory."""

    name: str
    version: str
    description: str
    game: str
    install_directory: Path
    mods: tuple[Mod, ...]
    source_path: Path
    state_path: Path | None = None
    backup_directory: Path | None = None
    game_adapter_id: str | None = None


@dataclass(frozen=True, slots=True)
class InstallationPlanEntry:
    """One prevalidated file operation produced by a game adapter."""

    staged_file: Path
    destination: PurePosixPath
    owner: str
    mod_name: str
    action: str = "install"
    metadata: dict[str, Any] = field(default_factory=dict)
    conflicts: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class InstallationPlan:
    """Complete set of game-file operations built before installation starts."""

    game_id: str
    game_root: Path
    entries: tuple[InstallationPlanEntry, ...]


@dataclass(frozen=True, slots=True)
class ManagedFileAction:
    """A validated ownership-based file operation."""

    path: PurePosixPath
    owner: str
    sha256: str
    source_storage: str
    destination_storage: str | None = None


@dataclass(frozen=True, slots=True)
class RemovalPlan:
    """Complete file-removal decision produced before uninstall starts."""

    mod_name: str
    package: str
    files: tuple[ManagedFileAction, ...]
    preserved_files: tuple[PurePosixPath, ...]
    orphan_dependencies: tuple[str, ...]
    cleanup_directories: tuple[PurePosixPath, ...]
    backup_required: bool = True


@dataclass(frozen=True, slots=True)
class DisablePlan:
    """Runtime files to move from the game into protected disabled storage."""

    mod_name: str
    package: str
    files: tuple[ManagedFileAction, ...]
    required_by: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EnablePlan:
    """Disabled runtime files to restore to their original game destinations."""

    mod_name: str
    package: str
    files: tuple[ManagedFileAction, ...]
    dependencies: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SwitchFile:
    """One ownership-validated file participating in a profile transition."""

    path: PurePosixPath
    owner: str
    sha256: str
    mod_name: str
    source: str


@dataclass(frozen=True, slots=True)
class SwitchPlan:
    """Complete, immutable profile reconciliation decision."""

    source_profile: str
    target_profile: str
    game_root: Path
    keep: tuple[SwitchFile, ...]
    remove: tuple[SwitchFile, ...]
    install: tuple[SwitchFile, ...]
    preserve_configs: tuple[SwitchFile, ...]
    restore_configs: tuple[SwitchFile, ...]
    disabled_packages: tuple[str, ...]
    dependencies: tuple[str, ...]
    unmanaged_conflicts: tuple[PurePosixPath, ...]
    modified_runtime_files: tuple[PurePosixPath, ...]
    download_packages: tuple[str, ...]
    backup_required: bool = True


@dataclass(slots=True)
class SwitchReport:
    """Summary of a dry-run or completed physical profile switch."""

    source_profile: str
    target_profile: str
    dry_run: bool = False
    changed: bool = False
    backup_id: str | None = None
    kept: int = 0
    removed: int = 0
    installed: int = 0
    restored_configs: int = 0
    disabled_packages: tuple[str, ...] = ()
    download_packages: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Profile:
    """Validated metadata for one stored ModSync profile."""

    name: str
    game: str
    install_directory: Path
    created_at: str
    updated_at: str
    modpack_source: Path
    mod_count: int
    directory: Path


@dataclass(frozen=True, slots=True)
class InstallFailure:
    """One mod that failed without aborting the full operation."""

    mod_name: str
    message: str


@dataclass(slots=True)
class InstallReport:
    """Summary returned by install and update operations."""

    installed: int = 0
    skipped: int = 0
    failures: list[InstallFailure] = field(default_factory=list)
    backup_id: str | None = None
    rollback_attempted: bool = False
    rollback_succeeded: bool | None = None
    rollback_error: str | None = None
    warnings: list[str] = field(default_factory=list)
    resolved: int = 0
    planned_files: int = 0
    plan_entries: tuple[InstallationPlanEntry, ...] = ()


@dataclass(slots=True)
class LifecycleReport:
    """Result of uninstall, disable, or enable."""

    action: str
    mod_name: str
    changed: bool = False
    dry_run: bool = False
    backup_id: str | None = None
    paths: tuple[str, ...] = ()
    preserved_files: tuple[str, ...] = ()
    orphan_dependencies: tuple[str, ...] = ()
    rollback_attempted: bool = False
    rollback_succeeded: bool | None = None
    rollback_error: str | None = None


@dataclass(frozen=True, slots=True)
class BackupInfo:
    """Validated summary of one stored backup."""

    backup_id: str
    created_at: str
    modpack_name: str
    modpack_version: str
    reason: str
    file_count: int


@dataclass(frozen=True, slots=True)
class RestoreReport:
    """Summary of a successful manual or automatic restore."""

    backup_id: str
    restored_files: int
    state_restored: bool


@dataclass(frozen=True, slots=True)
class VerificationIssue:
    """An integrity or state problem for an installed mod."""

    mod_name: str
    message: str


@dataclass(frozen=True, slots=True)
class VerificationReport:
    """Result of verifying every enabled mod in a pack."""

    checked: int
    issues: tuple[VerificationIssue, ...]

    @property
    def ok(self) -> bool:
        return not self.issues
