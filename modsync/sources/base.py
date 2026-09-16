"""Provider interface and central source registry."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

import requests

from ..exceptions import SourceError
from ..models import Mod, ResolvedMod


class ModSource(Protocol):
    """Resolve one configured mod into a provider-independent artifact."""

    def resolve(self, mod: Mod) -> ResolvedMod: ...


class SourceRegistry:
    """Dispatch source resolution without leaking provider details to installers."""

    def __init__(self) -> None:
        self._providers: dict[str, ModSource] = {}

    def register(self, name: str, provider: ModSource) -> None:
        if not name or name in self._providers:
            raise ValueError(f"Duplicate or empty source provider: {name}")
        self._providers[name] = provider

    def resolve(self, mod: Mod) -> ResolvedMod:
        source_type = mod.source.type if mod.source is not None else "direct"
        provider = self._providers.get(source_type)
        if provider is None:
            raise SourceError(f"Unsupported mod source type: {source_type}")
        return provider.resolve(mod)


def build_default_registry(
    session: requests.Session | None = None,
    *,
    sleeper: Callable[[float], None] | None = None,
) -> SourceRegistry:
    """Build the standard registry. Imports stay local to avoid provider cycles."""
    from .direct import DirectSource
    from .github import GitHubReleaseSource

    registry = SourceRegistry()
    registry.register("direct", DirectSource())
    registry.register("github", GitHubReleaseSource(session=session, sleeper=sleeper))
    return registry
