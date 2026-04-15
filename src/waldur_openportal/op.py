# flake8: noqa: F401
import logging
import os

from django.conf import settings
from openportal import (
    Allocation,  # type: ignore
    DailyProjectUsageReport,  # type: ignore
    DateRange,  # type: ignore
    Destination,  # type: ignore
    Health,  # type: ignore
    Instruction,  # type: ignore
    Job,  # type: ignore
    Node,  # type: ignore
    PortalIdentifier,  # type: ignore
    ProjectDetails,  # type: ignore
    ProjectIdentifier,  # type: ignore
    ProjectMapping,  # type: ignore
    ProjectTemplate,  # type: ignore
    ProjectUsageReport,  # type: ignore
    Quota,  # type: ignore
    Status,  # type: ignore
    Usage,  # type: ignore
    UsageReport,  # type: ignore
    UserIdentifier,  # type: ignore
    UserMapping,  # type: ignore
    fetch_job,  # type: ignore
    fetch_jobs,  # type: ignore
    get,  # type: ignore
    get_portal,  # type: ignore
    health,  # type: ignore
    is_config_loaded,  # type: ignore
    load_config,  # type: ignore
    run,  # type: ignore
    send_result,  # type: ignore
    sync_offerings,  # type: ignore
)

# These classes may not be available in all versions of the openportal library
try:
    from openportal import (
        ProjectStorageReport,  # type: ignore
        StorageReport,  # type: ignore
    )
except ImportError:

    class ProjectStorageReport:  # type: ignore
        """Stub for ProjectStorageReport - not available in this version of openportal"""

        def __init__(self, *args, **kwargs):
            raise NotImplementedError(
                "ProjectStorageReport is not available in this version of openportal"
            )

        @staticmethod
        def from_json(*args, **kwargs):
            raise NotImplementedError(
                "ProjectStorageReport is not available in this version of openportal"
            )

        @staticmethod
        def combine(*args, **kwargs):
            raise NotImplementedError(
                "ProjectStorageReport is not available in this version of openportal"
            )

        def to_json(self):
            raise NotImplementedError(
                "ProjectStorageReport is not available in this version of openportal"
            )

        def filter(self, *args, **kwargs):
            raise NotImplementedError(
                "ProjectStorageReport is not available in this version of openportal"
            )

        def remap_project(self, *args, **kwargs):
            raise NotImplementedError(
                "ProjectStorageReport is not available in this version of openportal"
            )

        def remap_users(self, *args, **kwargs):
            raise NotImplementedError(
                "ProjectStorageReport is not available in this version of openportal"
            )

        def to_storage_report(self, *args, **kwargs):
            raise NotImplementedError(
                "ProjectStorageReport is not available in this version of openportal"
            )

        def __iadd__(self, other):
            raise NotImplementedError(
                "ProjectStorageReport is not available in this version of openportal"
            )

    class StorageReport:  # type: ignore
        """Stub for StorageReport - not available in this version of openportal"""

        def __init__(self, *args, **kwargs):
            raise NotImplementedError(
                "StorageReport is not available in this version of openportal"
            )

        @staticmethod
        def combine(*args, **kwargs):
            raise NotImplementedError(
                "StorageReport is not available in this version of openportal"
            )


_have_openportal = True


def have_openportal():
    return _have_openportal


class OpenPortalError(Exception):
    pass

    def message(self):
        """
        Returns a user-friendly error message.
        """
        return "An unspecified OpenPortal error occurred."


class OpenPortalOtherError(OpenPortalError):
    def __init__(self, message=None):
        super().__init__()
        self._message = message

    def __str__(self):
        if self._message is None:
            return "OpenPortalError: An unspecified error occurred."
        else:
            return f"OpenPortalError: {self._message}"

    def __repr__(self):
        return f"OpenPortalError(message={self._message})"

    def message(self):
        if self._message is None:
            return "An unspecified error occurred."
        else:
            return self._message


class ManagedProjectPermissionError(OpenPortalError):
    pass


class ManagedProjectRejectedError(ManagedProjectPermissionError):
    def __init__(self, message=None):
        super().__init__()
        self._message = message

    def __str__(self):
        if self._message is None:
            return "ManagedProjectRejectedError: The project is rejected."
        else:
            return f"ManagedProjectRejectedError: {self._message}"

    def __repr__(self):
        return f"ManagedProjectRejectedError(message={self._message})"

    def message(self):
        if self._message is None:
            return "The project is rejected."
        else:
            return self._message


class ManagedProjectPendingError(ManagedProjectPermissionError):
    def __init__(self, message=None):
        super().__init__()
        self._message = message

    def __str__(self):
        if self._message is None:
            return "ManagedProjectPendingError: The project is pending."
        else:
            return f"ManagedProjectPendingError: {self._message}"

    def __repr__(self):
        return f"ManagedProjectPendingError(message={self._message})"

    def message(self):
        if self._message is None:
            return "The project is pending."
        else:
            return self._message


def convert_to_openportal_error(error_message: str) -> OpenPortalError:
    """
    Converts a Waldur OpenPortal error to an OpenPortalError.
    """
    error_message = error_message.lstrip("RuntimeError{").rstrip("}")

    if error_message.startswith("OpenPortalError: "):
        return OpenPortalOtherError(error_message[16:])
    elif error_message.startswith("ManagedProjectRejectedError: "):
        return ManagedProjectRejectedError(error_message[29:])
    elif error_message.startswith("ManagedProjectPendingError: "):
        return ManagedProjectPendingError(error_message[28:])
    else:
        return OpenPortalOtherError(error_message)


def is_config_available():
    """Check if OpenPortal config is available without raising exceptions"""
    # First check if OpenPortal plugin is enabled
    if not settings.WALDUR_OPENPORTAL.get("ENABLED", False):
        return False

    return bool(os.environ.get("OPENPORTAL_CONFIG"))


def ensure_config_loaded():
    """Load config only if available and enabled, return success status

    Returns:
        bool: True if config loaded successfully or already loaded, False if disabled/unavailable
    """
    logger = logging.getLogger(__name__)

    # Check if OpenPortal plugin is enabled
    if not settings.WALDUR_OPENPORTAL.get("ENABLED", False):
        logger.debug("OpenPortal plugin is disabled, skipping config loading")
        return False

    if not is_config_loaded():
        config_file = os.environ.get("OPENPORTAL_CONFIG")
        if not config_file:
            logger.warning(
                "OPENPORTAL_CONFIG environment variable not set, skipping OpenPortal operations"
            )
            return False

        try:
            # this isn't thread-safe - we should make it thread-safe
            # in the OpenPortal python layer because load_config() likely
            # modifies global state without proper synchronization
            load_config(config_file)
            logger.debug(f"OpenPortal config loaded from {config_file}")
            return True
        except Exception as e:
            logger.error(f"Failed to load OpenPortal config from '{config_file}': {e}")
            return False
    return True
