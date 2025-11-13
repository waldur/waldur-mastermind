# flake8: noqa: F401


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
        def __init__(self, *args, **kwargs):
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

    class Usage:
        def __init__(self, *args, **kwargs):
            _raise_no_openportal_error()

    class ProjectUsageReport:
        def __init__(self, *args, **kwargs):
            _raise_no_openportal_error()

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

    def ensure_config_loaded():
        _raise_no_openportal_error()
