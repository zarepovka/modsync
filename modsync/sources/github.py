"""GitHub Releases source provider using the official REST API."""

from __future__ import annotations

import fnmatch
import os
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlsplit

import requests

from .. import __version__
from ..exceptions import (
    SourceAmbiguousError,
    SourceError,
    SourceNotFoundError,
    SourceRateLimitError,
)
from ..models import Mod, ResolvedMod

_REPOSITORY_PART = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,98}[A-Za-z0-9])?$")
_SEMVER_TAG = re.compile(r"^[vV](?=\d+(?:\.\d+){1,3}(?:[-+][0-9A-Za-z.-]+)?$)(.+)$")
_ALLOWED_ASSET_HOSTS = {
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
}
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
_TOKEN_FROM_ENV = object()


def validate_repository(value: str) -> str:
    """Return a safe owner/repository identifier."""
    if any(ord(character) < 32 for character in value) or value.count("/") != 1:
        raise SourceError("GitHub repository must use the owner/repository form")
    owner, repository = value.split("/", 1)
    if not _REPOSITORY_PART.fullmatch(owner) or not _REPOSITORY_PART.fullmatch(repository):
        raise SourceError("GitHub repository contains unsafe or invalid characters")
    if owner in {".", ".."} or repository in {".", ".."}:
        raise SourceError("GitHub repository contains an unsafe path component")
    return value


def normalize_tag(tag: str) -> str:
    """Normalize a conventional semver v-prefix while preserving arbitrary tags."""
    match = _SEMVER_TAG.fullmatch(tag)
    return match.group(1) if match else tag


def _safe_asset_name(value: object) -> str:
    if not isinstance(value, str) or not value or value in {".", ".."}:
        raise SourceError("GitHub release asset has an unsafe filename")
    if "/" in value or "\\" in value or any(ord(character) < 32 for character in value):
        raise SourceError("GitHub release asset has an unsafe filename")
    if (
        any(character in '<>:"|?*' for character in value)
        or value.endswith((" ", "."))
        or value.split(".", 1)[0].upper() in _WINDOWS_RESERVED
    ):
        raise SourceError("GitHub release asset filename is not portable")
    return value


def _validate_selector(value: str, label: str, *, asset: bool = False) -> str:
    if not value or any(ord(character) < 32 for character in value):
        raise SourceError(f"GitHub {label} contains unsafe characters")
    if asset and ("/" in value or "\\" in value):
        raise SourceError("GitHub asset selector must match a filename, not a path")
    return value


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class GitHubReleaseSource:
    """Resolve published GitHub release assets with bounded retries."""

    def __init__(
        self,
        session: requests.Session | None = None,
        *,
        token: str | None | object = _TOKEN_FROM_ENV,
        sleeper: Callable[[float], None] | None = None,
        attempts: int = 3,
    ) -> None:
        self.session = session or requests.Session()
        self.token = (
            os.environ.get("MODSYNC_GITHUB_TOKEN") if token is _TOKEN_FROM_ENV else token
        )
        self.sleeper = sleeper or time.sleep
        self.attempts = max(1, attempts)

    def resolve(self, mod: Mod) -> ResolvedMod:
        if mod.source is None:
            raise SourceError(f"GitHub source configuration is missing for {mod.name}")
        options = mod.source.options
        repository = validate_repository(options["repository"])
        selector = _validate_selector(options["release"], "release selector")
        asset_selector = _validate_selector(options["asset"], "asset selector", asset=True)
        endpoint = self._endpoint(repository, selector)
        release = self._request_json(endpoint, mod.name)
        self._validate_release(release, mod.name)
        asset = self._select_asset(release["assets"], asset_selector, mod.name)
        filename = _safe_asset_name(asset.get("name"))
        download_url = asset.get("browser_download_url")
        if not isinstance(download_url, str):
            raise SourceError(f"GitHub release asset for {mod.name} has no download URL")
        parsed = urlsplit(download_url)
        if parsed.scheme != "https" or parsed.hostname not in _ALLOWED_ASSET_HOSTS:
            raise SourceError(f"GitHub release asset for {mod.name} has an unsafe download URL")

        tag = _validate_selector(release["tag_name"], "release tag")
        asset_id = asset.get("id")
        if not isinstance(asset_id, int) or isinstance(asset_id, bool):
            raise SourceError(f"GitHub release asset for {mod.name} has an invalid ID")
        resolved_at = _utc_now()
        source_metadata = {
            "type": "github",
            "repository": repository,
            "release": selector,
        }
        release_metadata = {
            "tag": tag,
            "release_id": release["id"],
            "asset": filename,
            "asset_id": asset_id,
            "download_url": download_url,
            "published_at": release["published_at"],
            "resolved_at": resolved_at,
        }
        identity = {
            "type": "github",
            "repository": repository,
            "tag": tag,
            "asset": filename,
            "asset_id": asset_id,
        }
        headers = self._headers()
        return ResolvedMod(
            name=mod.name,
            version=normalize_tag(tag),
            download_url=download_url,
            filename=filename,
            sha256=mod.sha256,
            source_metadata=source_metadata,
            release_metadata=release_metadata,
            source_identity=identity,
            request_headers=headers,
        )

    @staticmethod
    def _endpoint(repository: str, selector: str) -> str:
        base = f"https://api.github.com/repos/{repository}/releases"
        if selector == "latest":
            return f"{base}/latest"
        return f"{base}/tags/{quote(selector, safe='')}"

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": f"ModSync/{__version__}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if isinstance(self.token, str) and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _request_json(self, endpoint: str, mod_name: str) -> dict[str, Any]:
        for attempt in range(self.attempts):
            response = None
            try:
                response = self.session.get(
                    endpoint,
                    timeout=(10, 30),
                    allow_redirects=True,
                    headers=self._headers(),
                )
                if response.status_code in {403, 429}:
                    raise SourceRateLimitError(
                        "GitHub API rate limit reached; try again later or set "
                        "MODSYNC_GITHUB_TOKEN"
                    )
                if response.status_code == 404:
                    raise SourceNotFoundError(f"GitHub release not found for {mod_name}")
                if 500 <= response.status_code < 600 and attempt + 1 < self.attempts:
                    self.sleeper(float(2**attempt))
                    continue
                response.raise_for_status()
                value = response.json()
                if not isinstance(value, dict):
                    raise SourceError(f"GitHub returned malformed release data for {mod_name}")
                return value
            except (SourceError, SourceRateLimitError):
                raise
            except (requests.RequestException, ValueError) as exc:
                if attempt + 1 < self.attempts:
                    self.sleeper(float(2**attempt))
                    continue
                raise SourceError(f"Could not resolve GitHub release for {mod_name}: {exc}") from exc
            finally:
                if response is not None:
                    response.close()
        raise SourceError(f"Could not resolve GitHub release for {mod_name}")

    @staticmethod
    def _validate_release(release: dict[str, Any], mod_name: str) -> None:
        if (
            not isinstance(release.get("id"), int)
            or isinstance(release.get("id"), bool)
            or not isinstance(release.get("tag_name"), str)
            or not release["tag_name"]
            or not isinstance(release.get("published_at"), str)
            or not isinstance(release.get("assets"), list)
        ):
            raise SourceError(f"GitHub returned malformed release data for {mod_name}")
        if release.get("draft") is not False or release.get("prerelease") is not False:
            raise SourceError(f"GitHub release for {mod_name} is not a published stable release")

    @staticmethod
    def _select_asset(assets: list[object], selector: str, mod_name: str) -> dict[str, Any]:
        wildcard = any(character in selector for character in "*?[")
        matches = [
            asset
            for asset in assets
            if isinstance(asset, dict)
            and isinstance(asset.get("name"), str)
            and (
                fnmatch.fnmatchcase(asset["name"], selector)
                if wildcard
                else asset["name"] == selector
            )
        ]
        if not matches:
            raise SourceNotFoundError(
                f"No GitHub release asset matches {selector!r} for {mod_name}"
            )
        if len(matches) > 1:
            raise SourceAmbiguousError(
                f"GitHub release asset selector {selector!r} is ambiguous for {mod_name}"
            )
        return matches[0]
