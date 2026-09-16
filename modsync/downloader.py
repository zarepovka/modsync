"""Streaming HTTP downloads with conservative defaults."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from urllib.parse import unquote, urlsplit

import requests

from .exceptions import DownloadError
from .models import Mod

ProgressCallback = Callable[[int, int | None], None]


class Downloader:
    """Download mod artifacts over HTTP(S) into a caller-owned directory."""

    def __init__(
        self,
        session: requests.Session | None = None,
        *,
        max_bytes: int = 2 * 1024 * 1024 * 1024,
    ) -> None:
        self.session = session or requests.Session()
        self.max_bytes = max_bytes

    def download(
        self,
        mod: Mod,
        destination: Path,
        progress: ProgressCallback | None = None,
    ) -> Path:
        """Download ``mod`` and return the completed local artifact path."""
        parsed = urlsplit(mod.url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise DownloadError(f"Invalid download URL for {mod.name}: {mod.url}")

        filename = Path(unquote(parsed.path)).name or f"{mod.name}.download"
        filename = filename.replace("\x00", "")
        if not filename:
            filename = f"{mod.name}.download"
        destination.mkdir(parents=True, exist_ok=True)
        completed = destination / filename
        partial = destination / f"{filename}.part"

        response = None
        try:
            response = self.session.get(
                mod.url,
                stream=True,
                timeout=(10, 60),
                allow_redirects=True,
                headers={"User-Agent": "ModSync/0.1"},
            )
            response.raise_for_status()
            raw_length = response.headers.get("Content-Length")
            total = int(raw_length) if raw_length and raw_length.isdigit() else None
            if total is not None and total > self.max_bytes:
                raise DownloadError(f"Download for {mod.name} exceeds the 2 GiB safety limit")

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
            raise DownloadError(f"Could not download {mod.name}: {exc}") from exc
        finally:
            partial.unlink(missing_ok=True)
            if response is not None:
                response.close()
