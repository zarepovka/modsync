"""Cross-platform profile storage, validation, and locking."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import sys
import tempfile
from contextlib import AbstractContextManager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

from .config import load_modpack
from .exceptions import (
    ProfileError,
    ProfileExistsError,
    ProfileLockError,
    ProfileNotFoundError,
)
from .models import Modpack, Profile
from .state import atomic_write_bytes, atomic_write_json, empty_state

CONFIG_SCHEMA_VERSION = 1
PROFILE_SCHEMA_VERSION = 1
_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def default_data_directory() -> Path:
    """Return the native per-user data location for ModSync."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "ModSync"
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "ModSync"
        return Path.home() / "AppData" / "Local" / "ModSync"
    base = os.environ.get("XDG_DATA_HOME")
    if base:
        return Path(base) / "modsync"
    return Path.home() / ".local" / "share" / "modsync"


def validate_profile_name(name: object) -> str:
    """Validate a profile name before it is used as a filesystem component."""
    if not isinstance(name, str) or not _PROFILE_NAME_RE.fullmatch(name):
        raise ProfileError(
            "Profile name must be 1-64 characters and contain only ASCII letters, "
            "numbers, dots, underscores, or hyphens"
        )
    windows_stem = name.split(".", 1)[0].upper()
    if name in {".", ".."} or name.endswith(".") or windows_stem in _WINDOWS_RESERVED:
        raise ProfileError(f"Unsafe profile name: {name}")
    return name


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _parse_utc(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ProfileError(f"Invalid {label} in profile metadata")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProfileError(f"Invalid {label} in profile metadata") from exc
    if parsed.tzinfo is None:
        raise ProfileError(f"Invalid {label} in profile metadata")
    return value


class ProfileLock(AbstractContextManager["ProfileLock"]):
    """A non-blocking operating-system lock released automatically on exit."""

    def __init__(self, path: Path, profile_name: str) -> None:
        self.path = path
        self.profile_name = profile_name
        self._stream: BinaryIO | None = None

    def __enter__(self) -> "ProfileLock":
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.is_symlink():
                raise ProfileLockError(f"Unsafe lock file for profile {self.profile_name}")
            stream = self.path.open("a+b")
            if stream.seek(0, os.SEEK_END) == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            self._acquire(stream)
            self._stream = stream
            return self
        except ProfileLockError:
            raise
        except (OSError, ImportError) as exc:
            raise ProfileLockError(
                f"Profile {self.profile_name!r} is busy or cannot be locked"
            ) from exc

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._stream is None:
            return
        try:
            self._release(self._stream)
        finally:
            self._stream.close()
            self._stream = None

    def _acquire(self, stream: BinaryIO) -> None:
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                stream.close()
                raise ProfileLockError(
                    f"Profile {self.profile_name!r} is already being modified"
                ) from exc
        else:
            import fcntl

            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                stream.close()
                raise ProfileLockError(
                    f"Profile {self.profile_name!r} is already being modified"
                ) from exc

    @staticmethod
    def _release(stream: BinaryIO) -> None:
        if os.name == "nt":
            import msvcrt

            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class ProfileStore:
    """Persist independent profile configuration beneath one per-user directory."""

    def __init__(self, data_directory: Path | None = None) -> None:
        root = (data_directory or default_data_directory()).expanduser()
        self.root = root.absolute()
        self.profiles_directory = self.root / "profiles"
        self.locks_directory = self.root / "locks"
        self.config_path = self.root / "config.json"

    def create(self, name: str, modpack_path: Path) -> Profile:
        name = validate_profile_name(name)
        modpack = load_modpack(modpack_path)
        try:
            source_bytes = modpack.source_path.read_bytes()
        except OSError as exc:
            raise ProfileError(f"Cannot copy modpack into profile storage: {exc}") from exc
        with self.global_lock():
            self._prepare_root()
            if self.config_path.exists():
                self._load_config()
            else:
                self._save_config(None)
            if self._find_profile_name(name) is not None:
                raise ProfileExistsError(f"Profile already exists: {name}")
            try:
                temporary = Path(
                    tempfile.mkdtemp(prefix=".creating-", dir=self.profiles_directory)
                )
            except OSError as exc:
                raise ProfileError(f"Cannot prepare new profile storage: {exc}") from exc
            try:
                created = _utc_now()
                metadata = {
                    "schema_version": PROFILE_SCHEMA_VERSION,
                    "name": name,
                    "game": modpack.game,
                    "install_directory": str(modpack.install_directory),
                    "created_at": created,
                    "updated_at": created,
                    "modpack_source": str(modpack.source_path),
                    "mod_count": len(modpack.mods),
                }
                atomic_write_bytes(temporary / "modpack.json", source_bytes)
                atomic_write_json(temporary / "state.json", empty_state())
                (temporary / "backups").mkdir()
                atomic_write_json(temporary / "profile.json", metadata)
                temporary.replace(self.profiles_directory / name)
            except OSError as exc:
                shutil.rmtree(temporary, ignore_errors=True)
                raise ProfileError(f"Cannot create profile {name}: {exc}") from exc
            except Exception:
                shutil.rmtree(temporary, ignore_errors=True)
                raise
        return self.get(name)

    def list(self) -> list[Profile]:
        if not self.profiles_directory.exists():
            return []
        self._ensure_safe_directory(self.profiles_directory, "profile storage")
        profiles: list[Profile] = []
        try:
            children = sorted(
                self.profiles_directory.iterdir(), key=lambda path: path.name.casefold()
            )
        except OSError as exc:
            raise ProfileError(f"Cannot list profiles: {exc}") from exc
        for child in children:
            if child.name.startswith(".creating-"):
                continue
            if child.is_symlink() or not child.is_dir():
                raise ProfileError(f"Unsafe profile storage entry: {child.name}")
            profiles.append(self._load_profile(child))
        return profiles

    def get(self, name: str) -> Profile:
        name = validate_profile_name(name)
        actual = self._find_profile_name(name)
        if actual is None:
            raise ProfileNotFoundError(f"Profile not found: {name}")
        directory = self.profiles_directory / actual
        if directory.is_symlink() or not directory.is_dir():
            raise ProfileError(f"Unsafe profile directory: {actual}")
        return self._load_profile(directory)

    def load_modpack(self, name: str) -> Modpack:
        profile = self.get(name)
        stored = load_modpack(profile.directory / "modpack.json")
        if stored.game != profile.game or len(stored.mods) != profile.mod_count:
            raise ProfileError(f"Stored modpack does not match profile metadata: {profile.name}")
        return replace(
            stored,
            install_directory=profile.install_directory,
            state_path=profile.directory / "state.json",
            backup_directory=profile.directory / "backups",
        )

    def active_name(self) -> str | None:
        config = self._load_config()
        active = config["active_profile"]
        if active is not None and self._find_profile_name(active) is None:
            raise ProfileError(f"Active profile no longer exists: {active}")
        return active

    def shared_install_profiles(self, name: str) -> list[str]:
        """List other profiles that point at the same physical installation directory."""
        profile = self.get(name)
        target = profile.install_directory.resolve()
        return [
            other.name
            for other in self.list()
            if other.name != profile.name and other.install_directory.resolve() == target
        ]

    def activate(self, name: str) -> Profile:
        with self.global_lock():
            profile = self.get(name)
            self._save_config(profile.name)
            return profile

    def delete(self, name: str) -> Profile:
        name = validate_profile_name(name)
        with self.global_lock(), self.lock(name):
            profile = self.get(name)
            resolved = profile.directory.resolve()
            unsafe_parent = resolved.parent != self.profiles_directory.resolve()
            if unsafe_parent or profile.directory.is_symlink():
                raise ProfileError(f"Refusing to remove unsafe profile directory: {name}")
            try:
                shutil.rmtree(profile.directory)
            except OSError as exc:
                raise ProfileError(f"Cannot delete profile {profile.name}: {exc}") from exc
            config = self._load_config()
            if config["active_profile"] == profile.name:
                self._save_config(None)
            return profile

    def touch(self, name: str) -> None:
        profile = self.get(name)
        metadata = self._read_json(profile.directory / "profile.json", "profile metadata")
        metadata["updated_at"] = _utc_now()
        try:
            atomic_write_json(profile.directory / "profile.json", metadata)
        except OSError as exc:
            raise ProfileError(f"Cannot update profile metadata: {exc}") from exc

    def lock(self, name: str) -> ProfileLock:
        name = validate_profile_name(name)
        actual = self._find_profile_name(name) or name
        return ProfileLock(self.locks_directory / f"{actual}.lock", actual)

    def global_lock(self) -> ProfileLock:
        return ProfileLock(self.locks_directory / "global.lock", "profile storage")

    def _prepare_root(self) -> None:
        if self.root.is_symlink():
            raise ProfileError("Profile data directory must not be a symbolic link")
        try:
            self.profiles_directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ProfileError(f"Cannot prepare profile storage: {exc}") from exc
        self._ensure_safe_directory(self.profiles_directory, "profile storage")

    @staticmethod
    def _ensure_safe_directory(path: Path, label: str) -> None:
        try:
            details = path.lstat()
        except OSError as exc:
            raise ProfileError(f"Cannot read {label}: {exc}") from exc
        if path.is_symlink() or not stat.S_ISDIR(details.st_mode):
            raise ProfileError(f"{label.capitalize()} is not a safe directory")

    def _find_profile_name(self, name: str) -> str | None:
        if not self.profiles_directory.exists():
            return None
        folded = name.casefold()
        try:
            for child in self.profiles_directory.iterdir():
                if child.name.casefold() == folded:
                    return child.name
        except OSError as exc:
            raise ProfileError(f"Cannot inspect profile storage: {exc}") from exc
        return None

    def _load_profile(self, directory: Path) -> Profile:
        metadata = self._read_json(directory / "profile.json", "profile metadata")
        required_strings = (
            "name",
            "game",
            "install_directory",
            "created_at",
            "updated_at",
            "modpack_source",
        )
        if metadata.get("schema_version") != PROFILE_SCHEMA_VERSION or any(
            not isinstance(metadata.get(key), str) or not metadata[key]
            for key in required_strings
        ):
            raise ProfileError(f"Invalid profile metadata: {directory.name}")
        name = validate_profile_name(metadata["name"])
        if name != directory.name:
            raise ProfileError(f"Profile name does not match its directory: {directory.name}")
        _parse_utc(metadata["created_at"], "created_at")
        _parse_utc(metadata["updated_at"], "updated_at")
        mod_count = metadata.get("mod_count")
        if not isinstance(mod_count, int) or isinstance(mod_count, bool) or mod_count < 0:
            raise ProfileError(f"Invalid mod_count in profile metadata: {name}")
        install_directory = Path(metadata["install_directory"]).expanduser()
        source = Path(metadata["modpack_source"]).expanduser()
        if not install_directory.is_absolute() or not source.is_absolute():
            raise ProfileError(f"Profile paths must be absolute: {name}")
        for required in ("profile.json", "modpack.json", "state.json"):
            candidate = directory / required
            if candidate.is_symlink() or not candidate.is_file():
                raise ProfileError(f"Missing or unsafe {required} for profile {name}")
        backups = directory / "backups"
        if backups.is_symlink() or not backups.is_dir():
            raise ProfileError(f"Missing or unsafe backups directory for profile {name}")
        return Profile(
            name=name,
            game=metadata["game"],
            install_directory=install_directory.resolve(),
            created_at=metadata["created_at"],
            updated_at=metadata["updated_at"],
            modpack_source=source.resolve(),
            mod_count=mod_count,
            directory=directory,
        )

    def _load_config(self) -> dict[str, Any]:
        if not self.config_path.exists():
            return {"schema_version": CONFIG_SCHEMA_VERSION, "active_profile": None}
        config = self._read_json(self.config_path, "global profile configuration")
        active = config.get("active_profile")
        if config.get("schema_version") != CONFIG_SCHEMA_VERSION or (
            active is not None and not isinstance(active, str)
        ):
            raise ProfileError("Invalid global profile configuration")
        if active is not None:
            validate_profile_name(active)
        return config

    def _save_config(self, active_profile: str | None) -> None:
        self._prepare_root()
        try:
            atomic_write_json(
                self.config_path,
                {"schema_version": CONFIG_SCHEMA_VERSION, "active_profile": active_profile},
            )
        except OSError as exc:
            raise ProfileError(f"Cannot save global profile configuration: {exc}") from exc

    @staticmethod
    def _read_json(path: Path, label: str) -> dict[str, Any]:
        if path.is_symlink():
            raise ProfileError(f"Unsafe {label}: {path}")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProfileError(f"Cannot read {label} {path}: {exc}") from exc
        if not isinstance(value, dict):
            raise ProfileError(f"Invalid {label}: {path}")
        return value
