from django import http
from django.conf import settings
from django.utils.deprecation import MiddlewareMixin

from waldur_core.core import models as core_models

IMPERSONATOR_HEADER = settings.WALDUR_CORE.get("RESPONSE_HEADER_IMPERSONATOR_UUID")


def cors_middleware(get_response):
    """
    If CORS preflight header, then create an empty body response (200 OK) and return it
    """

    def middleware(request):
        if (
            request.method == "OPTIONS"
            and "HTTP_ACCESS_CONTROL_REQUEST_METHOD" in request.META
        ):
            response = http.HttpResponse()
            response["Content-Length"] = "0"
            return response

        return get_response(request)

    return middleware


class ImpersonationMiddleware(MiddlewareMixin):
    def process_response(self, request, response):
        user = getattr(request, "user")

        if isinstance(user, core_models.ImpersonatedUser):
            impersonator_uuid = user.impersonator.uuid.hex
            response.headers[IMPERSONATOR_HEADER] = impersonator_uuid

        return response
