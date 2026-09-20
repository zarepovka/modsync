"""Verification of installed versions and file integrity."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import mod_directory_name
from .exceptions import GameAdapterError
from .games.base import validate_relative_destination
from .hashing import sha256_file
from .models import (
    Mod,
    Modpack,
    ResolvedMod,
    ResolvedPlanItem,
    VerificationIssue,
    VerificationReport,
)
from .state import STATE_FILENAME, disabled_storage_path, load_state_file, record_status


def verify_mod_record(
    root: Path,
    mod: Mod,
    record: object,
    *,
    resolved: ResolvedMod | None = None,
    disabled_root: Path | None = None,
) -> list[str]:
    """Return problems found for one mod and its state record."""
    if not isinstance(record, dict):
        return ["is not recorded as installed"]

    problems: list[str] = []
    expected_version = resolved.version if resolved is not None else mod.version
    if expected_version is not None and record.get("version") != expected_version:
        problems.append(
            f"version mismatch: expected {expected_version}, "
            f"found {record.get('version', 'unknown')}"
        )
    if mod.sha256 is not None and record.get("source_sha256") != mod.sha256:
        problems.append("downloaded artifact checksum does not match the modpack")
    if resolved is not None:
        source = record.get("source")
        if isinstance(source, dict):
            if source.get("identity") != resolved.source_identity:
                problems.append("resolved source release has changed")
        elif resolved.source_metadata.get("type") != "direct":
            problems.append("resolved source metadata is missing")

    installed_files = record.get("installed_files")
    if installed_files is not None:
        if not isinstance(installed_files, list) or not installed_files:
            problems.append("has no recorded installed files")
            return problems
        expected_owner = record.get("owner")
        for entry in installed_files:
            if not isinstance(entry, dict):
                problems.append("contains an invalid ownership record")
                continue
            relative = entry.get("path")
            owner = entry.get("owner")
            expected_digest = entry.get("sha256")
            if not all(isinstance(value, str) for value in (relative, owner, expected_digest)):
                problems.append("contains an invalid ownership record")
                continue
            if expected_owner is not None and owner != expected_owner:
                problems.append(f"ownership mismatch: {relative}")
            try:
                safe_relative = validate_relative_destination(relative)
            except GameAdapterError:
                problems.append(f"contains an unsafe recorded path: {relative}")
                continue
            storage = entry.get("storage", "game")
            if storage not in {"game", "disabled"}:
                problems.append(f"contains an invalid storage record: {relative}")
                continue
            status = record_status(record)
            if status == "enabled" and storage != "game":
                problems.append(f"enabled file is outside the game: {relative}")
                continue
            if storage == "disabled":
                if disabled_root is None:
                    problems.append(f"disabled storage is unavailable: {relative}")
                    continue
                candidate = disabled_root / owner / Path(*safe_relative.parts)
                containment_root = disabled_root
            else:
                candidate = root / Path(*safe_relative.parts)
                containment_root = root
            try:
                candidate.resolve(strict=False).relative_to(
                    containment_root.resolve(strict=False)
                )
            except ValueError:
                problems.append(f"contains an unsafe recorded path: {relative}")
                continue
            if candidate.is_symlink() or not candidate.is_file():
                problems.append(f"missing file: {relative}")
            elif sha256_file(candidate) != expected_digest:
                problems.append(f"checksum mismatch: {relative}")
        return problems

    files = record.get("files")
    if not isinstance(files, dict) or not files:
        problems.append("has no recorded installed files")
        return problems

    mod_root = root / mod_directory_name(mod.name)
    expected_paths: set[str] = set()
    for relative, expected_digest in files.items():
        if not isinstance(relative, str) or not isinstance(expected_digest, str):
            problems.append("contains an invalid file record")
            continue
        expected_paths.add(relative)
        candidate = mod_root / Path(relative)
        try:
            candidate.resolve().relative_to(mod_root.resolve())
        except ValueError:
            problems.append(f"contains an unsafe recorded path: {relative}")
            continue
        if not candidate.is_file():
            problems.append(f"missing file: {relative}")
            continue
        if sha256_file(candidate) != expected_digest:
            problems.append(f"checksum mismatch: {relative}")

    if mod_root.is_dir():
        actual_paths = {
            item.relative_to(mod_root).as_posix() for item in mod_root.rglob("*") if item.is_file()
        }
        for relative in sorted(actual_paths - expected_paths):
            problems.append(f"unexpected file: {relative}")
    return problems


def verify_modpack(
    modpack: Modpack,
    *,
    resolved_by_name: dict[str, ResolvedMod] | None = None,
) -> VerificationReport:
    """Verify all enabled mods against local state and recorded file hashes."""
    state_path = modpack.state_path or modpack.install_directory / STATE_FILENAME
    state = load_state_file(state_path)
    disabled_root = disabled_storage_path(modpack)
    records: dict[str, Any] = state["mods"]
    issues: list[VerificationIssue] = []
    checked = 0
    for mod in modpack.mods:
        if not mod.enabled:
            continue
        checked += 1
        resolved = resolved_by_name.get(mod.name) if resolved_by_name is not None else None
        for message in verify_mod_record(
            modpack.install_directory,
            mod,
            records.get(mod.name),
            resolved=resolved,
            disabled_root=disabled_root,
        ):
            issues.append(VerificationIssue(mod_name=mod.name, message=message))
    explicit_names = {mod.name.casefold() for mod in modpack.mods}
    for name, record in records.items():
        if (
            not isinstance(name, str)
            or name.casefold() in explicit_names
            or not isinstance(record, dict)
            or record.get("role") != "dependency"
        ):
            continue
        version = record.get("version")
        dependency = Mod(
            name=name,
            version=version if isinstance(version, str) else None,
            url=None,
        )
        checked += 1
        for message in verify_mod_record(
            modpack.install_directory,
            dependency,
            record,
            disabled_root=disabled_root,
        ):
            issues.append(VerificationIssue(mod_name=name, message=message))
    return VerificationReport(checked=checked, issues=tuple(issues))


def verify_resolved_plan(
    modpack: Modpack, plan: list[ResolvedPlanItem]
) -> VerificationReport:
    """Verify every explicit and dependency entry in a resolved install plan."""
    state_path = modpack.state_path or modpack.install_directory / STATE_FILENAME
    records: dict[str, Any] = load_state_file(state_path)["mods"]
    disabled_root = disabled_storage_path(modpack)
    issues: list[VerificationIssue] = []
    for item in plan:
        for message in verify_mod_record(
            modpack.install_directory,
            item.mod,
            records.get(item.mod.name),
            resolved=item.resolved,
            disabled_root=disabled_root,
        ):
            issues.append(VerificationIssue(mod_name=item.mod.name, message=message))
    return VerificationReport(checked=len(plan), issues=tuple(issues))
