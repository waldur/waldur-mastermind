from django import http
from django.conf import settings
from django.utils.csp import CSP
from django.utils.deprecation import MiddlewareMixin

from waldur_core.core import models as core_models

IMPERSONATOR_HEADER = settings.WALDUR_CORE.get("RESPONSE_HEADER_IMPERSONATOR_UUID")


PUBLIC_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
    # MCP clients send MCP-Protocol-Version while discovering the OAuth server.
    "Access-Control-Allow-Headers": "Accept, Content-Type, Content-Length, "
    "Accept-Encoding, Authorization, MCP-Protocol-Version",
    "Access-Control-Max-Age": "3600",
}


def is_public_cors_path(path):
    return path.startswith(tuple(getattr(settings, "PUBLIC_CORS_PATH_PREFIXES", ())))


def add_public_cors_headers(response):
    """Let any origin call the endpoint, without credentials."""
    for name, value in PUBLIC_CORS_HEADERS.items():
        response[name] = value
    return response


# The pages mastermind renders itself: the admin, the browsable API and its
# sign-in, the health check and the landing page. They load their own static files, and run
# inline script only with the nonce of the response. Inline styles stay
# allowed: the admin and its widgets set them, and they cannot run script.
HTML_PAGE_CSP = {
    "default-src": [CSP.SELF],
    "script-src": [CSP.SELF, CSP.NONCE],
    "style-src": [CSP.SELF, CSP.UNSAFE_INLINE],
    "img-src": [CSP.SELF, "data:"],
    "font-src": [CSP.SELF, "data:"],
    "connect-src": [CSP.SELF],
    "form-action": [CSP.SELF],
    "object-src": [CSP.NONE],
    "base-uri": [CSP.NONE],
    "frame-ancestors": [CSP.NONE],
}

OWN_PAGE_PATH_PREFIXES = ("/admin/", "/api-auth-browsable/", "/health-check/")


def renders_own_page(request, response) -> bool:
    """Whether the response is one of the pages mastermind renders itself.

    Named rather than recognised by content type: any other HTML, such as a
    redirect body, an error page or an uploaded file that guesses to HTML,
    keeps the API policy, under which nothing runs.
    """
    if not response.get("Content-Type", "").startswith("text/html"):
        return False
    if 300 <= response.status_code < 400:
        return False
    renderer = getattr(response, "accepted_renderer", None)
    if renderer is not None:
        return getattr(renderer, "format", None) == "api"
    # path_info, not path: under a sub-path deployment (SCRIPT_NAME) path
    # carries the prefix.
    path = request.path_info
    return path == "/" or path.startswith(OWN_PAGE_PATH_PREFIXES)


def html_content_security_policy(get_response):
    """Give mastermind's own pages the page policy instead of the API one.

    Runs before Django's ContentSecurityPolicyMiddleware handles the response
    (it is listed after it), which then renders the policy with the nonce. A
    response that already carries a policy of its own keeps it.
    """

    def middleware(request):
        response = get_response(request)
        if (
            CSP.HEADER_ENFORCE not in response
            and not hasattr(response, "_csp_config")
            and renders_own_page(request, response)
        ):
            response._csp_config = HTML_PAGE_CSP
        return response

    return middleware


def cors_middleware(get_response):
    """
    If CORS preflight header, then create an empty body response (200 OK) and return it.

    Preflights for PUBLIC_CORS_PATH_PREFIXES are allowed from any origin; their
    views add the same headers to their responses.
    """

    def middleware(request):
        if (
            request.method == "OPTIONS"
            and "HTTP_ACCESS_CONTROL_REQUEST_METHOD" in request.META
        ):
            response = http.HttpResponse()
            response["Content-Length"] = "0"
            if is_public_cors_path(request.path):
                add_public_cors_headers(response)
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
