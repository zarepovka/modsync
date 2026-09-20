"""Central registry for game-specific installation adapters."""

from __future__ import annotations

from .base import GameAdapter
from ..exceptions import GameAdapterError


class GameRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, GameAdapter] = {}

    @staticmethod
    def normalize(value: str) -> str:
        return value.strip().casefold().replace("_", "-").replace(" ", "-")

    def register(self, adapter: GameAdapter, *aliases: str) -> None:
        for name in (adapter.game_id, adapter.display_name, *aliases):
            key = self.normalize(name)
            if not key:
                raise ValueError(f"Duplicate or empty game adapter: {name}")
            if key in self._adapters:
                if self._adapters[key] is adapter:
                    continue
                raise ValueError(f"Duplicate or empty game adapter: {name}")
            self._adapters[key] = adapter

    def get(self, game: str) -> GameAdapter:
        adapter = self._adapters.get(self.normalize(game))
        if adapter is None:
            raise GameAdapterError(f"Unsupported game adapter: {game}")
        return adapter

    def all(self) -> tuple[GameAdapter, ...]:
        """Return registered adapters once, preserving registration order."""
        return tuple(dict.fromkeys(self._adapters.values()))


def build_default_game_registry() -> GameRegistry:
    from .valheim import ValheimAdapter

    registry = GameRegistry()
    registry.register(ValheimAdapter())
    return registry
