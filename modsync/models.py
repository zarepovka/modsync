"""Core immutable data models used by ModSync."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
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
