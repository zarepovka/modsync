"""Verification of installed versions and file integrity."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import mod_directory_name
from .hashing import sha256_file
from .models import Mod, Modpack, VerificationIssue, VerificationReport
from .state import STATE_FILENAME, load_state_file


def verify_mod_record(root: Path, mod: Mod, record: object) -> list[str]:
    """Return problems found for one mod and its state record."""
    if not isinstance(record, dict):
        return ["is not recorded as installed"]

    problems: list[str] = []
    if record.get("version") != mod.version:
        problems.append(
            f"version mismatch: expected {mod.version}, found {record.get('version', 'unknown')}"
        )
    if mod.sha256 is not None and record.get("source_sha256") != mod.sha256:
        problems.append("downloaded artifact checksum does not match the modpack")

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


def verify_modpack(modpack: Modpack) -> VerificationReport:
    """Verify all enabled mods against local state and recorded file hashes."""
    state_path = modpack.state_path or modpack.install_directory / STATE_FILENAME
    state = load_state_file(state_path)
    records: dict[str, Any] = state["mods"]
    issues: list[VerificationIssue] = []
    checked = 0
    for mod in modpack.mods:
        if not mod.enabled:
            continue
        checked += 1
        for message in verify_mod_record(modpack.install_directory, mod, records.get(mod.name)):
            issues.append(VerificationIssue(mod_name=mod.name, message=message))
    return VerificationReport(checked=checked, issues=tuple(issues))
