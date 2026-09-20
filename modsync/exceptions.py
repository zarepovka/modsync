"""Application-specific exceptions with user-facing messages."""


class ModSyncError(Exception):
    """Base exception for expected ModSync failures."""


class ConfigError(ModSyncError):
    """Raised when a modpack configuration is invalid."""


class DownloadError(ModSyncError):
    """Raised when a mod cannot be downloaded safely."""


class SourceError(ModSyncError):
    """Raised when a source cannot be validated or resolved."""


class SourceNotFoundError(SourceError):
    """Raised when a requested release or asset does not exist."""


class SourceAmbiguousError(SourceError):
    """Raised when an asset selector matches more than one release asset."""


class SourceRateLimitError(SourceError):
    """Raised when the source service rejects a request due to rate limits."""


class DependencyError(SourceError):
    """Raised when a package dependency graph is invalid."""


class DependencyConflictError(DependencyError):
    """Raised when one package is required at incompatible versions."""


class DependencyCycleError(DependencyError):
    """Raised when a dependency graph contains a cycle."""


class InstallError(ModSyncError):
    """Raised when a downloaded mod cannot be installed safely."""


class GameAdapterError(InstallError):
    """Raised when a game adapter cannot validate or plan an installation."""


class InstallationConflictError(GameAdapterError):
    """Raised before apply when two owners or an unmanaged file collide."""


class LifecycleError(InstallError):
    """Raised when uninstall, disable, or enable cannot proceed safely."""


class DependencySafetyError(LifecycleError):
    """Raised when a lifecycle operation would break another package."""


class ManifestError(InstallError):
    """Raised when package manifest metadata is missing or inconsistent."""


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


class ProfileError(ModSyncError):
    """Raised when profile data is invalid or a profile operation cannot complete."""


class ProfileNotFoundError(ProfileError):
    """Raised when a requested profile does not exist."""


class ProfileExistsError(ProfileError):
    """Raised when a profile name is already in use."""


class ProfileLockError(ProfileError):
    """Raised when a modifying operation cannot acquire a profile lock."""
