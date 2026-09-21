"""Small immutable view models returned by the application service."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class GameView:
    game_id: str
    display_name: str
    provider: str
    install_path: Path
    bep_in_ex_installed: bool
    profile_count: int


@dataclass(frozen=True, slots=True)
class ModView:
    name: str
    version: str
    source: str
    status: str
    install_reason: str


@dataclass(frozen=True, slots=True)
class UpdatePreview:
    changed_packages: tuple[str, ...]
    resolved_packages: int
    planned_files: int

    @property
    def available(self) -> bool:
        return bool(self.changed_packages or self.planned_files)
