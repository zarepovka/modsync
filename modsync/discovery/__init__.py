"""Public API for local, provider-based game discovery."""

from .base import DiscoveryContext, GameDiscoveryProvider
from .registry import DiscoveryRegistry, build_default_discovery_registry
from .steam import SteamDiscoveryProvider
from .vdf import parse_keyvalues

__all__ = [
    "DiscoveryContext",
    "DiscoveryRegistry",
    "GameDiscoveryProvider",
    "SteamDiscoveryProvider",
    "build_default_discovery_registry",
    "parse_keyvalues",
]
