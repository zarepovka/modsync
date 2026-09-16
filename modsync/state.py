"""Persistent local installation state."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .exceptions import StateError

STATE_FILENAME = ".modsync-state.json"


def empty_state() -> dict[str, Any]:
    return {"schema_version": 1, "modpack": {}, "mods": {}}


def load_state(install_directory: Path) -> dict[str, Any]:
    """Load state, returning an empty state if ModSync has not run yet."""
    path = install_directory / STATE_FILENAME
    if not path.exists():
        return empty_state()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StateError(f"Cannot read installation state {path}: {exc}") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or not isinstance(value.get("mods"), dict)
    ):
        raise StateError(f"Unsupported or invalid installation state: {path}")
    return value


def save_state(install_directory: Path, state: dict[str, Any]) -> None:
    """Atomically save state in the installation directory."""
    path = install_directory / STATE_FILENAME
    temporary = install_directory / f"{STATE_FILENAME}.tmp"
    try:
        install_directory.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps(state, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise StateError(f"Cannot save installation state {path}: {exc}") from exc
