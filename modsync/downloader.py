"""Streaming HTTP downloads with conservative defaults."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import time
from urllib.parse import unquote, urlsplit

import requests

from . import __version__
from .exceptions import DownloadError
from .models import Mod, ResolvedMod

ProgressCallback = Callable[[int, int | None], None]


class Downloader:
    """Download mod artifacts over HTTP(S) into a caller-owned directory."""

    def __init__(
        self,
        session: requests.Session | None = None,
        *,
        max_bytes: int = 2 * 1024 * 1024 * 1024,
        attempts: int = 3,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self.session = session or requests.Session()
        self.max_bytes = max_bytes
        self.attempts = max(1, attempts)
        self.sleeper = sleeper or time.sleep

    def download(
        self,
        mod: Mod | ResolvedMod,
        destination: Path,
        progress: ProgressCallback | None = None,
    ) -> Path:
        """Download ``mod`` and return the completed local artifact path."""
        url = mod.download_url if isinstance(mod, ResolvedMod) else mod.url
        if not isinstance(url, str):
            raise DownloadError(f"No resolved download URL for {mod.name}")
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise DownloadError(f"Invalid download URL for {mod.name}")

        filename = (
            mod.filename
            if isinstance(mod, ResolvedMod)
            else Path(unquote(parsed.path)).name or f"{mod.name}.download"
        )
        filename = filename.replace("\x00", "")
        if not filename or filename in {".", ".."} or Path(filename).name != filename:
            raise DownloadError(f"Unsafe download filename for {mod.name}")
        destination.mkdir(parents=True, exist_ok=True)
        completed = destination / filename
        partial = destination / f"{filename}.part"

        for attempt in range(self.attempts):
            response = None
            try:
                response = self.session.get(
                    url,
                    stream=True,
                    timeout=(10, 60),
                    allow_redirects=True,
                    headers={
                        "User-Agent": f"ModSync/{__version__}",
                        **(mod.request_headers if isinstance(mod, ResolvedMod) else {}),
                    },
                )
                transient_status = response.status_code in {408, 425, 429} or (
                    500 <= response.status_code < 600
                )
                if transient_status and attempt + 1 < self.attempts:
                    self.sleeper(float(2**attempt))
                    continue
                response.raise_for_status()
                final_url = urlsplit(getattr(response, "url", url))
                if parsed.scheme == "https" and final_url.scheme != "https":
                    raise DownloadError(f"Unsafe redirect while downloading {mod.name}")
                self._validate_provider_redirect(mod, final_url.hostname)
                raw_length = response.headers.get("Content-Length")
                total = int(raw_length) if raw_length and raw_length.isdigit() else None
                if total is not None and total > self.max_bytes:
                    raise DownloadError(
                        f"Download for {mod.name} exceeds the 2 GiB safety limit"
                    )

                downloaded = 0
                with partial.open("wb") as output:
                    for chunk in response.iter_content(chunk_size=1024 * 256):
                        if not chunk:
                            continue
                        downloaded += len(chunk)
                        if downloaded > self.max_bytes:
                            raise DownloadError(
                                f"Download for {mod.name} exceeds the 2 GiB safety limit"
                            )
                        output.write(chunk)
                        if progress is not None:
                            progress(downloaded, total)
                partial.replace(completed)
                return completed
            except DownloadError:
                raise
            except (requests.RequestException, OSError, ValueError) as exc:
                if attempt + 1 < self.attempts:
                    self.sleeper(float(2**attempt))
                    continue
                raise DownloadError(f"Could not download {mod.name}: {exc}") from exc
            finally:
                partial.unlink(missing_ok=True)
                if response is not None:
                    response.close()
        raise DownloadError(f"Could not download {mod.name}")

    @staticmethod
    def _validate_provider_redirect(mod: Mod | ResolvedMod, hostname: str | None) -> None:
        if not isinstance(mod, ResolvedMod):
            return
        source_type = mod.source_metadata.get("type")
        allowed_hosts = {
            "github": {
                "github.com",
                "objects.githubusercontent.com",
                "release-assets.githubusercontent.com",
            },
            "thunderstore": {
                "thunderstore.io",
                "gcdn.thunderstore.io",
                "cdn.thunderstore.io",
            },
        }.get(source_type)
        if allowed_hosts is not None and hostname not in allowed_hosts:
            raise DownloadError(
                f"Unsafe {source_type} redirect while downloading {mod.name}"
            )
