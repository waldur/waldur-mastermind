import logging

from drf_spectacular.utils import extend_schema
from rest_framework import permissions as rf_permissions
from rest_framework import serializers, status
from rest_framework.authtoken.models import Token
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import NotFound
from rest_framework.response import Response

from waldur_core.core.permissions import IsStaff
from waldur_core.web_shell import tickets

logger = logging.getLogger(__name__)


class WebShellTicketSerializer(serializers.Serializer):
    url = serializers.CharField(
        read_only=True,
        help_text="Single-use link that opens the web shell. "
        "Valid for one minute; the ticket is in the URL fragment.",
    )


@extend_schema(
    summary="Open a web shell session",
    description="Returns a single-use link that opens `waldur shell` in the browser "
    "as the calling staff user. Only available when the deployment runs with DEBUG "
    "and WALDUR_CORE['WEB_SHELL_ENABLED'].",
    request=None,
    responses={status.HTTP_200_OK: WebShellTicketSerializer},
)
@api_view(["POST"])
@permission_classes((rf_permissions.IsAuthenticated, IsStaff))
def web_shell_ticket(request):
    if not tickets.is_enabled():
        raise NotFound()
    # Bind the shell to this login, so that logging out ends it. Requests
    # authenticated otherwise (PAT, OIDC bearer) carry no Waldur API token.
    token_key = request.auth.key if isinstance(request.auth, Token) else None
    url = tickets.build_url(tickets.mint(request.user, token_key))
    logger.info("Web shell ticket issued for user %s", request.user.username)
    return Response({"url": url})
