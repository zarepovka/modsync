"""Reusable ModSync application layer consumed by CLI and desktop frontends."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..backup import BackupManager
from ..config import load_modpack
from ..discovery import DiscoveryRegistry, build_default_discovery_registry
from ..exceptions import InstallError, ProfileError
from ..games.registry import GameRegistry
from ..installer import Installer
from ..models import (
    BackupInfo,
    GameInstallation,
    InstallReport,
    LifecycleReport,
    Profile,
    RestoreReport,
    SwitchPlan,
    SwitchReport,
)
from ..profiles import ProfileStore
from ..state import load_state_file, record_install_reason, record_status
from ..switching import ProfileSwitcher
from .events import CancellationToken, ProgressEvent
from .models import GameView, ModView, UpdatePreview

ProgressCallback = Callable[[ProgressEvent], None]


def _installation_provenance(installation: GameInstallation) -> dict[str, object]:
    result: dict[str, object] = {
        "provider": installation.provider,
        "game_id": installation.game_id,
        "app_id": installation.app_id,
        "platform": installation.platform,
    }
    if installation.library_path is not None:
        result["library_path"] = str(installation.library_path)
    return result


class ModSyncService:
    """High-level use cases with no argparse, QWidget, or textual CLI output."""

    def __init__(
        self,
        *,
        profile_store: ProfileStore | None = None,
        discovery_registry: DiscoveryRegistry | None = None,
        game_registry: GameRegistry | None = None,
        installer_factory: Callable[[], Installer] = Installer,
        switcher_factory: Callable[[ProfileStore], ProfileSwitcher] = ProfileSwitcher,
        backup_factory: Callable[[Any], BackupManager] = BackupManager,
    ) -> None:
        self.profiles = profile_store or ProfileStore()
        self.discovery = discovery_registry or build_default_discovery_registry()
        self.games = game_registry or self.discovery.games
        self.installer_factory = installer_factory
        self.switcher_factory = switcher_factory
        self.backup_factory = backup_factory

    @staticmethod
    def _emit(callback: ProgressCallback | None, event: ProgressEvent) -> None:
        if callback is not None:
            callback(event)

    @staticmethod
    def _checkpoint(token: CancellationToken | None) -> None:
        if token is not None:
            token.checkpoint()

    @staticmethod
    def _begin_mutation(token: CancellationToken | None) -> None:
        if token is not None:
            token.begin_mutation()

    def discover_games(
        self,
        game: str | None = None,
        *,
        progress: ProgressCallback | None = None,
        token: CancellationToken | None = None,
    ) -> list[GameInstallation]:
        self._emit(progress, ProgressEvent("discovery", "Searching for installed games…"))
        self._checkpoint(token)
        results = self.discovery.discover(game, provider="steam")
        self._checkpoint(token)
        self._emit(
            progress,
            ProgressEvent(
                "discovery",
                f"Found {len(results)} installation(s)",
                len(results),
                len(results),
            ),
        )
        return results

    def list_profiles(self) -> list[Profile]:
        return self.profiles.list()

    def active_profile(self) -> Profile | None:
        name = self.profiles.active_name()
        return self.profiles.get(name) if name is not None else None

    def game_view(self) -> GameView | None:
        profiles = self.profiles.list()
        if not profiles:
            return None
        active_name = self.profiles.active_name()
        profile = next(
            (item for item in profiles if item.name == active_name), profiles[0]
        )
        adapter = self.games.get(profile.game)
        has_loader = bool(
            getattr(adapter, "has_bepinex", lambda root: False)(profile.install_directory)
        )
        provider = (
            str(profile.installation.get("provider", "Manual"))
            if profile.installation is not None
            else "Manual"
        )
        return GameView(
            adapter.game_id,
            adapter.display_name,
            provider,
            profile.install_directory,
            has_loader,
            sum(1 for item in profiles if item.game == profile.game),
        )

    def create_profile(
        self,
        name: str,
        modpack_path: Path,
        *,
        installation: GameInstallation | None = None,
        manual_path: Path | None = None,
    ) -> Profile:
        if installation is not None and manual_path is not None:
            raise ProfileError("Choose either a discovered installation or a manual path")
        override = installation.install_path if installation is not None else manual_path
        if override is not None:
            probe = load_modpack(modpack_path, install_directory_override=override)
            self.games.get(probe.game).validate_game(override)
        return self.profiles.create(
            name,
            modpack_path,
            install_directory=override,
            installation=(
                _installation_provenance(installation)
                if installation is not None
                else None
            ),
        )

    def import_modpack(self, path: Path, *, install_directory: Path | None = None):
        """Validate and return an import preview without changing profile data."""
        return load_modpack(path, install_directory_override=install_directory)

    def delete_profile(self, name: str) -> Profile:
        return self.profiles.delete(name)

    def activate_profile(self, name: str) -> Profile:
        return self.profiles.activate(name)

    def switch_plan(self, target_name: str) -> SwitchPlan:
        source = self.profiles.active_name()
        if source is None:
            raise ProfileError("Activate the profile that currently owns the game files first")
        return self.switcher_factory(self.profiles).build_plan(source, target_name)

    def switch_profile(
        self,
        target_name: str,
        *,
        progress: ProgressCallback | None = None,
        token: CancellationToken | None = None,
    ) -> SwitchReport:
        self._checkpoint(token)
        self._emit(progress, ProgressEvent("switch", "Preparing profile switch…"))
        self._begin_mutation(token)
        self._emit(
            progress,
            ProgressEvent("switch", "Switching profile safely…", cancellable=False),
        )
        return self.switcher_factory(self.profiles).switch(target_name)

    def list_mods(self, profile_name: str | None = None) -> list[ModView]:
        name = profile_name or self.profiles.active_name()
        if name is None:
            return []
        modpack = self.profiles.load_modpack(name)
        state = load_state_file(modpack.state_path or Path())
        configured = {mod.name.casefold(): mod for mod in modpack.mods}
        views: list[ModView] = []
        for name, record in state["mods"].items():
            if not isinstance(name, str) or not isinstance(record, dict):
                continue
            source = record.get("source")
            source_name = source.get("type", "unknown") if isinstance(source, dict) else "unknown"
            version = record.get("version")
            views.append(
                ModView(
                    name,
                    version if isinstance(version, str) else "unknown",
                    str(source_name),
                    record_status(record),
                    record_install_reason(record),
                )
            )
        recorded = {item.name.casefold() for item in views}
        for folded, mod in configured.items():
            if folded not in recorded:
                source = mod.source.type if mod.source is not None else "direct"
                views.append(
                    ModView(
                        mod.name,
                        mod.version or "not installed",
                        source,
                        "not installed" if mod.enabled else "configured disabled",
                        "explicit",
                    )
                )
        return sorted(views, key=lambda item: item.name.casefold())

    def install_profile(
        self,
        profile_name: str,
        *,
        progress: ProgressCallback | None = None,
        token: CancellationToken | None = None,
    ) -> InstallReport:
        return self._install_or_update(
            profile_name, updating=False, progress=progress, token=token
        )

    def update_profile(
        self,
        profile_name: str,
        *,
        progress: ProgressCallback | None = None,
        token: CancellationToken | None = None,
        dry_run: bool = False,
    ) -> InstallReport:
        return self._install_or_update(
            profile_name,
            updating=True,
            progress=progress,
            token=token,
            dry_run=dry_run,
        )

    def _install_or_update(
        self,
        profile_name: str,
        *,
        updating: bool,
        progress: ProgressCallback | None,
        token: CancellationToken | None,
        dry_run: bool = False,
    ) -> InstallReport:
        modpack = self.profiles.load_modpack(profile_name)
        installer = self.installer_factory()
        action = "Updating" if updating else "Installing"
        self._emit(progress, ProgressEvent("resolve", f"{action}: resolving packages…"))
        self._checkpoint(token)

        def download(mod, current: int, total: int | None) -> None:
            self._checkpoint(token)
            self._emit(
                progress,
                ProgressEvent("download", f"Downloading {mod.name}…", current, total),
            )

        def phase(message: str, cancellable: bool) -> None:
            if cancellable:
                self._checkpoint(token)
            else:
                self._begin_mutation(token)
            self._emit(
                progress,
                ProgressEvent("install", message, cancellable=cancellable),
            )

        report = (
            installer.update_modpack(
                modpack, download, dry_run=dry_run, phase=phase
            )
            if updating
            else installer.install_modpack(
                modpack, download, dry_run=dry_run, phase=phase
            )
        )
        if not dry_run:
            self.profiles.touch(profile_name)
        return report

    def check_updates(
        self,
        profile_name: str,
        *,
        progress: ProgressCallback | None = None,
        token: CancellationToken | None = None,
    ) -> UpdatePreview:
        report = self.update_profile(
            profile_name, progress=progress, token=token, dry_run=True
        )
        if report.failures:
            details = "; ".join(
                f"{failure.mod_name}: {failure.message}" for failure in report.failures
            )
            raise InstallError(f"Could not check for updates: {details}")
        changed = tuple(dict.fromkeys(entry.mod_name for entry in report.plan_entries))
        return UpdatePreview(changed, report.resolved, report.planned_files)

    def lifecycle_preview(self, profile_name: str, action: str, mod_name: str) -> LifecycleReport:
        modpack = self.profiles.load_modpack(profile_name)
        installer = self.installer_factory()
        if action == "uninstall":
            return installer.uninstall_mod(modpack, mod_name, dry_run=True)
        if action == "disable":
            return installer.disable_mod(modpack, mod_name, dry_run=True)
        if action == "enable":
            return installer.enable_mod(modpack, mod_name, dry_run=True)
        raise ValueError(f"Unknown lifecycle action: {action}")

    def lifecycle(
        self,
        profile_name: str,
        action: str,
        mod_name: str,
        *,
        progress: ProgressCallback | None = None,
        token: CancellationToken | None = None,
    ) -> LifecycleReport:
        self._checkpoint(token)
        self._begin_mutation(token)
        self._emit(
            progress,
            ProgressEvent(action, f"Applying {action} safely…", cancellable=False),
        )
        modpack = self.profiles.load_modpack(profile_name)
        installer = self.installer_factory()
        if action == "uninstall":
            result = installer.uninstall_mod(modpack, mod_name)
        elif action == "disable":
            result = installer.disable_mod(modpack, mod_name)
        elif action == "enable":
            result = installer.enable_mod(modpack, mod_name)
        else:
            raise ValueError(f"Unknown lifecycle action: {action}")
        self.profiles.touch(profile_name)
        return result

    def list_backups(self, profile_name: str) -> list[BackupInfo]:
        return self.backup_factory(self.profiles.load_modpack(profile_name)).list_backups()

    def restore_backup(
        self,
        profile_name: str,
        backup_id: str,
        *,
        progress: ProgressCallback | None = None,
        token: CancellationToken | None = None,
    ) -> RestoreReport:
        self._checkpoint(token)
        self._begin_mutation(token)
        self._emit(
            progress,
            ProgressEvent("restore", "Restoring verified backup…", cancellable=False),
        )
        result = self.backup_factory(
            self.profiles.load_modpack(profile_name)
        ).restore(backup_id)
        self.profiles.touch(profile_name)
        return result

    def add_mod(self, profile_name: str, definition: dict[str, Any]) -> Profile:
        """Validate a source definition through config.py and append it atomically."""
        profile = self.profiles.get(profile_name)
        source_path = profile.directory / "modpack.json"
        try:
            document = json.loads(source_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProfileError(f"Cannot read stored modpack: {exc}") from exc
        if not isinstance(document, dict) or not isinstance(document.get("mods"), list):
            raise ProfileError("Stored modpack is invalid")
        document["mods"].append(definition)
        return self.profiles.replace_modpack(profile_name, document)
