"""Rotating, redacted desktop application logging."""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

from ..profiles import default_data_directory

_SENSITIVE = (
    re.compile(r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?\S+"),
    re.compile(r"(?i)(MODSYNC_GITHUB_TOKEN\s*[:=]\s*)\S+"),
    re.compile(r"\b(?:ghp_|github_pat_)[A-Za-z0-9_]+\b"),
)


def redact(value: str) -> str:
    result = value
    for pattern in _SENSITIVE:
        result = pattern.sub(lambda match: (match.group(1) if match.lastindex else "") + "[REDACTED]", result)
    return result


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def log_directory() -> Path:
    return default_data_directory() / "logs"


def configure_logging(directory: Path | None = None) -> Path:
    root = directory or log_directory()
    root.mkdir(parents=True, exist_ok=True)
    destination = root / "modsync-gui.log"
    logger = logging.getLogger("modsync")
    logger.setLevel(logging.INFO)
    if not any(isinstance(item, RotatingFileHandler) for item in logger.handlers):
        handler = RotatingFileHandler(
            destination, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(
            RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        logger.addHandler(handler)
    return destination


def close_logging() -> None:
    """Close GUI file handlers so Windows can release the log directory."""
    logger = logging.getLogger("modsync")
    for handler in tuple(logger.handlers):
        if isinstance(handler, RotatingFileHandler):
            logger.removeHandler(handler)
            handler.close()
