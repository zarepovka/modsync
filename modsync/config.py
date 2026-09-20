"""Loading and validation for ``modpack.json`` files."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .exceptions import ConfigError
from .models import Mod, Modpack, SourceSpec

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_THUNDERSTORE_COMMUNITY_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,254}[a-z0-9])?$")
_THUNDERSTORE_NAMESPACE_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_]{0,62}[A-Za-z0-9])?$")
_THUNDERSTORE_PACKAGE_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_]{0,126}[A-Za-z0-9])?$")
_THUNDERSTORE_VERSION_RE = re.compile(
    r"^(?:latest|(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*))$"
)
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
    source_value = value.get("source")
    version_value = value.get("version")
    if version_value is not None and (
        not isinstance(version_value, str) or not version_value.strip()
    ):
        raise ConfigError(f"{context}.version must be a non-empty string when provided")
    version = version_value.strip() if isinstance(version_value, str) else None
    url_value = value.get("url")
    if url_value is not None and (not isinstance(url_value, str) or not url_value.strip()):
        raise ConfigError(f"{context}.url must be a non-empty string when provided")
    url = url_value.strip() if isinstance(url_value, str) else None

    if source_value is None:
        if version is None:
            raise ConfigError(f"{context}.version is required for legacy direct URLs")
        if url is None:
            raise ConfigError(f"{context}.url or {context}.source is required")
        source = SourceSpec(type="direct", options={"url": url, "version": version})
    else:
        if not isinstance(source_value, dict):
            raise ConfigError(f"{context}.source must be an object")
        source_type = _required_string(source_value, "type", f"{context}.source").lower()
        if source_type == "direct":
            allowed = {"type", "url", "version"}
            unexpected = set(source_value) - allowed
            if unexpected:
                raise ConfigError(
                    f"{context}.source contains unsupported fields: {', '.join(sorted(unexpected))}"
                )
            source_url = _required_string(source_value, "url", f"{context}.source")
            source_version_value = source_value.get("version", version)
            if source_version_value is not None and (
                not isinstance(source_version_value, str) or not source_version_value.strip()
            ):
                raise ConfigError(f"{context}.source.version must be a non-empty string")
            source_version = (
                source_version_value.strip() if isinstance(source_version_value, str) else None
            )
            source_options = {"url": source_url}
            if source_version is not None:
                source_options["version"] = source_version
            source = SourceSpec(type="direct", options=source_options)
            url = source_url
            version = source_version
        elif source_type == "github":
            allowed = {"type", "repository", "release", "asset"}
            unexpected = set(source_value) - allowed
            if unexpected:
                raise ConfigError(
                    f"{context}.source contains unsupported fields: {', '.join(sorted(unexpected))}"
                )
            source = SourceSpec(
                type="github",
                options={
                    "repository": _required_string(
                        source_value, "repository", f"{context}.source"
                    ),
                    "release": _required_string(source_value, "release", f"{context}.source"),
                    "asset": _required_string(source_value, "asset", f"{context}.source"),
                },
            )
            url = None
        elif source_type == "thunderstore":
            allowed = {"type", "community", "namespace", "package", "version"}
            unexpected = set(source_value) - allowed
            if unexpected:
                raise ConfigError(
                    f"{context}.source contains unsupported fields: {', '.join(sorted(unexpected))}"
                )
            community = _required_string(source_value, "community", f"{context}.source")
            namespace = _required_string(source_value, "namespace", f"{context}.source")
            package = _required_string(source_value, "package", f"{context}.source")
            requested_version = _required_string(
                source_value, "version", f"{context}.source"
            )
            if not _THUNDERSTORE_COMMUNITY_RE.fullmatch(community):
                raise ConfigError(f"{context}.source.community is not a safe identifier")
            if not _THUNDERSTORE_NAMESPACE_RE.fullmatch(namespace):
                raise ConfigError(f"{context}.source.namespace is not a valid Thunderstore name")
            if not _THUNDERSTORE_PACKAGE_RE.fullmatch(package):
                raise ConfigError(f"{context}.source.package is not a valid Thunderstore name")
            if not _THUNDERSTORE_VERSION_RE.fullmatch(requested_version):
                raise ConfigError(
                    f"{context}.source.version must be latest or Major.Minor.Patch"
                )
            source = SourceSpec(
                type="thunderstore",
                options={
                    "community": community,
                    "namespace": namespace,
                    "package": package,
                    "version": requested_version,
                },
            )
            version = None if requested_version == "latest" else requested_version
            url = None
        else:
            raise ConfigError(f"{context}.source.type is unsupported: {source_type}")

    if url is not None:
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
    return Mod(
        name=name,
        version=version,
        url=url,
        sha256=checksum,
        enabled=enabled,
        source=source,
    )


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
