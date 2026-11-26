from rest_framework import status
from rest_framework.exceptions import APIException

from waldur_core.core.enums import LoginFailureCode


class OAuthException(APIException):
    status_code = status.HTTP_401_UNAUTHORIZED

    def __init__(
        self,
        provider,
        error_message,
        error_description=None,
        code: LoginFailureCode | None = None,
    ):
        # With a code, the OIDC complete view sends the browser to the
        # login-failed page with that code instead of returning JSON. The
        # message goes to the failed-login event and the logs, not the URL.
        self.code = code
        self.user_message = error_message
        if error_description:
            self.user_message = f"{error_message} ({error_description})"
        self.message = f"{provider} error: {self.user_message}"
        super().__init__(detail=self.message)

    def __str__(self):
        return self.message
