class RancherException(Exception):
    pass


class BadRequest(RancherException):
    """General purpose exception class."""


class Unauthorized(BadRequest):
    """Raised when invalid credentials are provided."""

    message = 'Unauthorized: bad credentials.'
