"""SCIM 2.0 error responses (RFC 7644 §3.12)."""

from rest_framework.response import Response
from rest_framework.views import exception_handler

ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"


class ScimError(Exception):
    """Raised by SCIM views to produce an RFC 7644 §3.12 error response."""

    def __init__(
        self,
        status_code: int,
        detail: str,
        scim_type: str | None = None,
    ):
        self.status_code = status_code
        self.detail = detail
        self.scim_type = scim_type
        super().__init__(detail)

    def to_response(self) -> Response:
        body: dict = {
            "schemas": [ERROR_SCHEMA],
            "status": str(self.status_code),
            "detail": self.detail,
        }
        if self.scim_type:
            body["scimType"] = self.scim_type
        return Response(body, status=self.status_code)


def scim_exception_handler(exc, context):
    """DRF exception handler that emits SCIM-shaped error bodies."""
    if isinstance(exc, ScimError):
        return exc.to_response()

    response = exception_handler(exc, context)
    if response is None:
        return None

    detail = response.data
    if isinstance(detail, dict) and "detail" in detail:
        message = str(detail["detail"])
    else:
        message = str(detail)

    response.data = {
        "schemas": [ERROR_SCHEMA],
        "status": str(response.status_code),
        "detail": message,
    }
    return response
