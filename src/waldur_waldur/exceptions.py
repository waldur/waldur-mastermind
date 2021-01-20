class WaldurClientException(Exception):
    pass


class BadRequest(WaldurClientException):
    """General purpose exception class."""


class Unauthorized(BadRequest):
    """Raised when invalid credentials are provided."""

    message = 'Unauthorized: bad credentials.'


class NotFound(BadRequest):
    pass
