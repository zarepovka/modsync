"""Compact system/light/dark palette support."""

from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication


def apply_theme(app: QApplication, theme: str) -> None:
    if theme == "System":
        app.setPalette(app.style().standardPalette())
        return
    palette = QPalette()
    dark = theme == "Dark"
    window = QColor("#202124" if dark else "#f6f7f9")
    base = QColor("#292a2d" if dark else "#ffffff")
    text = QColor("#f1f3f4" if dark else "#202124")
    disabled = QColor("#9aa0a6" if dark else "#80868b")
    highlight = QColor("#8ab4f8" if dark else "#2563eb")
    palette.setColor(QPalette.ColorRole.Window, window)
    palette.setColor(QPalette.ColorRole.WindowText, text)
    palette.setColor(QPalette.ColorRole.Base, base)
    palette.setColor(QPalette.ColorRole.AlternateBase, window)
    palette.setColor(QPalette.ColorRole.Text, text)
    palette.setColor(QPalette.ColorRole.Button, base)
    palette.setColor(QPalette.ColorRole.ButtonText, text)
    palette.setColor(QPalette.ColorRole.Highlight, highlight)
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, disabled)
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, disabled)
    app.setPalette(palette)
