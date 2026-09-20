"""Game-adapter contracts and installation-plan safety checks."""

from __future__ import annotations

import os
import re
import stat
from abc import ABC, abstractmethod
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from ..exceptions import GameAdapterError, InstallationConflictError
from ..models import InstallationPlan, InstallationPlanEntry, ResolvedMod

_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def validate_relative_destination(value: PurePosixPath | str) -> PurePosixPath:
    """Return a portable relative destination or reject it as untrusted."""
    raw = value.as_posix() if isinstance(value, PurePosixPath) else value
    if (
        not isinstance(raw, str)
        or not raw
        or raw.startswith(("/", "\\"))
        or _DRIVE_RE.match(raw)
        or "\\" in raw
        or any(ord(character) < 32 or ord(character) == 127 for character in raw)
    ):
        raise GameAdapterError(f"Unsafe installation destination: {raw!r}")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise GameAdapterError(f"Unsafe installation destination: {raw!r}")
    if any(":" in part for part in path.parts):
        raise GameAdapterError(f"Unsafe installation destination: {raw!r}")
    return path


def collision_key(path: PurePosixPath | str, *, case_sensitive: bool) -> str:
    """Produce a deterministic collision key independent of the host filesystem."""
    normalized = validate_relative_destination(path).as_posix()
    return normalized if case_sensitive else normalized.casefold()


def validate_plan(
    plan: InstallationPlan,
    *,
    managed_owners: dict[str, str] | None = None,
    case_sensitive: bool | None = None,
) -> InstallationPlan:
    """Validate all destinations, collisions, existing files, and symlink escapes."""
    root = plan.game_root.absolute()
    owners = managed_owners or {}
    sensitive = os.name != "nt" if case_sensitive is None else case_sensitive
    seen: dict[str, InstallationPlanEntry] = {}
    owner_keys = {
        collision_key(path, case_sensitive=sensitive): owner for path, owner in owners.items()
    }

    for entry in plan.entries:
        try:
            staged_details = entry.staged_file.lstat()
        except OSError as exc:
            raise GameAdapterError(
                f"Staged installation file is missing: {entry.staged_file}"
            ) from exc
        if (
            not stat.S_ISREG(staged_details.st_mode)
            or entry.staged_file.is_symlink()
            or staged_details.st_nlink > 1
        ):
            raise GameAdapterError(
                f"Staged installation file is not a safe regular file: {entry.staged_file}"
            )
        relative = validate_relative_destination(entry.destination)
        key = collision_key(relative, case_sensitive=sensitive)
        previous = seen.get(key)
        if previous is not None:
            raise InstallationConflictError(
                "Installation conflict\n\n"
                f"{previous.owner} and {entry.owner} both want to install:\n"
                f"{relative.as_posix()}"
            )
        seen[key] = entry

        destination = root.joinpath(*relative.parts)
        current = root
        for part in relative.parts[:-1]:
            current = current / part
            if current.is_symlink():
                raise GameAdapterError(
                    f"Refusing to traverse symbolic link: {current.relative_to(root)}"
                )
        try:
            destination.resolve(strict=False).relative_to(root.resolve(strict=False))
        except ValueError as exc:
            raise GameAdapterError(
                f"Installation destination escapes the game root: {relative.as_posix()}"
            ) from exc

        existing_owner = owner_keys.get(key)
        if destination.exists() or destination.is_symlink():
            details = destination.lstat()
            if (
                destination.is_symlink()
                or not stat.S_ISREG(details.st_mode)
                or details.st_nlink > 1
            ):
                raise InstallationConflictError(
                    f"Installation conflict: unsafe existing destination {relative.as_posix()}"
                )
            if existing_owner is None:
                raise InstallationConflictError(
                    "Installation conflict: unmanaged file already exists:\n"
                    f"{relative.as_posix()}"
                )
            if existing_owner != entry.owner:
                raise InstallationConflictError(
                    "Installation conflict\n\n"
                    f"{existing_owner} owns a file requested by {entry.owner}:\n"
                    f"{relative.as_posix()}"
                )
    return plan


class GameAdapter(ABC):
    """Translate staged provider artifacts into game-specific file operations."""

    game_id: str
    display_name: str

    @abstractmethod
    def validate_game(self, game_root: Path) -> None: ...

    @abstractmethod
    def build_installation_plan(
        self,
        game_root: Path,
        packages: Iterable[tuple[str, ResolvedMod, Path]],
    ) -> InstallationPlan: ...

    def verify_installation(
        self, game_root: Path, installed_files: Iterable[dict[str, Any]]
    ) -> list[str]:
        """Return adapter-specific problems; hash verification remains generic."""
        return []
