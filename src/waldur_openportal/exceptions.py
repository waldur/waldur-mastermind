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


class OpenPortalUnsupportedCommandError(OpenPortalError):
    """
    Raised when the remote portal does not understand a command we sent,
    which normally means it runs an older OpenPortal version. Callers use
    this to fall back to whatever local state they already hold.

    Defined here because the openportal SDK does not provide it, as of 0.91.0.
    Catching a name the SDK lacks costs nothing until the fallback is needed,
    and then raises AttributeError while handling the original error. Check
    before switching to an SDK-provided one:

        >>> hasattr(openportal, "OpenPortalUnsupportedCommandError")

    See docs/guides/how-to-reconcile-a-fork.md, section 7.
    """

    def __init__(self, message=None):
        super().__init__()
        self._message = message

    def __str__(self):
        if self._message is None:
            return "OpenPortalUnsupportedCommandError: The remote portal does not support this command."
        else:
            return f"OpenPortalUnsupportedCommandError: {self._message}"

    def __repr__(self):
        return f"OpenPortalUnsupportedCommandError(message={self._message})"

    def message(self):
        if self._message is None:
            return "The remote portal does not support this command."
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
