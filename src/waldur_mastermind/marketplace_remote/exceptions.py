from rest_framework.exceptions import APIException


class RemoteWaldurError(Exception):
    pass


class RemoteStatusSyncFailed(APIException):
    status_code = 502

    # The response carries no traceback and no remote response body: the
    # remote's answer is logged, not returned (see describe_remote_error).
    def __init__(self, error_message, error_description=None):
        self.message = f"Error: {error_message}"
        if error_description:
            self.message = f"{self.message} ({error_description})"
        detail = {"error_message": self.message}
        super().__init__(detail=detail)

    def __str__(self):
        return self.message
