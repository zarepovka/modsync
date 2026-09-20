"""Read-only Steam library discovery for games known to ModSync."""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from ..exceptions import DiscoveryMetadataError, DiscoveryProviderError, GameAdapterError
from ..games.base import GameAdapter
from ..models import GameInstallation
from .base import (
    DiscoveryContext,
    GameDiscoveryProvider,
    canonical_path_key,
    platform_name,
)
from .vdf import get_key, parse_keyvalues

_MAX_METADATA_BYTES = 4 * 1024 * 1024


def _windows_registry_roots() -> tuple[Path, ...]:
    if os.name != "nt":
        return ()
    try:
        import winreg
    except ImportError:
        return ()
    queries = (
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
    )
    roots: list[Path] = []
    for hive, key_name, value_name in queries:
        try:
            with winreg.OpenKey(hive, key_name) as key:
                value, _ = winreg.QueryValueEx(key, value_name)
            if isinstance(value, str) and value.strip():
                roots.append(Path(value))
        except OSError:
            continue
    return tuple(roots)


def _safe_read(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError(path)
    try:
        size = path.stat().st_size
        if size > _MAX_METADATA_BYTES:
            raise DiscoveryMetadataError(f"Steam metadata is too large: {path}")
        return path.read_text(encoding="utf-8-sig")
    except UnicodeError as exc:
        raise DiscoveryMetadataError(f"Steam metadata is not valid UTF-8: {path}") from exc
    except OSError as exc:
        raise DiscoveryMetadataError(f"Cannot read Steam metadata {path}: {exc}") from exc


class SteamDiscoveryProvider(GameDiscoveryProvider):
    provider_id = "steam"

    def __init__(self, context: DiscoveryContext | None = None) -> None:
        self.context = context or DiscoveryContext()
        self.platform = platform_name(self.context.platform)
        self.steam_found = False

    def _root_candidates(self) -> list[Path]:
        if self.context.steam_roots is not None:
            return list(self.context.steam_roots)
        home = self.context.home.expanduser()
        if self.platform == "windows":
            reader = self.context.registry_reader or _windows_registry_roots
            try:
                roots = list(reader())
            except OSError:
                roots = []
            for variable in ("PROGRAMFILES(X86)", "PROGRAMFILES"):
                value = self.context.environ.get(variable)
                if value:
                    roots.append(Path(value) / "Steam")
            roots.extend((Path("C:/Program Files (x86)/Steam"), Path("C:/Program Files/Steam")))
            return roots
        if self.platform == "macos":
            return [home / "Library" / "Application Support" / "Steam"]
        if self.platform == "linux":
            return [
                home / ".local" / "share" / "Steam",
                home / ".steam" / "steam",
                home
                / ".var"
                / "app"
                / "com.valvesoftware.Steam"
                / ".local"
                / "share"
                / "Steam",
                home / ".var" / "app" / "com.valvesoftware.Steam" / "data" / "Steam",
            ]
        raise DiscoveryProviderError(f"Steam discovery is unsupported on {self.platform}")

    def _metadata_path(self, raw: str) -> Path | None:
        if not raw or any(ord(char) < 32 for char in raw):
            return None
        pure = PureWindowsPath(raw) if self.platform == "windows" else PurePosixPath(raw)
        if not pure.is_absolute() or ".." in pure.parts:
            return None
        try:
            return self.context.path_resolver(raw).expanduser().resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            return None

    def _libraries(self, root: Path) -> tuple[list[Path], list[str]]:
        libraries = [root.resolve(strict=False)]
        errors: list[str] = []
        metadata = root / "steamapps" / "libraryfolders.vdf"
        if not metadata.exists():
            return libraries, errors
        try:
            parsed = parse_keyvalues(_safe_read(metadata))
            folder_data = get_key(parsed, "libraryfolders")
            if not isinstance(folder_data, dict):
                raise DiscoveryMetadataError(f"Missing libraryfolders object in {metadata}")
            for index, value in folder_data.items():
                if not index.isdecimal():
                    continue
                raw_path = get_key(value, "path") if isinstance(value, dict) else value
                if not isinstance(raw_path, str):
                    continue
                candidate = self._metadata_path(raw_path)
                if candidate is not None and candidate.is_dir():
                    libraries.append(candidate)
        except (DiscoveryMetadataError, FileNotFoundError) as exc:
            errors.append(str(exc))
        return libraries, errors

    @staticmethod
    def _safe_install_dir(value: Any) -> str | None:
        if not isinstance(value, str) or not value or any(ord(char) < 32 for char in value):
            return None
        if value in {".", ".."} or "/" in value or "\\" in value or ":" in value:
            return None
        return value

    def discover(self, adapter: GameAdapter) -> list[GameInstallation]:
        app_id = getattr(adapter, "steam_app_id", None)
        if not isinstance(app_id, int) or isinstance(app_id, bool) or app_id <= 0:
            return []
        roots: list[Path] = []
        seen_roots: set[str] = set()
        for candidate in self._root_candidates():
            try:
                canonical = candidate.expanduser().resolve(strict=False)
            except (OSError, RuntimeError):
                continue
            identity = canonical_path_key(canonical, self.platform)
            if identity not in seen_roots and canonical.is_dir():
                seen_roots.add(identity)
                roots.append(canonical)
        self.steam_found = bool(roots)
        if not roots:
            return []

        libraries: list[Path] = []
        errors: list[str] = []
        seen_libraries: set[str] = set()
        for root in roots:
            found, found_errors = self._libraries(root)
            errors.extend(found_errors)
            for library in found:
                identity = canonical_path_key(library, self.platform)
                if identity not in seen_libraries:
                    seen_libraries.add(identity)
                    libraries.append(library)

        installations: list[GameInstallation] = []
        seen_installations: set[str] = set()
        for library in libraries:
            manifest = library / "steamapps" / f"appmanifest_{app_id}.acf"
            if not manifest.exists():
                continue
            try:
                parsed = parse_keyvalues(_safe_read(manifest))
                state = get_key(parsed, "AppState")
                if not isinstance(state, dict):
                    raise DiscoveryMetadataError(f"Missing AppState object in {manifest}")
                parsed_app_id = get_key(state, "appid")
                if str(parsed_app_id) != str(app_id):
                    raise DiscoveryMetadataError(f"Unexpected app ID in {manifest}")
                install_dir = self._safe_install_dir(get_key(state, "installdir"))
                if install_dir is None:
                    raise DiscoveryMetadataError(f"Unsafe installdir in {manifest}")
                common = library / "steamapps" / "common"
                lexical = common / install_dir
                if lexical.parent != common:
                    raise DiscoveryMetadataError(f"Unsafe install path in {manifest}")
                install_path = lexical.resolve(strict=False)
            except (DiscoveryMetadataError, FileNotFoundError) as exc:
                errors.append(str(exc))
                continue

            identity = canonical_path_key(install_path, self.platform)
            if identity in seen_installations:
                continue
            seen_installations.add(identity)
            validation_error: str | None = None
            try:
                adapter.validate_game(install_path)
                validated = True
            except (GameAdapterError, OSError) as exc:
                validated = False
                validation_error = str(exc)
            metadata: dict[str, Any] = {
                "manifest_path": str(manifest.resolve(strict=False)),
                "steam_root": str(root_for_library(library, roots)),
            }
            if validation_error:
                metadata["validation_error"] = validation_error
            installations.append(
                GameInstallation(
                    game_id=adapter.game_id,
                    display_name=adapter.display_name,
                    provider=self.provider_id,
                    install_path=install_path,
                    app_id=app_id,
                    platform=self.platform,
                    library_path=library,
                    validated=validated,
                    metadata=metadata,
                )
            )
        if not installations and errors:
            raise DiscoveryMetadataError(
                "Steam metadata could not be used safely: " + "; ".join(errors[:3])
            )
        return installations


def root_for_library(library: Path, roots: list[Path]) -> Path:
    """Choose a stable provenance root without assuming library containment."""
    library_key = str(library.resolve(strict=False))
    for root in roots:
        if str(root.resolve(strict=False)) == library_key:
            return root
    return roots[0]
