"""Extensible game adapter layer."""

from .base import GameAdapter, collision_key, validate_plan, validate_relative_destination
from .registry import GameRegistry, build_default_game_registry
from .valheim import ValheimAdapter

__all__ = [
    "GameAdapter",
    "GameRegistry",
    "ValheimAdapter",
    "build_default_game_registry",
    "collision_key",
    "validate_plan",
    "validate_relative_destination",
]
