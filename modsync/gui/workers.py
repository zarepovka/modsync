"""QThreadPool workers with structured progress and cooperative cancellation."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, Signal, Slot

from ..services import CancellationToken, ProgressEvent


class WorkerSignals(QObject):
    progress = Signal(object)
    result = Signal(object)
    error = Signal(object)
    finished = Signal()
    cancellation_changed = Signal(bool)


class OperationWorker(QRunnable):
    """Execute one service operation without ever touching widgets."""

    def __init__(
        self,
        operation: Callable[[Callable[[ProgressEvent], None], CancellationToken], Any],
    ) -> None:
        super().__init__()
        self.operation = operation
        self.signals = WorkerSignals()
        self.token = CancellationToken()
        self.setAutoDelete(True)

    def cancel(self) -> bool:
        accepted = self.token.cancel()
        self.signals.cancellation_changed.emit(accepted)
        return accepted

    @Slot()
    def run(self) -> None:
        try:
            result = self.operation(self.signals.progress.emit, self.token)
        except Exception as exc:  # Forwarded to the GUI as a safe user-facing error.
            self.signals.error.emit(exc)
        else:
            self.signals.result.emit(result)
        finally:
            self.signals.finished.emit()
