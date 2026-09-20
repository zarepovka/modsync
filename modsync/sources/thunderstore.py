"""Thunderstore package source with recursive, exact-version dependencies."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import requests

from .. import __version__
from ..exceptions import (
    DependencyConflictError,
    DependencyCycleError,
    ManifestError,
    SourceError,
    SourceNotFoundError,
    SourceRateLimitError,
)
from ..models import Mod, ResolvedMod

API_ROOT = "https://thunderstore.io/api/experimental/package"
_COMMUNITY_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,254}[a-z0-9])?$")
_NAMESPACE_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_]{0,62}[A-Za-z0-9])?$")
_DEPENDENCY_NAMESPACE_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9_-]{0,126}[A-Za-z0-9])?$"
)
_PACKAGE_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_]{0,126}[A-Za-z0-9])?$")
_VERSION_RE = re.compile(
    r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$"
)
_ORIGIN_DOWNLOAD_RE = re.compile(
    r"^/package/download/[A-Za-z0-9_-]+/[A-Za-z0-9_]+/[0-9]+\.[0-9]+\.[0-9]+/$"
)
_CDN_DOWNLOAD_RE = re.compile(
    r"^/live/repository/packages/[A-Za-z0-9_-]+-[A-Za-z0-9_]+-"
    r"[0-9]+\.[0-9]+\.[0-9]+\.zip$"
)
_DOWNLOAD_HOSTS = {"thunderstore.io", "gcdn.thunderstore.io", "cdn.thunderstore.io"}
_MAX_MANIFEST_BYTES = 1024 * 1024


def validate_community(value: str) -> str:
    if not _COMMUNITY_RE.fullmatch(value):
        raise SourceError("Thunderstore community is not a safe identifier")
    return value


def validate_namespace(value: str, *, dependency: bool = False) -> str:
    pattern = _DEPENDENCY_NAMESPACE_RE if dependency else _NAMESPACE_RE
    if not pattern.fullmatch(value):
        raise SourceError("Thunderstore namespace is invalid or unsafe")
    return value


def validate_package_name(value: str) -> str:
    if not _PACKAGE_RE.fullmatch(value):
        raise SourceError("Thunderstore package name is invalid or unsafe")
    return value


def validate_version(value: str, *, allow_latest: bool = True) -> str:
    if allow_latest and value == "latest":
        return value
    if not _VERSION_RE.fullmatch(value):
        raise SourceError("Thunderstore version must use Major.Minor.Patch")
    return value


def parse_dependency(value: object) -> tuple[str, str, str]:
    """Parse the official Namespace-Package-Major.Minor.Patch reference."""
    if not isinstance(value, str) or any(ord(character) < 32 for character in value):
        raise SourceError("Thunderstore dependency must be a safe string")
    parts = value.rsplit("-", 2)
    if len(parts) != 3:
        raise SourceError(f"Invalid Thunderstore dependency: {value!r}")
    namespace, package, version = parts
    validate_namespace(namespace, dependency=True)
    validate_package_name(package)
    validate_version(version, allow_latest=False)
    return namespace, package, version


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _provider_key(community: str, namespace: str, package: str) -> str:
    return (
        f"thunderstore:{community.casefold()}:{namespace.casefold()}:{package.casefold()}"
    )


def _validate_download_url(value: object) -> str:
    if not isinstance(value, str):
        raise SourceError("Thunderstore package has no valid download URL")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _DOWNLOAD_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or parsed.query
        or parsed.fragment
    ):
        raise SourceError("Thunderstore package has an unsafe download URL")
    if parsed.hostname == "thunderstore.io":
        valid_path = _ORIGIN_DOWNLOAD_RE.fullmatch(parsed.path)
    else:
        valid_path = _CDN_DOWNLOAD_RE.fullmatch(parsed.path)
    if not valid_path:
        raise SourceError("Thunderstore package has an unexpected download URL")
    return value


class ThunderstoreSource:
    """Resolve Thunderstore packages and their exact recursive dependencies."""

    def __init__(
        self,
        session: requests.Session | None = None,
        *,
        sleeper: Callable[[float], None] | None = None,
        attempts: int = 3,
    ) -> None:
        self.session = session or requests.Session()
        self.sleeper = sleeper or time.sleep
        self.attempts = max(1, attempts)
        self._response_cache: dict[str, dict[str, Any]] = {}

    def resolve(self, mod: Mod) -> ResolvedMod:
        if mod.source is None:
            raise SourceError(f"Thunderstore source configuration is missing for {mod.name}")
        options = mod.source.options
        community = validate_community(options["community"])
        namespace = validate_namespace(options["namespace"])
        package = validate_package_name(options["package"])
        requested = validate_version(options["version"])
        constraints: dict[str, str] = {}
        memo: dict[tuple[str, str], ResolvedMod] = {}
        return self._resolve_node(
            community,
            namespace,
            package,
            requested,
            display_name=mod.name,
            checksum=mod.sha256,
            constraints=constraints,
            memo=memo,
            stack=(),
            labels=(),
        )

    def _resolve_node(
        self,
        community: str,
        namespace: str,
        package: str,
        requested: str,
        *,
        display_name: str,
        checksum: str | None,
        constraints: dict[str, str],
        memo: dict[tuple[str, str], ResolvedMod],
        stack: tuple[str, ...],
        labels: tuple[str, ...],
    ) -> ResolvedMod:
        package_data = self._package_metadata(community, namespace, package)
        if requested == "latest":
            version_data = package_data.get("latest")
            if not isinstance(version_data, dict):
                raise SourceError(
                    f"Thunderstore returned malformed latest metadata for {namespace}/{package}"
                )
        else:
            version_data = self._version_metadata(namespace, package, requested)

        version = self._validate_version_metadata(version_data, namespace, package)
        if requested != "latest" and version != requested:
            raise SourceNotFoundError(
                f"Thunderstore package version {requested} was not found."
            )
        key = _provider_key(community, namespace, package)
        previous = constraints.get(key)
        if previous is not None and previous != version:
            raise DependencyConflictError(
                "Dependency conflict:\n\n"
                f"{package} is required as both {previous} and {version}."
            )
        constraints[key] = version
        if key in stack:
            start = stack.index(key)
            chain = (*labels[start:], f"{namespace}-{package}")
            raise DependencyCycleError(
                f"Circular dependency detected: {' -> '.join(chain)}"
            )
        memo_key = (key, version)
        if memo_key in memo:
            return memo[memo_key]

        raw_dependencies = version_data.get("dependencies")
        if not isinstance(raw_dependencies, list):
            raise SourceError(
                f"Thunderstore returned malformed dependencies for {namespace}/{package}"
            )
        dependency_refs: list[str] = []
        dependencies: list[ResolvedMod] = []
        next_stack = (*stack, key)
        next_labels = (*labels, f"{namespace}-{package}")
        for raw_dependency in raw_dependencies:
            dependency_namespace, dependency_package, dependency_version = parse_dependency(
                raw_dependency
            )
            dependency_refs.append(raw_dependency)
            dependencies.append(
                self._resolve_node(
                    community,
                    dependency_namespace,
                    dependency_package,
                    dependency_version,
                    display_name=f"{dependency_namespace}-{dependency_package}",
                    checksum=None,
                    constraints=constraints,
                    memo=memo,
                    stack=next_stack,
                    labels=next_labels,
                )
            )

        download_url = _validate_download_url(version_data.get("download_url"))
        deprecated = package_data.get("is_deprecated")
        if not isinstance(deprecated, bool):
            raise SourceError(
                f"Thunderstore returned malformed package metadata for {namespace}/{package}"
            )
        warnings = ()
        if requested == "latest" and deprecated:
            warnings = (
                f"Thunderstore package {namespace}-{package} is deprecated; "
                "no automatic replacement was selected.",
            )
        resolved_at = _utc_now()
        filename = f"{namespace}-{package}-{version}.zip"
        resolved = ResolvedMod(
            name=display_name,
            version=version,
            download_url=download_url,
            filename=filename,
            sha256=checksum,
            source_metadata={
                "type": "thunderstore",
                "community": community,
                "namespace": namespace,
                "package": package,
                "requested_version": requested,
                "version": version,
                "download_url": download_url,
                "resolved_at": resolved_at,
            },
            release_metadata={
                "dependencies": dependency_refs,
                "deprecated": deprecated,
                "date_created": version_data.get("date_created"),
            },
            source_identity={
                "type": "thunderstore",
                "community": community,
                "namespace": namespace,
                "package": package,
                "version": version,
            },
            dependencies=tuple(dependencies),
            warnings=warnings,
            provider_key=key,
        )
        memo[memo_key] = resolved
        return resolved

    def _package_metadata(
        self, community: str, namespace: str, package: str
    ) -> dict[str, Any]:
        url = (
            f"{API_ROOT}/{quote(namespace, safe='')}/{quote(package, safe='')}/"
        )
        data = self._request_json(
            url, f"Thunderstore package {namespace}/{package} was not found."
        )
        if data.get("namespace") != namespace or data.get("name") != package:
            raise SourceError(
                f"Thunderstore returned mismatched package metadata for {namespace}/{package}"
            )
        listings = data.get("community_listings")
        if not isinstance(listings, list) or any(
            not isinstance(listing, dict) for listing in listings
        ):
            raise SourceError(
                f"Thunderstore returned malformed community metadata for {namespace}/{package}"
            )
        if not any(listing.get("community") == community for listing in listings):
            raise SourceNotFoundError(
                f"Thunderstore community {community!r} was not found for "
                f"package {namespace}/{package}."
            )
        return data

    def _version_metadata(
        self, namespace: str, package: str, version: str
    ) -> dict[str, Any]:
        url = (
            f"{API_ROOT}/{quote(namespace, safe='')}/{quote(package, safe='')}/"
            f"{quote(version, safe='')}/"
        )
        return self._request_json(
            url, f"Thunderstore package version {version} was not found."
        )

    @staticmethod
    def _validate_version_metadata(
        data: dict[str, Any], namespace: str, package: str
    ) -> str:
        version = data.get("version_number")
        if (
            data.get("namespace") != namespace
            or data.get("name") != package
            or not isinstance(version, str)
            or not _VERSION_RE.fullmatch(version)
            or data.get("is_active") is not True
        ):
            raise SourceError(
                f"Thunderstore returned malformed version metadata for {namespace}/{package}"
            )
        return version

    def _request_json(self, url: str, not_found_message: str) -> dict[str, Any]:
        cached = self._response_cache.get(url)
        if cached is not None:
            return cached
        for attempt in range(self.attempts):
            response = None
            try:
                response = self.session.get(
                    url,
                    timeout=(10, 30),
                    allow_redirects=True,
                    headers={
                        "Accept": "application/json",
                        "User-Agent": f"ModSync/{__version__}",
                    },
                )
                final = urlsplit(getattr(response, "url", url))
                if final.scheme != "https" or final.hostname != "thunderstore.io":
                    raise SourceError("Thunderstore API returned an unsafe redirect")
                if response.status_code == 404:
                    raise SourceNotFoundError(not_found_message)
                if response.status_code == 429 and attempt + 1 < self.attempts:
                    self.sleeper(float(2**attempt))
                    continue
                if response.status_code in {403, 429}:
                    raise SourceRateLimitError(
                        "Thunderstore API rate limit reached; try again later"
                    )
                if 500 <= response.status_code < 600 and attempt + 1 < self.attempts:
                    self.sleeper(float(2**attempt))
                    continue
                response.raise_for_status()
                value = response.json()
                if not isinstance(value, dict):
                    raise SourceError("Thunderstore API returned malformed JSON data")
                self._response_cache[url] = value
                return value
            except SourceError:
                raise
            except (requests.RequestException, ValueError) as exc:
                if attempt + 1 < self.attempts:
                    self.sleeper(float(2**attempt))
                    continue
                raise SourceError(f"Could not query Thunderstore API: {exc}") from exc
            finally:
                if response is not None:
                    response.close()
        raise SourceError("Could not query Thunderstore API")

    def validate_staged(self, resolved: ResolvedMod, directory: Path) -> None:
        """Validate the root manifest against already-resolved API metadata."""
        manifest_path = directory / "manifest.json"
        try:
            details = manifest_path.lstat()
            if manifest_path.is_symlink() or not manifest_path.is_file():
                raise ManifestError("Thunderstore manifest.json is not a regular file")
            if details.st_size > _MAX_MANIFEST_BYTES:
                raise ManifestError("Thunderstore manifest.json exceeds the 1 MiB limit")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except ManifestError:
            raise
        except (OSError, json.JSONDecodeError) as exc:
            raise ManifestError(f"Cannot read Thunderstore manifest.json: {exc}") from exc
        if not isinstance(manifest, dict):
            raise ManifestError("Thunderstore manifest.json must contain an object")
        package = resolved.source_metadata["package"]
        if manifest.get("name") != package:
            raise ManifestError(
                f"Thunderstore manifest name mismatch: expected {package}"
            )
        if manifest.get("version_number") != resolved.version:
            raise ManifestError(
                "Thunderstore manifest/API version mismatch: "
                f"expected {resolved.version}, found {manifest.get('version_number', 'unknown')}"
            )
        raw_dependencies = manifest.get("dependencies")
        if not isinstance(raw_dependencies, list):
            raise ManifestError("Thunderstore manifest dependencies must be an array")
        try:
            dependencies = [
                "-".join(parse_dependency(dependency)) for dependency in raw_dependencies
            ]
        except SourceError as exc:
            raise ManifestError(f"Invalid Thunderstore manifest dependency: {exc}") from exc
        expected = resolved.release_metadata.get("dependencies")
        if (
            not isinstance(expected, list)
            or len(dependencies) != len(set(dependencies))
            or sorted(dependencies) != sorted(expected)
        ):
            raise ManifestError(
                "Thunderstore manifest dependencies do not match API metadata"
            )
