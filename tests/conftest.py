"""Shared GUI test configuration."""

from __future__ import annotations

import os


# This must be set before pytest-qt imports Qt and creates QApplication.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
