"""How Waldur stops a call to a remote Waldur: errors that are safe to show.

Each is an ``httpx.TransportError``, as a failed connection is, so the code
that handles one handles the other. They live apart from
``waldur_core.core.pinned_transport`` so that code catching them does not load
httpcore at Django startup.
"""

import httpx


class RemoteCallRefused(httpx.TransportError):
    """Waldur stopped a remote call: an answer it will not read, or a limit.

    ``user_message`` is safe to show users; ``reason``, which ``str()``
    includes, has the details for the log.
    """

    user_message = "The remote Waldur call was stopped."

    def __init__(self, reason: str):
        # The reason is in str() for the logs; users get user_message.
        super().__init__(f"{self.user_message} ({reason})")
        self.reason = reason


class EncodedResponseRefused(RemoteCallRefused):
    user_message = "The remote Waldur sent an encoded answer, which is not accepted."


class ResponseTooLarge(RemoteCallRefused):
    user_message = "The remote Waldur's answer is too large."


class TooManyRequests(RemoteCallRefused):
    user_message = "The remote Waldur call needed more requests than allowed."


class RemoteCallTimeout(RemoteCallRefused):
    user_message = "The remote Waldur took too long to answer."


class InvalidRemoteAnswer(RemoteCallRefused):
    user_message = "The remote Waldur's answer could not be read."
