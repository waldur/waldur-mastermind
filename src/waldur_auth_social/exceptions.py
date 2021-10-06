from rest_framework import status
from rest_framework.exceptions import APIException


class OAuthException(APIException):
    status_code = status.HTTP_401_UNAUTHORIZED

    def __init__(self, provider, error_message, error_description=None):
        self.message = '%s error: %s' % (provider, error_message)
        if error_description:
            self.message = '%s (%s)' % (self.message, error_description)
        super(OAuthException, self).__init__(detail=self.message)

    def __str__(self):
        return self.message
