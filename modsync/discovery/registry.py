"""Registry combining game adapters with independent discovery providers."""

from __future__ import annotations

from ..exceptions import DiscoveryProviderError
from ..games.registry import GameRegistry, build_default_game_registry
from ..models import GameInstallation
from .base import GameDiscoveryProvider, canonical_path_key
from .steam import SteamDiscoveryProvider


class DiscoveryRegistry:
    def __init__(self, game_registry: GameRegistry | None = None) -> None:
        self.games = game_registry or build_default_game_registry()
        self._providers: dict[str, GameDiscoveryProvider] = {}

    @staticmethod
    def normalize(value: str) -> str:
        return value.strip().casefold().replace("_", "-").replace(" ", "-")

    def register(self, provider: GameDiscoveryProvider) -> None:
        key = self.normalize(provider.provider_id)
        if not key or key in self._providers:
            raise ValueError(f"Duplicate or empty discovery provider: {provider.provider_id}")
        self._providers[key] = provider

    def get(self, provider: str) -> GameDiscoveryProvider:
        value = self._providers.get(self.normalize(provider))
        if value is None:
            raise DiscoveryProviderError(f"Unsupported discovery provider: {provider}")
        return value

    def discover(
        self, game: str | None = None, *, provider: str | None = None
    ) -> list[GameInstallation]:
        adapters = (self.games.get(game),) if game is not None else self.games.all()
        providers = (self.get(provider),) if provider is not None else tuple(self._providers.values())
        results: list[GameInstallation] = []
        seen: set[tuple[str, str]] = set()
        for adapter in adapters:
            for discovery_provider in providers:
                for installation in discovery_provider.discover(adapter):
                    key = (
                        installation.game_id.casefold(),
                        canonical_path_key(installation.install_path, installation.platform),
                    )
                    if key not in seen:
                        seen.add(key)
                        results.append(installation)
        return sorted(
            results,
            key=lambda item: (item.display_name.casefold(), str(item.install_path).casefold()),
        )


def build_default_discovery_registry() -> DiscoveryRegistry:
    registry = DiscoveryRegistry()
    registry.register(SteamDiscoveryProvider())
    return registry
