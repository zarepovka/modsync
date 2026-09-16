"""Loading and validation for ``modpack.json`` files."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .exceptions import ConfigError
from .models import Mod, Modpack

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def mod_directory_name(name: str) -> str:
    """Return a portable, path-safe directory name for a mod."""
    result = _SAFE_NAME_RE.sub("-", name.strip()).strip(". -")
    if not result:
        raise ConfigError(f"Mod name {name!r} cannot be converted to a safe directory name")
    if result.upper() in _WINDOWS_RESERVED:
        result = f"mod-{result}"
    return result


def _required_string(data: dict[str, Any], key: str, context: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{context}.{key} must be a non-empty string")
    return value.strip()


def _parse_mod(value: object, index: int) -> Mod:
    context = f"mods[{index}]"
    if not isinstance(value, dict):
        raise ConfigError(f"{context} must be an object")

    name = _required_string(value, "name", context)
    version = _required_string(value, "version", context)
    url = _required_string(value, "url", context)
    parsed_url = urlsplit(url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise ConfigError(f"{context}.url must be a valid HTTP or HTTPS URL")

    checksum = value.get("sha256")
    if checksum is not None:
        if not isinstance(checksum, str) or not _SHA256_RE.fullmatch(checksum):
            raise ConfigError(f"{context}.sha256 must be null or a 64-character hexadecimal digest")
        checksum = checksum.lower()

    enabled = value.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError(f"{context}.enabled must be true or false")

    mod_directory_name(name)
    return Mod(name=name, version=version, url=url, sha256=checksum, enabled=enabled)


def load_modpack(path: str | Path) -> Modpack:
    """Read, validate, and normalize a modpack configuration."""
    source = Path(path).expanduser().resolve()
    try:
        raw = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Cannot read modpack file {source}: {exc}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"Invalid JSON in {source.name} at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc

    if not isinstance(data, dict):
        raise ConfigError("The modpack root must be a JSON object")

    name = _required_string(data, "name", "modpack")
    version = _required_string(data, "version", "modpack")
    game = _required_string(data, "game", "modpack")
    install_value = _required_string(data, "install_directory", "modpack")
    description = data.get("description", "")
    if not isinstance(description, str):
        raise ConfigError("modpack.description must be a string")

    raw_mods = data.get("mods")
    if not isinstance(raw_mods, list):
        raise ConfigError("modpack.mods must be an array")
    mods = tuple(_parse_mod(item, index) for index, item in enumerate(raw_mods))

    names = [mod.name.casefold() for mod in mods]
    if len(names) != len(set(names)):
        raise ConfigError("Mod names must be unique (case-insensitive)")
    directories = [mod_directory_name(mod.name).casefold() for mod in mods]
    if len(directories) != len(set(directories)):
        raise ConfigError("Mod names resolve to duplicate installation directories")

    install_directory = Path(install_value).expanduser()
    if not install_directory.is_absolute():
        install_directory = source.parent / install_directory
    install_directory = install_directory.resolve()

    return Modpack(
        name=name,
        version=version,
        description=description.strip(),
        game=game,
        install_directory=install_directory,
        mods=mods,
        source_path=source,
    )
