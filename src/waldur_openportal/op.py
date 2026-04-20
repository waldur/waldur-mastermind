import logging
import os

from django.conf import settings

logger = logging.getLogger(__name__)


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


try:
    from openportal import (  # noqa: F401
        Allocation,
        DailyProjectUsageReport,
        DateRange,
        Destination,
        Health,
        Instruction,
        Job,
        Node,
        PortalIdentifier,
        ProjectDetails,
        ProjectIdentifier,
        ProjectMapping,
        ProjectStorageReport,
        ProjectTemplate,
        ProjectUsageReport,
        Quota,
        Status,
        StorageReport,
        Usage,
        UsageReport,
        UserIdentifier,
        UserMapping,
        fetch_job,
        fetch_jobs,
        get,
        get_portal,
        health,
        is_config_loaded,
        load_config,
        run,
        send_result,
        sync_offerings,
    )

    _have_openportal = True

    def have_openportal():
        return _have_openportal

    def ensure_config_loaded():
        if not is_config_loaded():
            try:
                import os

                config_file = os.environ.get("OPENPORTAL_CONFIG")
            except KeyError:
                raise OpenPortalError("OPENPORTAL_CONFIG environment variable not set")

            if not config_file:
                raise OpenPortalError("OPENPORTAL_CONFIG environment variable not set")

            try:
                # this isn't thread-safe - we should make it thread-save
                # in the OpenPortal python layer
                load_config(config_file)
            except Exception as e:
                raise OpenPortalError(
                    f"Failed to load OpenPortal config from '{config_file}': {e}"
                )

except ImportError:
    _have_openportal = False

    def have_openportal():
        return _have_openportal

    def _raise_no_openportal_error():
        raise OpenPortalError("OpenPortal is not installed.")

    class Allocation:
        def __init__(self, *args, **kwargs):
            _raise_no_openportal_error()

    class DailyProjectUsageReport:
        # Properties (Rust #[getter])
        @property
        def num_jobs(self):
            _raise_no_openportal_error()

        @property
        def total_wait_seconds(self):
            _raise_no_openportal_error()

        @property
        def is_consistent(self):
            _raise_no_openportal_error()

        @property
        def average_wait_seconds(self):
            _raise_no_openportal_error()

        @property
        def components(self):
            _raise_no_openportal_error()
            return []

        @property
        def total_usage(self):
            _raise_no_openportal_error()

        @property
        def is_complete(self):
            _raise_no_openportal_error()

        # Regular methods
        def __init__(self, *args, **kwargs):
            _raise_no_openportal_error()

        def add_unattributed_usage(self, *args, **kwargs):
            _raise_no_openportal_error()

        def set_complete(self, *args, **kwargs):
            _raise_no_openportal_error()

    class Destination:
        def __init__(self, *args, **kwargs):
            _raise_no_openportal_error()

    class Health:
        def __init__(self, *args, **kwargs):
            _raise_no_openportal_error()

    class Instruction:
        def __init__(self, *args, **kwargs):
            _raise_no_openportal_error()

    class Job:
        def __init__(self, *args, **kwargs):
            _raise_no_openportal_error()

    class Node:
        def __init__(self, *args, **kwargs):
            _raise_no_openportal_error()

    class PortalIdentifier:
        def __init__(self, *args, **kwargs):
            _raise_no_openportal_error()

    class ProjectIdentifier:
        def __init__(self, *args, **kwargs):
            _raise_no_openportal_error()

    class ProjectMapping:
        def __init__(self, *args, **kwargs):
            _raise_no_openportal_error()

    class UserIdentifier:
        def __init__(self, *args, **kwargs):
            _raise_no_openportal_error()

    class UserMapping:
        def __init__(self, *args, **kwargs):
            _raise_no_openportal_error()

    class DateRange:
        def __init__(self, *args, **kwargs):
            _raise_no_openportal_error()

    class UsageReport:
        def __init__(self, *args, **kwargs):
            _raise_no_openportal_error()

        @staticmethod
        def combine(*args, **kwargs):
            _raise_no_openportal_error()

        def filter(self, *args, **kwargs):
            _raise_no_openportal_error()
            return self

    class Usage:
        def __init__(self, *args, **kwargs):
            _raise_no_openportal_error()

        @staticmethod
        def from_hours(*args, **kwargs):
            _raise_no_openportal_error()

    class Quota:
        def __init__(self, *args, **kwargs):
            _raise_no_openportal_error()

    class StorageReport:
        def __init__(self, *args, **kwargs):
            _raise_no_openportal_error()

        def filter(self, *args, **kwargs):
            _raise_no_openportal_error()
            return self

    class ProjectStorageReport:
        # Properties (Rust #[getter])
        @property
        def project(self):
            _raise_no_openportal_error()

        @property
        def generated_at(self):
            _raise_no_openportal_error()

        @property
        def project_quotas(self):
            _raise_no_openportal_error()

        @property
        def user_quotas(self):
            _raise_no_openportal_error()

        @property
        def users(self):
            _raise_no_openportal_error()
            return []

        @property
        def user_mapping(self):
            _raise_no_openportal_error()

        # Regular methods
        def __init__(self, *args, **kwargs):
            _raise_no_openportal_error()

        @staticmethod
        def from_json(*args, **kwargs):
            _raise_no_openportal_error()

        @staticmethod
        def combine(*args, **kwargs):
            _raise_no_openportal_error()

        def to_json(self):
            _raise_no_openportal_error()

        def is_empty(self):
            _raise_no_openportal_error()

        def daily_reports(self, *args, **kwargs):
            _raise_no_openportal_error()
            return []

        def get_report(self, *args, **kwargs):
            _raise_no_openportal_error()

        def remap_project(self, *args, **kwargs):
            _raise_no_openportal_error()

        def remap_portal(self, *args, **kwargs):
            _raise_no_openportal_error()

        def remap_users(self, *args, **kwargs):
            _raise_no_openportal_error()

        def filter(self, *args, **kwargs):
            _raise_no_openportal_error()
            return self

        def __iadd__(self, other):
            _raise_no_openportal_error()
            return self

        def __add__(self, other):
            _raise_no_openportal_error()
            return self

    class ProjectUsageReport(UsageReport):
        # Properties (Rust #[getter])
        @property
        def dates(self):
            _raise_no_openportal_error()
            return []

        @property
        def components(self):
            _raise_no_openportal_error()
            return []

        @property
        def project(self):
            _raise_no_openportal_error()

        @property
        def portal(self):
            _raise_no_openportal_error()

        @property
        def users(self):
            _raise_no_openportal_error()
            return []

        @property
        def user_mapping(self):
            _raise_no_openportal_error()

        @property
        def unmapped_users(self):
            _raise_no_openportal_error()
            return []

        @property
        def total_usage(self):
            _raise_no_openportal_error()

        @property
        def num_jobs(self):
            _raise_no_openportal_error()

        @property
        def total_wait_seconds(self):
            _raise_no_openportal_error()

        @property
        def average_wait_seconds(self):
            _raise_no_openportal_error()

        @property
        def unmapped_usage(self):
            _raise_no_openportal_error()

        @property
        def is_complete(self):
            _raise_no_openportal_error()

        # Regular methods
        def __init__(self, *args, **kwargs):
            _raise_no_openportal_error()

        @staticmethod
        def from_json(*args, **kwargs):
            _raise_no_openportal_error()

        @staticmethod
        def combine(*args, **kwargs):
            _raise_no_openportal_error()

        def to_json(self):
            _raise_no_openportal_error()

        def in_hours(self):
            _raise_no_openportal_error()

        def usage(self, *args, **kwargs):
            _raise_no_openportal_error()

        def get_report(self, *args, **kwargs):
            _raise_no_openportal_error()

        def get_component(self, *args, **kwargs):
            _raise_no_openportal_error()

        def add_mapping(self, *args, **kwargs):
            _raise_no_openportal_error()

        def add_mappings(self, *args, **kwargs):
            _raise_no_openportal_error()

        def set_project(self, *args, **kwargs):
            _raise_no_openportal_error()

        def scale_total(self, *args, **kwargs):
            _raise_no_openportal_error()

        def set_report(self, *args, **kwargs):
            _raise_no_openportal_error()

        def add_report(self, *args, **kwargs):
            _raise_no_openportal_error()

        def daily_reports(self, *args, **kwargs):
            _raise_no_openportal_error()
            return []

        def set_complete(self, *args, **kwargs):
            _raise_no_openportal_error()

        def set_day_complete(self, *args, **kwargs):
            _raise_no_openportal_error()

        def to_usage_report(self, *args, **kwargs):
            _raise_no_openportal_error()

        def remap_project(self, *args, **kwargs):
            _raise_no_openportal_error()

        def remap_portal(self, *args, **kwargs):
            _raise_no_openportal_error()

        def remap_users(self, *args, **kwargs):
            _raise_no_openportal_error()

        def filter(self, *args, **kwargs):
            _raise_no_openportal_error()
            return self

        def __iadd__(self, other):
            _raise_no_openportal_error()
            return self

        def __add__(self, other):
            _raise_no_openportal_error()
            return self

    class ProjectTemplate:
        def __init__(self, *args, **kwargs):
            _raise_no_openportal_error()

    def is_config_loaded():
        _raise_no_openportal_error()

    def load_config(*args, **kwargs):
        _raise_no_openportal_error()

    def health(*args, **kwargs):
        _raise_no_openportal_error()

    def get(*args, **kwargs):
        _raise_no_openportal_error()

    def get_portal(*args, **kwargs):
        _raise_no_openportal_error()

    def sync_offerings(*args, **kwargs):
        _raise_no_openportal_error()

    def run(*args, **kwargs):
        _raise_no_openportal_error()


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
