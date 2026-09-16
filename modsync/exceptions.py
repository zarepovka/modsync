"""Application-specific exceptions with user-facing messages."""


class ModSyncError(Exception):
    """Base exception for expected ModSync failures."""


class ConfigError(ModSyncError):
    """Raised when a modpack configuration is invalid."""


class DownloadError(ModSyncError):
    """Raised when a mod cannot be downloaded safely."""


class InstallError(ModSyncError):
    """Raised when a downloaded mod cannot be installed safely."""


class StateError(ModSyncError):
    """Raised when the local ModSync state file cannot be read or written."""


class BackupError(ModSyncError):
    """Raised when a backup cannot be created, read, or maintained."""


class BackupNotFoundError(BackupError):
    """Raised when a requested backup does not exist."""


class BackupIntegrityError(BackupError):
    """Raised when backup metadata or file integrity validation fails."""


class RollbackError(BackupError):
    """Raised when a backup cannot be restored safely."""
