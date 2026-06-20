from rest_framework import status
from rest_framework.exceptions import APIException


class OAuthException(APIException):
    status_code = status.HTTP_401_UNAUTHORIZED

    def __init__(
        self, provider, error_message, error_description=None, user_facing=False
    ):
        # When True, the message is meant for the end user; the OIDC complete view
        # surfaces it on the login-failed page instead of returning a JSON response.
        self.user_facing = user_facing
        self.user_message = error_message
        if error_description:
            self.user_message = f"{error_message} ({error_description})"
        self.message = f"{provider} error: {self.user_message}"
        super().__init__(detail=self.message)

    def __str__(self):
        return self.message
