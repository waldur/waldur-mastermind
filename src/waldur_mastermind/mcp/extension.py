from datetime import timedelta

from waldur_core.core import WaldurExtension


class MCPExtension(WaldurExtension):
    class Settings:
        # DOT only defaults these internally, but makemigrations resolves its
        # swappable foreign keys from Django settings when its migrations are
        # disabled, as under pytest --no-migrations.
        OAUTH2_PROVIDER_APPLICATION_MODEL = "oauth2_provider.Application"
        OAUTH2_PROVIDER_ACCESS_TOKEN_MODEL = "oauth2_provider.AccessToken"
        OAUTH2_PROVIDER_REFRESH_TOKEN_MODEL = "oauth2_provider.RefreshToken"
        OAUTH2_PROVIDER_ID_TOKEN_MODEL = "oauth2_provider.IDToken"
        OAUTH2_PROVIDER_GRANT_MODEL = "oauth2_provider.Grant"
        OAUTH2_PROVIDER_DEVICE_GRANT_MODEL = "oauth2_provider.DeviceGrant"

        # Proxies in front of the API that add to X-Forwarded-For. The throttle
        # on new OAuth clients keys on the address the first of them saw, which
        # a client can't set. Helm and docker-compose run one (the ingress or
        # Caddy); 0 uses REMOTE_ADDR, for an API served without a proxy.
        MCP_OAUTH_TRUSTED_PROXY_COUNT = 1

        OAUTH2_PROVIDER = {
            "OAUTH2_VALIDATOR_CLASS": "waldur_mastermind.mcp.oauth.MCPOAuth2Validator",
            "SCOPES": {"mcp:read": "Read your Waldur data"},
            "DEFAULT_SCOPES": ["mcp:read"],
            "PKCE_REQUIRED": True,
            "ACCESS_TOKEN_EXPIRE_SECONDS": 3600,
            "REFRESH_TOKEN_EXPIRE_SECONDS": 30 * 24 * 3600,
            "ROTATE_REFRESH_TOKEN": True,
            "REFRESH_TOKEN_REUSE_PROTECTION": True,
            # Desktop clients listen on an ephemeral localhost port (RFC 8252).
            "ALLOW_LOCALHOST_LOOPBACK": True,
            "DCR_ENABLED": True,
            "DCR_REGISTRATION_PERMISSION_CLASSES": (
                "oauth2_provider.dcr.AllowAllDCRPermission",
            ),
            # DOT's default never expires them, so cleartokens would never remove
            # the token left behind by each open registration.
            "DCR_REGISTRATION_TOKEN_EXPIRE_SECONDS": 30 * 24 * 3600,
            "CIMD_ENABLED": True,
            "CIMD_REGISTRATION_PERMISSION_CLASSES": (
                "waldur_mastermind.mcp.oauth.ThrottledCIMDPermission",
            ),
            "OAUTH2_RESPONSE_TYPES_SUPPORTED": ["code"],
            "OAUTH2_GRANT_TYPES_SUPPORTED": ["authorization_code", "refresh_token"],
            "OAUTH2_TOKEN_ENDPOINT_AUTH_METHODS_SUPPORTED": [
                "none",
                "client_secret_basic",
                "client_secret_post",
            ],
            "COMPLIANT_BCP_RFC9700_IMPLICIT_GRANT": True,
            "COMPLIANT_BCP_RFC9700_PASSWORD_GRANT": True,
            "COMPLIANT_BCP_RFC9700_PKCE_METHOD": True,
            "COMPLIANT_BCP_RFC9700_ACCESS_TOKEN_TRANSPORT": True,
            "COMPLIANT_BCP_RFC9700_AUTHZ_RESPONSE_ISS": True,
            "COMPLIANT_BCP_RFC9700_TOKEN_STORAGE": True,
            "COMPLIANT_BCP_RFC9700_REFRESH_TOKEN": True,
            "COMPLIANT_BCP_RFC9700_REDIRECT_URI_MATCHING": True,
            "COMPLIANT_BCP_RFC9700_PKCE_REQUIRED": True,
        }

    @staticmethod
    def update_settings(settings):
        settings["INSTALLED_APPS"] += ("oauth2_provider",)

    @staticmethod
    def django_app():
        return "waldur_mastermind.mcp"

    @staticmethod
    def django_urls():
        from .urls import urlpatterns

        return urlpatterns

    @staticmethod
    def celery_tasks():
        return {
            "waldur-mcp-clear-expired-oauth-tokens": {
                "task": "waldur_mastermind.mcp.clear_expired_oauth_tokens",
                "schedule": timedelta(hours=24),
                "args": (),
            },
            "waldur-mcp-clear-expired-cimd-applications": {
                "task": "waldur_mastermind.mcp.clear_expired_cimd_applications",
                "schedule": timedelta(hours=24),
                "args": (),
            },
        }
