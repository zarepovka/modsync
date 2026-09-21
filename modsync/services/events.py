"""Application-level progress and cooperative cancellation primitives."""

from __future__ import annotations

from dataclasses import dataclass
from threading import Event, Lock

from ..exceptions import ModSyncError


class OperationCancelled(ModSyncError):
    """Raised at a safe checkpoint after the user requests cancellation."""


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """Structured operation progress shared by desktop and other frontends."""

    stage: str
    message: str
    current: int | None = None
    total: int | None = None
    cancellable: bool = True


class CancellationToken:
    """Cooperative cancellation that becomes immutable at the mutation boundary."""

    def __init__(self) -> None:
        self._requested = Event()
        self._mutation_started = Event()
        self._lock = Lock()

    @property
    def cancellation_requested(self) -> bool:
        return self._requested.is_set()

    @property
    def mutation_started(self) -> bool:
        return self._mutation_started.is_set()

    @property
    def cancellable(self) -> bool:
        return not self.mutation_started

    def cancel(self) -> bool:
        """Request cancellation, returning false after mutation has started."""
        with self._lock:
            if self._mutation_started.is_set():
                return False
            self._requested.set()
            return True

    def checkpoint(self) -> None:
        if self._requested.is_set() and not self._mutation_started.is_set():
            raise OperationCancelled("Operation cancelled safely before files were changed")

    def begin_mutation(self) -> None:
        """Cross the point after which a worker must finish or roll back safely."""
        with self._lock:
            if self._requested.is_set():
                raise OperationCancelled("Operation cancelled safely before files were changed")
            self._mutation_started.set()
