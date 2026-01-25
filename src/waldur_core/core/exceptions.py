from django.utils.translation import gettext_lazy as _
from rest_framework import status
from rest_framework.exceptions import APIException


class IncorrectStateException(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = _("Cannot modify an object in its current state.")


class RuntimeStateException(Exception):
    pass


class ExtensionDisabled(APIException):
    status_code = status.HTTP_424_FAILED_DEPENDENCY
    default_detail = _("Extension is disabled.")


class IncorrectMethodException(APIException):
    status_code = status.HTTP_405_METHOD_NOT_ALLOWED
    default_detail = _("Method not allowed.")


class IncompleteProfileException(APIException):
    status_code = status.HTTP_428_PRECONDITION_REQUIRED
    default_detail = _(
        "User profile is incomplete. Please fill in all mandatory fields."
    )
    default_code = "incomplete_profile"

    def __init__(self, missing_fields=None, detail=None, code=None):
        super().__init__(detail=detail, code=code)
        if missing_fields:
            self.detail = {
                "detail": str(self.default_detail),
                "code": self.default_code,
                "missing_fields": missing_fields,
            }
