"""Provider-neutral contracts and context for local game discovery."""

from __future__ import annotations

import os
import sys
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ..games.base import GameAdapter
from ..models import GameInstallation


@dataclass(frozen=True, slots=True)
class DiscoveryContext:
    """Injectable host details used by discovery providers.

    ``steam_roots`` and ``path_resolver`` make platform behavior testable with
    synthetic directories without changing environment variables or touching a
    real Steam installation.
    """

    platform: str = field(default_factory=lambda: sys.platform)
    home: Path = field(default_factory=Path.home)
    environ: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    steam_roots: Sequence[Path] | None = None
    registry_reader: Callable[[], Sequence[Path]] | None = None
    path_resolver: Callable[[str], Path] = Path


class GameDiscoveryProvider(ABC):
    """Read-only source of local installations for registered game adapters."""

    provider_id: str

    @abstractmethod
    def discover(self, adapter: GameAdapter) -> list[GameInstallation]:
        """Return candidates after passing each path to the game adapter."""


def platform_name(value: str) -> str:
    folded = value.strip().casefold()
    if folded.startswith("win") or folded == "windows":
        return "windows"
    if folded in {"darwin", "mac", "macos"}:
        return "macos"
    if folded.startswith("linux"):
        return "linux"
    return folded or "unknown"


def canonical_path_key(path: Path, platform: str) -> str:
    """Return a canonical identity, case-folded where host paths are insensitive."""
    value = os.path.normpath(str(path.expanduser().resolve(strict=False))).replace("\\", "/")
    if platform_name(platform) == "windows":
        value = value.casefold()
    return value
