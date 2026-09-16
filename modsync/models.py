"""Core immutable data models used by ModSync."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Mod:
    """A mod declared in a modpack."""

    name: str
    version: str
    url: str
    sha256: str | None = None
    enabled: bool = True


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
