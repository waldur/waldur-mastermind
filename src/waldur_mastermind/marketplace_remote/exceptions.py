import traceback

from rest_framework.exceptions import APIException


class RemoteWaldurError(Exception):
    pass


class RemoteStatusSyncFailed(APIException):
    status_code = 502

    def __init__(self, error_message, error_description=None):
        tb = traceback.format_exc()
        self.message = f"Error: {error_message}"
        if error_description:
            self.message = f"{self.message} ({error_description})"
        detail = {
            "error_message": self.message,
            "error_traceback": tb,
        }
        super().__init__(detail=detail)

    def __str__(self):
        return self.message
