"""Provider interface and central source registry."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import requests

from ..exceptions import DependencyConflictError, DependencyCycleError, SourceError
from ..models import Mod, ResolvedMod, ResolvedPlanItem


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

    def resolve_plan(self, mods: tuple[Mod, ...]) -> list[ResolvedPlanItem]:
        """Resolve and flatten dependencies in deterministic dependency-first order."""
        roots = [(mod, self.resolve(mod)) for mod in mods if mod.enabled]
        root_by_key: dict[str, tuple[Mod, ResolvedMod]] = {}
        for mod, resolved in roots:
            key = self._key(resolved, mod)
            previous = root_by_key.get(key)
            if previous is not None:
                if previous[1].version != resolved.version:
                    self._raise_conflict(resolved, previous[1].version, resolved.version)
                raise DependencyConflictError(
                    f"Package source is declared more than once: {resolved.name}"
                )
            root_by_key[key] = (mod, resolved)

        result: list[ResolvedPlanItem] = []
        emitted: set[str] = set()
        versions: dict[str, str] = {}
        names: dict[str, str] = {}

        def visit(
            mod: Mod,
            resolved: ResolvedMod,
            *,
            explicit: bool,
            required_by: tuple[str, ...],
            stack: tuple[str, ...],
            labels: tuple[str, ...],
        ) -> None:
            key = self._key(resolved, mod)
            previous_version = versions.get(key)
            if previous_version is not None and previous_version != resolved.version:
                self._raise_conflict(resolved, previous_version, resolved.version)
            versions[key] = resolved.version
            if key in stack:
                start = stack.index(key)
                chain = (*labels[start:], resolved.name)
                raise DependencyCycleError(
                    f"Circular dependency detected: {' -> '.join(chain)}"
                )
            if key in emitted:
                return

            target_mod = mod
            target_resolved = resolved
            target_explicit = explicit
            declared_root = root_by_key.get(key)
            if declared_root is not None:
                target_mod, target_resolved = declared_root
                target_explicit = True
                if target_resolved.version != resolved.version:
                    self._raise_conflict(
                        resolved, target_resolved.version, resolved.version
                    )

            next_stack = (*stack, key)
            next_labels = (*labels, target_resolved.name)
            for dependency in target_resolved.dependencies:
                dependency_mod = Mod(
                    name=dependency.name,
                    version=dependency.version,
                    url=None,
                    source=None,
                )
                visit(
                    dependency_mod,
                    dependency,
                    explicit=False,
                    required_by=(*required_by, target_resolved.name),
                    stack=next_stack,
                    labels=next_labels,
                )

            folded_name = target_mod.name.casefold()
            name_key = names.get(folded_name)
            if name_key is not None and name_key != key:
                raise DependencyConflictError(
                    f"Installation name conflict: {target_mod.name} refers to multiple packages"
                )
            names[folded_name] = key
            emitted.add(key)
            result.append(
                ResolvedPlanItem(
                    mod=target_mod,
                    resolved=target_resolved,
                    explicit=target_explicit,
                    required_by=required_by,
                )
            )

        for mod, resolved in roots:
            visit(
                mod,
                resolved,
                explicit=True,
                required_by=(),
                stack=(),
                labels=(),
            )
        return result

    def validate_staged(self, resolved: ResolvedMod, directory: Path) -> None:
        """Let a provider validate extracted content without coupling the installer."""
        source_type = resolved.source_metadata.get("type")
        provider = self._providers.get(source_type) if isinstance(source_type, str) else None
        validator = getattr(provider, "validate_staged", None)
        if validator is not None:
            validator(resolved, directory)

    @staticmethod
    def _key(resolved: ResolvedMod, mod: Mod) -> str:
        return resolved.provider_key or f"explicit:{mod.name.casefold()}"

    @staticmethod
    def _raise_conflict(resolved: ResolvedMod, first: str, second: str) -> None:
        package = resolved.source_metadata.get("package", resolved.name)
        raise DependencyConflictError(
            "Dependency conflict:\n\n"
            f"{package} is required as both {first} and {second}."
        )


def build_default_registry(
    session: requests.Session | None = None,
    *,
    sleeper: Callable[[float], None] | None = None,
) -> SourceRegistry:
    """Build the standard registry. Imports stay local to avoid provider cycles."""
    from .direct import DirectSource
    from .github import GitHubReleaseSource
    from .thunderstore import ThunderstoreSource

    registry = SourceRegistry()
    registry.register("direct", DirectSource())
    registry.register("github", GitHubReleaseSource(session=session, sleeper=sleeper))
    registry.register(
        "thunderstore", ThunderstoreSource(session=session, sleeper=sleeper)
    )
    return registry
