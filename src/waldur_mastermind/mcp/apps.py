from django.apps import AppConfig
from django.conf import settings
from django.db.models import signals

MCP_PROTOCOL_VERSION_HEADER = "mcp-protocol-version"


def configure_django_cors_headers():
    """Let django-cors-headers answer preflights from browser-based MCP clients.

    Deployments that add it answer preflights before cors_middleware does, with
    its own origin check and header list.
    """
    from corsheaders.defaults import default_headers
    from corsheaders.signals import check_request_enabled

    from waldur_mastermind.mcp import handlers

    check_request_enabled.connect(
        handlers.allow_public_cors,
        dispatch_uid="waldur_mastermind.mcp.handlers.allow_public_cors",
    )
    allowed = getattr(settings, "CORS_ALLOW_HEADERS", default_headers)
    if MCP_PROTOCOL_VERSION_HEADER not in {header.lower() for header in allowed}:
        # Read on every request, so this also extends a list that a deployment's
        # settings override sets after the base settings.
        settings.CORS_ALLOW_HEADERS = (*allowed, MCP_PROTOCOL_VERSION_HEADER)


class MCPConfig(AppConfig):
    name = "waldur_mastermind.mcp"
    verbose_name = "MCP server"

    def ready(self):
        from waldur_core.core.models import User
        from waldur_mastermind.mcp import handlers

        # post_save, so sessions go only once the deactivation is saved.
        # _old_values is set by waldur_core.core's pre_save handler.
        signals.post_save.connect(
            handlers.revoke_oauth_tokens_on_deactivation,
            sender=User,
            dispatch_uid="waldur_mastermind.mcp.handlers.revoke_oauth_tokens_on_deactivation",
        )

        if "corsheaders" in settings.INSTALLED_APPS:
            configure_django_cors_headers()
