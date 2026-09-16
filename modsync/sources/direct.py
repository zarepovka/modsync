"""Legacy and explicit direct-download source provider."""

from __future__ import annotations

from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

from ..exceptions import SourceError
from ..models import Mod, ResolvedMod


def _download_filename(url: str, fallback: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SourceError(f"Invalid direct download URL for {fallback}")
    filename = PurePosixPath(unquote(parsed.path)).name or f"{fallback}.download"
    if any(ord(character) < 32 for character in filename) or filename in {".", ".."}:
        raise SourceError(f"Unsafe download filename for {fallback}")
    return filename


class DirectSource:
    """Resolve a fixed HTTP(S) URL without remote metadata lookup."""

    def resolve(self, mod: Mod) -> ResolvedMod:
        options = mod.source.options if mod.source is not None else {}
        url = options.get("url") or mod.url
        if not url:
            raise SourceError(f"Direct source for {mod.name} requires a URL")
        version = options.get("version") or mod.version or "unversioned"
        filename = _download_filename(url, mod.name)
        source_metadata = {"type": "direct", "url": url}
        identity = {"type": "direct", "url": url, "version": version}
        return ResolvedMod(
            name=mod.name,
            version=version,
            download_url=url,
            filename=filename,
            sha256=mod.sha256,
            source_metadata=source_metadata,
            release_metadata={"version": version},
            source_identity=identity,
        )
