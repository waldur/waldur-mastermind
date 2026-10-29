from django.urls import include, path
from oauth2_provider import views as oauth2_views

from waldur_mastermind.mcp import views
from waldur_mastermind.mcp.views import public_oauth_endpoint

# Under DOT's own namespace, because its views reverse "oauth2_provider:<name>".
oauth_urlpatterns = [
    path(
        "authorize/",
        public_oauth_endpoint(views.AuthorizationView.as_view()),
        name="authorize",
    ),
    path(
        "token/",
        public_oauth_endpoint(views.TokenView.as_view()),
        name="token",
    ),
    path(
        "revoke_token/",
        public_oauth_endpoint(oauth2_views.RevokeTokenView.as_view()),
        name="revoke-token",
    ),
    path(
        "register/",
        public_oauth_endpoint(views.DynamicClientRegistrationView.as_view()),
        name="dcr-register",
    ),
    # RFC 7592 management: DCR's response links to it, so reverse() needs it.
    path(
        "register/<str:client_id>/",
        public_oauth_endpoint(views.DynamicClientRegistrationManagementView.as_view()),
        name="dcr-register-management",
    ),
]

urlpatterns = [
    # RFC 8414 puts the metadata at the issuer's root, which is the API host.
    path(
        ".well-known/oauth-authorization-server",
        public_oauth_endpoint(oauth2_views.OAuthServerMetadataView.as_view()),
        name="oauth-server-metadata",
    ),
    path("oauth/", include((oauth_urlpatterns, "oauth2_provider"))),
]
