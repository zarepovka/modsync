"""Shared small Qt Widgets components."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLabel, QPushButton, QVBoxLayout, QWidget


class EmptyState(QWidget):
    action_requested = Signal()

    def __init__(self, title: str, message: str, action: str = "") -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        heading = QLabel(title)
        heading.setObjectName("emptyStateTitle")
        heading.setStyleSheet("font-size: 20px; font-weight: 600;")
        heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        body = QLabel(message)
        body.setWordWrap(True)
        body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(heading)
        layout.addWidget(body)
        if action:
            button = QPushButton(action)
            button.clicked.connect(self.action_requested)
            layout.addWidget(button, alignment=Qt.AlignmentFlag.AlignCenter)
