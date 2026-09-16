"""Persistent local installation state."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

from .exceptions import StateError

STATE_FILENAME = ".modsync-state.json"


def empty_state() -> dict[str, Any]:
    return {"schema_version": 1, "modpack": {}, "mods": {}}


def validate_state(value: object, source: str = "installation state") -> dict[str, Any]:
    """Validate the stable top-level state contract."""
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or not isinstance(value.get("modpack"), dict)
        or not isinstance(value.get("mods"), dict)
    ):
        raise StateError(f"Unsupported or invalid {source}")
    return value


def load_state(install_directory: Path) -> dict[str, Any]:
    """Load state, returning an empty state if ModSync has not run yet."""
    return load_state_file(install_directory / STATE_FILENAME)


def load_state_file(path: Path) -> dict[str, Any]:
    """Load state from an explicit path, returning an empty initial state if absent."""
    if not path.exists():
        return empty_state()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StateError(f"Cannot read installation state {path}: {exc}") from exc
    try:
        return validate_state(value, f"installation state: {path}")
    except StateError as exc:
        raise StateError(str(exc)) from exc


def atomic_write_bytes(path: Path, content: bytes) -> None:
    """Write bytes with flush/fsync and replace the destination on the same filesystem."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path, value: object) -> None:
    """Serialize JSON and atomically replace ``path``."""
    content = (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    atomic_write_bytes(path, content)


def save_state(install_directory: Path, state: dict[str, Any]) -> None:
    """Atomically save state in the installation directory."""
    save_state_file(install_directory / STATE_FILENAME, state)


def save_state_file(path: Path, state: dict[str, Any]) -> None:
    """Atomically save state to an explicit path."""
    try:
        validate_state(state)
        atomic_write_json(path, state)
    except OSError as exc:
        raise StateError(f"Cannot save installation state {path}: {exc}") from exc
