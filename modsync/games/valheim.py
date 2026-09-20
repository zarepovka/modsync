"""Valheim adapter implementing the documented Thunderstore/BepInEx layout."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Iterable

from ..config import mod_directory_name
from ..exceptions import GameAdapterError
from ..models import InstallationPlan, InstallationPlanEntry, ResolvedMod
from .base import GameAdapter

_ROUTING_DIRECTORIES = {"plugins", "core", "patchers", "monomod", "config"}
_PACKAGE_METADATA = {"manifest.json", "readme.md", "changelog.md", "icon.png"}


def _owner(mod_name: str, resolved: ResolvedMod) -> str:
    metadata = resolved.source_metadata
    if metadata.get("type") == "thunderstore":
        namespace = metadata.get("namespace")
        package = metadata.get("package")
        if isinstance(namespace, str) and isinstance(package, str):
            return mod_directory_name(f"{namespace}-{package}")
    return mod_directory_name(mod_name)


def _is_bepinex_package(resolved: ResolvedMod, staged: Path) -> bool:
    package = resolved.source_metadata.get("package")
    if isinstance(package, str) and package.casefold().startswith("bepinexpack"):
        return True
    return (staged / "doorstop_config.ini").is_file() and (staged / "BepInEx" / "core").is_dir()


def _route_file(relative: PurePosixPath, owner: str) -> PurePosixPath | None:
    parts = relative.parts
    if not parts:
        return None
    if parts[-1].casefold() in _PACKAGE_METADATA and len(parts) == 1:
        return None

    folded = [part.casefold() for part in parts]
    if len(parts) >= 2 and folded[0] == "bepinex" and folded[1] == "bepinex":
        raise GameAdapterError(f"Malformed package layout: {relative.as_posix()}")
    if folded[0] == "bepinex":
        parts = parts[1:]
        folded = folded[1:]
        if not parts:
            return None

    route_index = next(
        (index for index, part in enumerate(folded) if part in _ROUTING_DIRECTORIES),
        None,
    )
    if route_index is not None:
        route = folded[route_index]
        remainder = parts[route_index + 1 :]
        if not remainder:
            return None
        if route == "config":
            return PurePosixPath("BepInEx", route, *remainder)
        return PurePosixPath("BepInEx", route, owner, *remainder)

    if parts[-1].casefold().endswith(".mm.dll"):
        return PurePosixPath("BepInEx", "monomod", owner, parts[-1])
    # Current r2modman behavior ignores non-override directory components.
    return PurePosixPath("BepInEx", "plugins", owner, parts[-1])


class ValheimAdapter(GameAdapter):
    game_id = "valheim"
    display_name = "Valheim"
    steam_app_id = 892970

    def validate_game(self, game_root: Path) -> None:
        if game_root.is_symlink() or not game_root.is_dir():
            raise GameAdapterError(f"Valheim game root is not a directory: {game_root}")
        try:
            names = {item.name.casefold() for item in game_root.iterdir()}
        except OSError as exc:
            raise GameAdapterError(f"Cannot inspect Valheim game root: {exc}") from exc
        markers = {"valheim.exe", "valheim.x86", "valheim.x86_64", "valheim.app", "valheim_data"}
        if not names.intersection(markers):
            raise GameAdapterError(
                "The selected directory does not appear to be a Valheim installation."
            )

    @staticmethod
    def has_bepinex(game_root: Path) -> bool:
        root = game_root / "BepInEx"
        return root.is_dir() and ((root / "core").is_dir() or (root / "plugins").is_dir())

    def build_installation_plan(
        self,
        game_root: Path,
        packages: Iterable[tuple[str, ResolvedMod, Path]],
    ) -> InstallationPlan:
        entries: list[InstallationPlanEntry] = []
        loader_present = False
        package_values = list(packages)
        for mod_name, resolved, staged in package_values:
            owner = _owner(mod_name, resolved)
            loader = _is_bepinex_package(resolved, staged)
            loader_present = loader_present or loader
            for source in sorted(staged.rglob("*")):
                if not source.is_file():
                    continue
                relative = PurePosixPath(source.relative_to(staged).as_posix())
                if loader:
                    destination = relative
                    if relative.name.casefold() in _PACKAGE_METADATA and len(relative.parts) == 1:
                        continue
                else:
                    destination = _route_file(relative, owner)
                    if destination is None:
                        continue
                entries.append(
                    InstallationPlanEntry(
                        staged_file=source,
                        destination=destination,
                        owner=owner,
                        mod_name=mod_name,
                        metadata={"source": relative.as_posix(), "loader": loader},
                    )
                )
        if not self.has_bepinex(game_root) and not loader_present:
            raise GameAdapterError(
                "BepInEx does not appear to be installed for this Valheim installation."
            )
        if not entries:
            raise GameAdapterError("The packages contain no installable runtime files")
        return InstallationPlan(self.game_id, game_root, tuple(entries))
